"""
wait_profile.py

PERF (Phase 0 measurement pass -- "migrate every static wait to a dynamic
wait, but prove it first"): process-wide accounting of how much wall time
each wait primitive actually costs, so the "static vs dynamic wait" question
is answered by DATA on this machine and this CRM rather than by reading code.

WHY THIS EXISTS
Every wait in the upload path already goes through one of two doors:
  - fast_waits.wait_*  -- the in-browser (page.wait_for_function) engine,
    which is fully dynamic and demand-driven, or
  - a _py_* / inline Python polling loop -- the faithful fallback that only
    runs when the in-browser path could not be used or errored.
Both are "waiting", but they cost wildly different amounts, and the
fallback in particular is a hidden per-iteration Playwright round trip. This
module makes that cost visible:

    [wait-profile] waits: 3.10s across 214 call(s); order time 12.40s
                  -> 25.0% of it
    [wait-profile]   wait_value_equals      1200.0ms  n=40  avg=30.0ms
    [wait-profile] VERDICT (25.0%): material -- ...

COST / SAFETY CONTRACT (same rules closure_diagnostics._bump follows)
  - Never raises. A profiling miss must never affect the automation.
  - Cheap: two dict updates and a perf_counter pair per wait call, against
    waits that cost milliseconds. OFF BY DEFAULT (Sep 2026): a normal run
    must not pay for numbers nobody reads, so accounting is opt-in -- either
    WW_WAIT_PROFILE=1, or --enable-diagnostics on run_upload.py, which turns
    this on together with Playwright tracing so ONE flag describes a debug
    run. WW_WAIT_PROFILE=0 forces it off even then.
  - Per-PROCESS, not global: each worker/browser subprocess is its own
    process, so this needs no locking and cannot cross-contaminate clients.
  - "fallback.*" kinds are a SUBSET of their public kind's time (the
    fallback runs inside the public call), so the report labels them as such
    and never adds them to the headline total.
"""

from __future__ import annotations

import functools
import os
import time
from typing import Optional

ENV_FLAG = "WW_WAIT_PROFILE"

# Thresholds for the verdict line, as a share of measured order time.
_NOT_BOTTLENECK_PCT = 10.0
_DOMINANT_PCT = 30.0

_TOP_KINDS = 12


class _Span:
    """Minimal __enter__/__exit__ timer (cheaper than
    contextlib.contextmanager, and __exit__ runs even when the wrapped code
    raises, so the timing is recorded on every path)."""

    __slots__ = ("kind", "start")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.start = 0.0

    def __enter__(self) -> "_Span":
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        add(self.kind, (time.perf_counter() - self.start) * 1000.0)
        return False  # never swallow the exception


class WaitProfile:
    """Accumulates per-kind wait milliseconds and call counts."""

    def __init__(self, enabled: Optional[bool] = None) -> None:
        """OFF unless something explicitly asks for it.

        `enabled=None` (the default) reads WW_WAIT_PROFILE, whose own default
        is now "0" -- see the module docstring. Passing `enabled=` explicitly
        overrides the environment completely, which is what every test does so
        that its intent never depends on an ambient variable.
        """
        if enabled is None:
            enabled = os.environ.get(ENV_FLAG, "0").strip().lower() not in (
                "", "0", "false", "no", "off",
            )
        self.enabled = bool(enabled)
        self._ms: dict[str, float] = {}
        self._n: dict[str, int] = {}
        self._total_ms: float = 0.0
        self._calls: int = 0

    # -- recording ---------------------------------------------------------
    def add(self, kind: str, ms: float) -> None:
        if not self.enabled:
            return
        try:
            self._ms[kind] = self._ms.get(kind, 0.0) + ms
            self._n[kind] = self._n.get(kind, 0) + 1
            self._total_ms += ms
            self._calls += 1
        except Exception:
            pass

    def bump(self, kind: str, by: int = 1) -> None:
        """Count an event with no duration of its own (e.g. a fallback taken).
        Costs nothing measurable and never raises."""
        if not self.enabled:
            return
        try:
            self._n[kind] = self._n.get(kind, 0) + by
        except Exception:
            pass

    def span(self, kind: str) -> _Span:
        return _Span(kind)

    # -- lifecycle ---------------------------------------------------------
    def reset(self) -> None:
        self._ms = {}
        self._n = {}
        self._total_ms = 0.0
        self._calls = 0

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "total_ms": round(self._total_ms, 1),
            "calls": self._calls,
            "kinds_ms": {k: round(v, 1) for k, v in self._ms.items()},
            "counts": dict(self._n),
        }

    # -- reporting ---------------------------------------------------------
    def render_lines(self, order_ms: float = 0.0) -> list[str]:
        if not self.enabled or not self._ms:
            return []
        lines: list[str] = []
        pct = (self._total_ms / order_ms * 100.0) if order_ms > 0 else 0.0
        head = (f"[wait-profile] waits: {self._total_ms / 1000.0:.2f}s across "
                f"{self._calls} call(s)")
        if order_ms > 0:
            head += (f"; measured order time {order_ms / 1000.0:.2f}s"
                     f" -> {pct:.1f}% of it")
        lines.append(head)
        for kind, ms in sorted(self._ms.items(), key=lambda kv: -kv[1])[:_TOP_KINDS]:
            n = self._n.get(kind, 0)
            avg = (ms / n) if n else 0.0
            lines.append(f"[wait-profile]   {kind:<32} {ms:9.1f}ms"
                         f"  n={n:<5} avg={avg:7.1f}ms")
        if order_ms > 0:
            if pct < _NOT_BOTTLENECK_PCT:
                verdict = ("NOT the bottleneck -- leave the waits alone; further "
                           "wait tuning cannot pay for itself. Spend the effort on "
                           "concurrency/throughput instead.")
            elif pct < _DOMINANT_PCT:
                verdict = ("material -- only the kinds above justify a Phase-2 "
                           "targeted change; tune one at a time, canary each.")
            else:
                verdict = ("DOMINANT -- Phase 2 is justified; migrate the top "
                           "kinds first, one change per commit, canary each.")
            lines.append(f"[wait-profile] VERDICT ({pct:.1f}%): {verdict}")
        return lines


PROFILE = WaitProfile()


def add(kind: str, ms: float) -> None:
    PROFILE.add(kind, ms)


def bump(kind: str, by: int = 1) -> None:
    PROFILE.bump(kind, by)


def reset() -> None:
    PROFILE.reset()


def render_lines(order_ms: float = 0.0) -> list[str]:
    return PROFILE.render_lines(order_ms)


def profiled(kind: str):
    """Decorator: time a wait helper and attribute the cost to `kind`.
    functools.wraps preserves __name__/__doc__ and keeps inspect.signature
    working through __wrapped__, so this stays invisible to callers and to
    anything that patches these functions by name."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not PROFILE.enabled:
                return fn(*args, **kwargs)
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                PROFILE.add(kind, (time.perf_counter() - start) * 1000.0)
        return wrapper
    return deco
