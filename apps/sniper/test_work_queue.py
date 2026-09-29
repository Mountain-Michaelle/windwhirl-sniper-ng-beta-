"""Tests for the shared row-claim cursor used by ``--workers`` shards."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper.work_queue import OrderClaimQueue  # noqa: E402


def test_seconds_until_stealable_is_zero_while_the_band_is_unspent(tmp_path):
    """Nothing has been spent yet, so there is no grace period to wait out.

    The old idle path slept a flat 1s unconditionally on this route; the
    dynamic wait must report 0 so the caller re-asks immediately.
    """
    queue = _queue(tmp_path)

    assert queue.seconds_until_stealable() == 0.0


def test_seconds_until_stealable_counts_down_then_floors_at_zero(tmp_path):
    """A spent band reports the real remaining grace, never a negative
    number (which would turn into a bogus sleep or, worse, a negative-time
    'already expired' claim)."""
    now = [1000.0]
    queue = OrderClaimQueue(tmp_path / "claims", shard=(0, 2),
                            steal_grace_s=20.0, clock=lambda: now[0])

    # Burn ONLY this worker's reserved band (even positions). The odd ones
    # stay unclaimed, standing in for a live peer that still owns them --
    # which is the only situation the grace period actually exists for.
    while (claim := queue.claim_next(4)) is not None:
        queue.mark_complete(claim)
    assert claim in (None, 0, 2)

    assert queue.claim_next(4) is None           # grace has not elapsed yet
    assert 0.0 < queue.seconds_until_stealable() <= 20.0

    now[0] += 10.0
    mid = queue.seconds_until_stealable()
    assert 9.0 < mid < 11.0                     # ~10s of the 20s left

    now[0] += 1000.0                            # long past the deadline
    assert queue.seconds_until_stealable() == 0.0
    # The peer's unclaimed odd row is now stealable.
    assert queue.claim_next(4) is not None


def test_zero_grace_queue_never_reports_a_wait(tmp_path):
    """steal_grace_s=0 must stay a genuinely immediate steal -- the dynamic
    wait must not reintroduce a floor that reintroduces latency."""
    queue = OrderClaimQueue(tmp_path / "claims", shard=(0, 2),
                            steal_grace_s=0.0)

    # Claim exactly this worker's reserved band (even positions) and stop.
    # With no grace there is no "band spent" pause at all: the very next
    # claim already reaches for a peer's row, so the loop below would
    # otherwise drain the whole file.
    for _ in range(2):
        claim = queue.claim_next(4)
        assert claim is not None
        queue.mark_complete(claim)

    assert queue.seconds_until_stealable() == 0.0
    stolen = queue.claim_next(4)
    assert stolen in (1, 3)               # a peer's row, taken with no delay


# ── JobLock: the dynamic mid-write wait must not weaken any guarantee ──────
#
# JobLock gates EVERY client job, so the conversion from "sleep 100ms, look
# once" to "poll until the file is readable" is only acceptable if all three
# original outcomes survive exactly:
#   live owner     -> RuntimeError (refuse to start)
#   dead owner     -> take over (stale recovery)
#   unparseable    -> take over (treated as stale), after the grace period


def test_job_lock_refuses_to_start_when_a_live_process_holds_it(tmp_path):
    import json as _json
    import os as _os

    from apps.sniper.client_workspace import JobLock

    path = tmp_path / "job.lock"
    path.write_text(_json.dumps({"pid": _os.getpid()}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="already running"):
        JobLock(path).__enter__()


def test_job_lock_takes_over_from_a_dead_owner_without_waiting(tmp_path):
    """A definitively dead owner is decided on the FIRST read -- the grace
    period only ever applies to the ambiguous unparseable case, so crash
    recovery is not slowed down at all."""
    import json as _json

    from apps.sniper.client_workspace import JobLock

    path = tmp_path / "job.lock"
    # A pid that is valid but certainly not running on this machine.
    path.write_text(_json.dumps({"pid": 0x7FFFFFFF}), encoding="utf-8")

    started = time.perf_counter()
    with JobLock(path) as lock:
        assert lock.fd is not None
    assert time.perf_counter() - started < 0.2, \
        "a dead owner must not pay the mid-write grace period"


def test_job_lock_waits_out_a_mid_write_lock_then_respects_a_live_owner(
        tmp_path, monkeypatch):
    """The window the dynamic wait exists for.

    A lock file that exists but has not been written yet is indistinguishable
    from a corrupt one by a single read. The OLD code slept a flat 100ms and
    re-read exactly once, so a lock that finished writing a microsecond later
    was still called corrupt and stolen from a LIVE owner. Here the file
    becomes readable -- naming a live pid -- partway through, and the lock
    must be refused rather than taken.
    """
    import json as _json
    import os as _os

    from apps.sniper import client_workspace as cw

    path = tmp_path / "job.lock"
    path.write_text("", encoding="utf-8")     # created, pid not yet written

    live_payload = _json.dumps({"pid": _os.getpid()})
    state = {"reads": 0}
    real_read = cw._lock_owner_verdict

    def flaky_verdict(p):
        state["reads"] += 1
        if state["reads"] == 1:
            return None                        # still mid-write
        path.write_text(live_payload, encoding="utf-8")
        return real_read(p)

    monkeypatch.setattr(cw, "_lock_owner_verdict", flaky_verdict)

    with pytest.raises(RuntimeError, match="already running"):
        cw.JobLock(path).__enter__()

    assert state["reads"] >= 2, "must re-read, not decide on the first miss"


def test_job_lock_still_recovers_from_a_permanently_corrupt_lock(
        tmp_path, monkeypatch):
    """A lock that never becomes readable must end up stale -- exactly as
    before the change, which also recovered from unparseable content. This
    is the guard against the dynamic wait turning a recoverable lock into a
    job that can never start."""
    from apps.sniper import client_workspace as cw

    path = tmp_path / "job.lock"
    path.write_text("}{ not json at all", encoding="utf-8")

    # Keep the grace period short so the test does not sit for the real 0.5s.
    monkeypatch.setattr(cw, "_LOCK_WRITE_GRACE_S", 0.05)

    with cw.JobLock(path) as lock:
        assert lock.fd is not None


def _queue(tmp_path) -> OrderClaimQueue:
    return OrderClaimQueue(tmp_path / "claims")


def test_first_claim_is_zero(tmp_path):
    queue = _queue(tmp_path)

    assert queue.claim_next(3) == 0


def test_claims_advance_only_after_mark_complete(tmp_path):
    queue = _queue(tmp_path)
    queue.reset()

    first = queue.claim_next(3)
    assert first == 0
    # Not confirmed yet -> the same row is re-offered (crash-safe).
    assert queue.claim_next(3) == first

    queue.mark_complete(first)
    assert queue.claim_next(3) == 1
    queue.mark_complete(1)
    assert queue.claim_next(3) == 2
    queue.mark_complete(2)
    assert queue.claim_next(3) is None
    assert queue.in_flight is None


def test_in_flight_row_is_resumed_after_a_crashed_session(tmp_path):
    worker = _queue(tmp_path)
    worker.reset()
    assert worker.claim_next(5) == 0
    # Simulate the session raising before mark_complete(): the worker process
    # stays alive (``--lock`` restart) and must finish the same row.
    assert worker.in_flight == 0

    assert worker.claim_next(5) == 0
    worker.mark_complete(0)
    assert worker.claim_next(5) == 1


def test_worker_scans_past_rows_claimed_by_another_worker(tmp_path):
    first_worker = _queue(tmp_path)
    first_worker.reset()
    assert first_worker.claim_next(3) == 0
    first_worker.mark_complete(0)

    second_worker = _queue(tmp_path)

    # Row 0 is taken, so this worker must start at row 1.
    assert second_worker.claim_next(3) == 1
    # And the first worker must skip the row it handed over: row 2, not row 1.
    assert first_worker.claim_next(3) == 2
    first_worker.mark_complete(2)
    assert first_worker.claim_next(3) is None


def test_reset_reopens_rows_skipped_by_a_dead_worker(tmp_path):
    crashed = _queue(tmp_path)
    crashed.reset()
    assert crashed.claim_next(2) == 0
    # Worker process dies; its sentinel stays behind.

    next_run = _queue(tmp_path)
    next_run.reset()

    assert next_run.claim_next(2) == 0


def test_reset_clears_in_flight(tmp_path):
    queue = _queue(tmp_path)
    queue.reset()
    queue.claim_next(4)
    assert queue.in_flight == 0

    queue.reset()

    assert queue.in_flight is None
    assert queue.claim_next(4) == 0


def test_claiming_stops_at_total(tmp_path):
    queue = _queue(tmp_path)

    claimed = []
    while True:
        index = queue.claim_next(2)
        if index is None:
            break
        claimed.append(index)
        queue.mark_complete(index)

    assert claimed == [0, 1]


class FakeClock:
    """Manually advanced clock so the steal delay is testable without sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _sharded_queue(tmp_path, index: int, count: int, clock=None,
                   steal_grace_s: float = 20.0) -> OrderClaimQueue:
    return OrderClaimQueue(tmp_path / "claims", shard=(index, count),
                           steal_grace_s=steal_grace_s, clock=clock or FakeClock())


def test_reserved_bands_split_evenly_regardless_of_start_order(tmp_path):
    """A worker that starts first must not swallow the whole file.

    This is the real production ordering: a worker reusing a cached session
    reaches the claim loop before a sibling that still has to log in.
    """
    total = 60
    worker_count = 4

    # Worker 0 runs to exhaustion before the others ever start.
    eager = _sharded_queue(tmp_path, 0, worker_count)
    taken_alone = []
    while True:
        index = eager.claim_next(total)
        if index is None:
            break
        taken_alone.append(index)
        eager.mark_complete(index)
    # Its own reserved band is exactly 1/4 of the rows -- never the whole file.
    assert taken_alone == [i for i in range(total) if i % worker_count == 0]

    # The late workers still find their full bands, and all rows are covered.
    bands = {index: [] for index in range(worker_count)}
    bands[0].extend(taken_alone)
    for index in range(1, worker_count):
        queue = _sharded_queue(tmp_path, index, worker_count)
        while True:
            position = queue.claim_next(total)
            if position is None:
                break
            bands[index].append(position)
            queue.mark_complete(position)

    for index, rows in bands.items():
        assert rows == [i for i in range(total) if i % worker_count == index]
    assert sorted(i for rows in bands.values() for i in rows) == list(range(total))


def test_worker_waits_out_the_steal_delay_before_touching_a_peers_band(tmp_path):
    """A live peer's untouched rows must not be stolen the moment a band ends."""
    clock = FakeClock()
    total = 6
    worker_count = 2

    worker_zero = _sharded_queue(tmp_path, 0, worker_count, clock=clock)
    # Own band (0, 2, 4) is fast; the peer's band (1, 3, 5) is the slow work.
    for expected in (0, 2, 4):
        assert worker_zero.claim_next(total) == expected
        worker_zero.mark_complete(expected)

    # Band spent, but the steal delay has not elapsed: it must wait rather than
    # grab the peer's rows, and the rows are still visibly unclaimed.
    assert worker_zero.claim_next(total) is None
    assert worker_zero.unclaimed_in(range(total)) == 3

    # Still waiting just before the delay expires.
    clock.advance(19.0)
    assert worker_zero.claim_next(total) is None

    # Once the delay passes, the idle worker absorbs the peer's abandoned work.
    clock.advance(2.0)
    assert worker_zero.claim_next(total) == 1
    worker_zero.mark_complete(1)
    assert worker_zero.claim_next(total) == 3


def test_single_worker_behaves_like_a_plain_scan(tmp_path):
    queue = _queue(tmp_path)

    claimed = []
    while True:
        index = queue.claim_next(4)
        if index is None:
            break
        claimed.append(index)
        queue.mark_complete(index)

    assert claimed == [0, 1, 2, 3]


def test_parallel_sharded_workers_never_claim_the_same_row(tmp_path):
    total = 64
    worker_count = 4
    barrier = threading.Barrier(worker_count)
    lock = threading.Lock()
    per_worker: dict[int, list[int]] = {index: [] for index in range(worker_count)}

    def worker(index: int) -> None:
        # A real, small grace so the bands do the balancing; the run still ends
        # because each worker keeps asking until nothing is left unclaimed.
        queue = _sharded_queue(tmp_path, index, worker_count, steal_grace_s=0.05)
        barrier.wait()
        idle_rounds = 0
        while idle_rounds < 4:
            position = queue.claim_next(total)
            if position is None:
                idle_rounds += 1
                time.sleep(0.02)
                continue
            idle_rounds = 0
            with lock:
                per_worker[index].append(position)
            queue.mark_complete(position)

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    everything = [row for rows in per_worker.values() for row in rows]
    assert sorted(everything) == list(range(total))
    # The reserved bands keep the split even rather than first-come-first-served.
    counts = [len(rows) for rows in per_worker.values()]
    assert max(counts) - min(counts) <= 1


def test_parallel_workers_never_claim_the_same_row(tmp_path):
    total = 40
    worker_count = 4
    barrier = threading.Barrier(worker_count)
    claimed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        queue = _queue(tmp_path)
        barrier.wait()
        while True:
            index = queue.claim_next(total)
            if index is None:
                return
            with lock:
                claimed.append(index)
            queue.mark_complete(index)

    threads = [threading.Thread(target=worker) for _ in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert sorted(claimed) == list(range(total))
