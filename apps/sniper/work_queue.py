"""Atomic, crash-tolerant claiming of order rows for parallel workers.

``--workers N`` splits one client job across N browser processes. Each order
must be processed by exactly one worker, and the split has to stay fair even
though rows take very different amounts of time: a fixed round-robin slice
leaves a worker idle whenever its slice happens to hold the slow rows.

Workers instead *claim* rows one at a time and a worker that finishes early
keeps taking the next unclaimed row instead of stopping at the end of a
pre-assigned slice.

How a claim works
-----------------
The claim directory holds one sentinel file per claimed row
(``row-<index>.claim``). Claiming index *i* is a single
``os.open(..., O_CREAT | O_EXCL | O_WRONLY)``, which is atomic on both POSIX
and Windows: exactly one worker in the whole machine can create that file, and
that worker owns the row. No lock file is needed, so there is no lock to wedge,
no stale-owner recovery, and no cross-process or cross-thread interference.
Each worker remembers its scan position, so the cost of finding the next free
index is amortised O(1) per row.

Scheduling: reserved bands plus a steal delay
---------------------------------------------
Scanning the file in plain ascending order hands the whole file to whichever
worker reaches the loop first, and that is not a theoretical race -- a worker
that reuses a cached session reaches the claim loop well before a sibling that
has to log in from scratch, and it would then upload almost every order on its
own while the others sat idle.

Each worker therefore owns a *reserved band*: the indices congruent to its
number modulo the worker count. A band gives an even split no matter how the
workers interleave, and it is what stops that first-starting worker from
monopolising the file.

Bands alone reintroduce the original problem in its worst form, though: a fixed
slice leaves a worker idle when a peer holds the slow rows, which is exactly
what a dynamic queue is meant to avoid. So once a worker finishes its own band
it eventually starts claiming rows outside it too.

The delay before that happens is the part that matters. An unclaimed row inside
a peer's band looks identical whether the peer is dead, or merely has not got
to it yet, so a worker that steals immediately walks straight into its peer's
untouched band and takes it. A worker therefore waits ``steal_grace_s`` after
exhausting its own band before it touches other workers' rows -- long enough
for live peers to reach their bands, short enough that a genuinely dead peer's
rows are still recovered in-run rather than waiting for the next run.

Crash behaviour
---------------
A row claimed but not yet finished stays claimed. The worker that claimed it
remembers it in memory, so a browser/network crash inside ``--lock`` mode
resumes the *same* row when the session restarts. If the worker process itself
dies, its rows are picked up by whichever surviving worker clears the steal
delay; failing that they are still not marked done in the progress DB, and the
parent deletes every claim sentinel at the start of the next run (``reset()``),
so the next run simply reprocesses them.
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from typing import Callable, Optional

DEFAULT_STEAL_GRACE_S = 20.0


class OrderClaimQueue:
    """Shared row cursor that each worker consumes as it finishes rows.

    ``claim_next(total)`` returns the next owned index, or ``None`` when this
    worker has nothing to take *right now* -- either because every index below
    ``total`` is claimed, or because its own band is spent and the steal delay
    has not elapsed yet. The caller tells the two apart with
    ``unclaimed_in()`` and simply asks again after a short pause.

    ``mark_complete(index)`` confirms the row finished so the next claim moves
    on; if the session dies first, the same index is re-offered on the next
    ``claim_next`` call (same queue object, i.e. the same worker process).

    ``shard=(index, count)`` reserves the indices congruent to ``index`` modulo
    ``count`` so the work is split evenly no matter which worker reaches the
    loop first, and only a worker whose own band is spent will take another
    worker's rows.

    ``steal_grace_s`` is how long that wait lasts: long enough for a live peer
    to reach its own rows, short enough that a dead peer's rows are still
    recovered within the same run. ``clock`` is injectable so the tests can
    drive the delay without sleeping.
    """

    def __init__(
        self,
        claim_dir: Path,
        shard: tuple[int, int] = (0, 1),
        *,
        steal_grace_s: float = DEFAULT_STEAL_GRACE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.claim_dir = Path(claim_dir)
        self.shard = (max(0, int(shard[0])), max(1, int(shard[1])))
        self.steal_grace_s = max(0.0, float(steal_grace_s))
        self._clock = clock
        self._stride_next: Optional[int] = None
        self._steal_next = 0
        self._in_flight: Optional[int] = None
        self._band_spent_at: Optional[float] = None
        self._dir_ready = False

    @property
    def in_flight(self) -> Optional[int]:
        """The claimed-but-unconfirmed row, if any (survives session restarts)."""
        return self._in_flight

    def reset(self) -> None:
        """Start a fresh run: drop every sentinel and restart the scan at 0."""
        self.claim_dir.mkdir(parents=True, exist_ok=True)
        self._dir_ready = True
        for sentinel in self.claim_dir.glob("row-*.claim"):
            try:
                sentinel.unlink()
            except OSError:
                pass
        self._stride_next = None
        self._steal_next = 0
        self._in_flight = None
        self._band_spent_at = None

    def claim_next(self, total: int) -> Optional[int]:
        """Return the next row index this worker should process, or ``None``.

        ``None`` means "nothing right now", not "finished": check
        ``unclaimed_in()`` to tell a finished run from one that is merely
        waiting out the steal delay.
        """
        if self._in_flight is not None:
            # A previous session died holding this row -- finish it first.
            return self._in_flight
        if not self._dir_ready:
            self.claim_dir.mkdir(parents=True, exist_ok=True)
            self._dir_ready = True

        index = self._claim_reserved(total)
        if index is not None:
            self._in_flight = index
            return index

        # Own band is spent. Only reach for another worker's rows once the
        # steal delay has passed, so a live peer gets to its own band first --
        # stealing instantly would hand this worker the peer's untouched rows
        # and recreate the imbalance the bands exist to prevent.
        if self._band_spent_at is None:
            self._band_spent_at = self._clock()
        if self._clock() - self._band_spent_at < self.steal_grace_s:
            return None

        index = self._claim_any_free(total)
        if index is None:
            return None
        self._in_flight = index
        return index

    def mark_complete(self, index: int) -> None:
        """Confirm ``index`` finished so the next claim scans past it."""
        if self._in_flight == index:
            self._in_flight = None

    def seconds_until_stealable(self) -> float:
        """How much longer this worker must wait before it may steal a
        peer's rows; 0.0 once the grace period has elapsed (or if the band
        was never spent).

        Exposed so the caller's idle wait can be sized to the ACTUAL
        remaining grace instead of a flat 1s tick -- the same ``self._clock``
        the decision in ``claim_next`` uses, so the two can never disagree
        about whether the delay is up.
        """
        if self._band_spent_at is None:
            return 0.0
        return max(0.0, self.steal_grace_s - (self._clock() - self._band_spent_at))

    def unclaimed_in(self, positions) -> int:
        """Count the rows in ``positions`` that no worker has claimed yet."""
        return sum(1 for index in positions if not self._is_claimed(index))

    def _claim_reserved(self, total: int) -> Optional[int]:
        """Claim the next index belonging to this worker's reserved band."""
        count = self.shard[1]
        if self._stride_next is None:
            self._stride_next = self.shard[0] % count
        while self._stride_next < total:
            position = self._stride_next
            # Advance the cursor before the attempt so a lost race (or a
            # crash) cannot make this worker revisit the same index forever.
            self._stride_next = position + count
            if self._try_claim(position):
                return position
        self._stride_next = None
        return None

    def _claim_any_free(self, total: int) -> Optional[int]:
        """Claim any index no worker has taken yet (the work-stealing pass)."""
        while self._steal_next < total:
            position = self._steal_next
            self._steal_next = position + 1
            if self._try_claim(position):
                return position
        return None

    def _is_claimed(self, index: int) -> bool:
        return (self.claim_dir / f"row-{index}.claim").exists()

    def _try_claim(self, index: int) -> bool:
        path = self.claim_dir / f"row-{index}.claim"
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST or isinstance(exc, FileExistsError):
                return False
            raise
        os.close(fd)
        return True


__all__ = ["OrderClaimQueue", "DEFAULT_STEAL_GRACE_S"]
