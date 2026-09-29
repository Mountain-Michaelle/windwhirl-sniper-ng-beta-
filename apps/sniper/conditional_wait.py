"""Conditional (event-ish) polling for COORDINATION waits.

These are NOT page waits. Nothing in the browser has to settle here -- a
lock file has to be released, a claim sentinel has to be written. A fixed
``time.sleep(N)`` therefore only ever *adds* latency on top of the real
wait: the condition can become true one microsecond after the sleep starts
and the caller still sleeps the full N.

``poll_until`` closes that gap. It re-tests the condition on a short
backing-off schedule and returns the instant it holds, while keeping the
SAME total deadline the fixed sleep respected. Semantics are unchanged:
same condition, same deadline, same timeout value -- only the wake-up
granularity differs, so this can never turn a wait into a shorter timeout
than before.

This is deliberately NOT used for page waits. The upload flow's browser
waits already run through ``fast_waits``' in-page ``wait_for_function``
engine (one round trip, demand-driven). This module exists only for the
filesystem/lock coordination polls where there is no in-page engine to
use and where the condition is a plain Python predicate.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

# Backing-off poll schedule. The first re-test happens almost immediately
# (the overwhelmingly common case is a condition that is already true or
# becomes true within a millisecond), then intervals grow so a genuinely
# slow peer -- another worker still scraping the catalog -- is not hammered
# with a tight syscall loop. Total elapsed is still bounded by ``timeout_s``.
_POLL_SCHEDULE_S = (0.005, 0.010, 0.025, 0.050, 0.100, 0.200, 0.500, 1.000)


def poll_until(
    condition: Callable[[], Optional[T]],
    timeout_s: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[T]:
    """Wait for ``condition`` to return something other than ``None``.

    ``condition`` is evaluated immediately -- before any sleep at all -- so
    an already-satisfied condition costs one call and zero wait. Returns the
    first non-``None`` value, or ``None`` if ``timeout_s`` elapses first.

    The contract is "not None", not truthiness: ``False`` is a legitimate
    answer for a boolean condition and ends the wait just like any value.

    The deadline is authoritative: this never sleeps past it, so callers get
    the same "waited about as long as you asked" guarantee a fixed sleep
    gave them. Callers that want a distinct "timed out" signal should compare
    against their own recorded deadline, exactly as they did before.
    """
    deadline = clock() + timeout_s
    attempt = 0
    while True:
        value = condition()
        if value is not None:
            return value
        now = clock()
        if now >= deadline:
            return None
        interval = _POLL_SCHEDULE_S[min(attempt, len(_POLL_SCHEDULE_S) - 1)]
        attempt += 1
        # Never sleep past the deadline: a short remainder must not turn into
        # a full interval of extra waiting.
        sleep(min(interval, max(deadline - now, 0.0)))
