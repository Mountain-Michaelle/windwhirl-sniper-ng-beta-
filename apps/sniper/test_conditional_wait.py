"""Tests for poll_until -- the early-exit wait that replaced the fixed
``time.sleep`` in the uploader's coordination paths.

The contract these tests defend, and the reason the conversion is safe at
all: a condition that is ALREADY true must cost one call and zero wait, and
a condition that becomes true must return at that moment rather than at the
end of some fixed quantum. The deadline must still be honoured, because the
callers' "give up and carry on degraded" behaviour depends on it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper.conditional_wait import poll_until  # noqa: E402


class _FakeClock:
    """Monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_already_satisfied_costs_one_call_and_no_wait():
    """The headline win: an uncontended lock used to sleep a flat quantum
    before it even looked. It must now return instantly."""
    clock = _FakeClock()
    calls = []

    def condition():
        calls.append(1)
        return "got it"

    result = poll_until(condition, 5.0, clock=clock, sleep=clock.sleep)

    assert result == "got it"
    assert len(calls) == 1
    assert clock.sleeps == []


def test_returns_as_soon_as_the_condition_flips():
    """Becomes true on the 4th check: we stop there, not after the timeout."""
    clock = _FakeClock()
    state = {"n": 0}

    def condition():
        state["n"] += 1
        return "ready" if state["n"] >= 4 else None

    result = poll_until(condition, 60.0, clock=clock, sleep=clock.sleep)

    assert result == "ready"
    assert state["n"] == 4
    # 5ms + 10ms + 25ms of backing-off, then the condition held.
    assert clock.sleeps == [0.005, 0.010, 0.025]


def test_intervals_back_off_so_a_slow_peer_is_not_hammered():
    """The first re-test is fast; later ones stretch out and then stay
    capped. A sibling holding a lock for a long time must not cost a tight
    syscall loop."""
    clock = _FakeClock()

    assert poll_until(lambda: None, 500.0,
                      clock=clock, sleep=clock.sleep) is None

    assert clock.sleeps[:4] == [0.005, 0.010, 0.025, 0.050]
    assert max(clock.sleeps) == 1.0             # capped, not growing forever
    # The clipped tail is the only thing allowed to break the ramp.
    head = clock.sleeps[:-1]
    assert head == sorted(head)
    assert clock.sleeps[-1] <= 1.0


def test_never_sleeps_past_the_deadline():
    """A long final interval must be clipped, or the caller would wait longer
    than the timeout it asked for -- the exact regression a fixed sleep
    risks and the reason the deadline is authoritative here."""
    clock = _FakeClock()

    poll_until(lambda: None, 0.12, clock=clock, sleep=clock.sleep)

    assert clock.now <= 0.12 + 1e-9
    assert clock.sleeps[-1] <= 0.12


def test_zero_timeout_does_a_single_check_and_never_sleeps():
    """timeout_s=0 is the 'already out of time' case: exactly one condition
    evaluation, no sleeping at all."""
    clock = _FakeClock()
    calls = []

    def condition():
        calls.append(1)
        return None

    assert poll_until(condition, 0.0, clock=clock, sleep=clock.sleep) is None
    assert len(calls) == 1
    assert clock.sleeps == []


def test_never_sleeps_at_all_when_the_deadline_has_already_passed():
    """A clock that jumps past the deadline during the first check must not
    produce a sleep -- the wait is over, so waiting longer would be wrong."""
    clock = _FakeClock()
    now = [0.0]

    def condition():
        now[0] += 100.0          # time leaps forward inside the check
        return None

    assert poll_until(condition, 0.5,
                      clock=lambda: now[0], sleep=clock.sleep) is None


def test_returns_the_condition_value_not_just_truthiness():
    """Callers hand back an object (a JobLock, a self). It must survive."""
    clock = _FakeClock()
    sentinel = object()
    state = {"n": 0}

    def condition():
        state["n"] += 1
        return sentinel if state["n"] >= 2 else None

    assert poll_until(condition, 5.0,
                      clock=clock, sleep=clock.sleep) is sentinel


def test_false_counts_as_satisfied_like_any_non_none_value():
    """``False`` is a legitimate answer for a boolean condition, and the
    contract is "not None" -- documented so a future caller is not surprised
    that a False result ends the wait."""
    clock = _FakeClock()
    assert poll_until(lambda: False, 5.0,
                      clock=clock, sleep=clock.sleep) is False


def test_real_clock_returns_promptly_for_a_slowly_appearing_condition():
    """Wall-clock check: a condition that flips ~30ms in must not cost the
    old 100ms quantum, and must not cost anywhere near the timeout."""
    started = time.perf_counter()

    def condition():
        if time.perf_counter() - started < 0.030:
            return None
        return "done"

    result = poll_until(condition, 5.0)
    elapsed = time.perf_counter() - started

    assert result == "done"
    assert elapsed < 0.25, f"waited {elapsed:.3f}s for a 30ms condition"
