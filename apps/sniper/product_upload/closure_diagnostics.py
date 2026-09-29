"""
closure_diagnostics.py

Forensic capture for the Add Order automation: the "what actually
happened, and why" file. Nothing here drives the browser or decides
retry policy -- it only records evidence, on request, about the exact
moment something went wrong. It has zero dependency on dialog_controls.py,
field_values.py, or add_products.py, so it can be imported by any of
them without risk of a circular import; it is the lowest layer in the
split.

FIX (Sep 2026, "need proof of why it closed, not just that it closed"):
originally this held one function -- _capture_closure_evidence -- that
wrote a screenshot, a small .txt state dump, and (if tracing was
running) saved a Playwright trace chunk. That answers "did it close and
roughly when", but not "what selector/value was in flight, what options
were on screen a moment ago, what happened in the few steps right before
this". This version extends it in three small, deliberately bounded ways:

  1. `record_breadcrumb(cfg, message)` / `recent_breadcrumbs()` -- a
     tiny in-process ring buffer (last 25 entries) of one-line notes
     dialog_controls.py drops at each notable action (opening a
     combobox, clicking an option, advancing a wizard step, force-
     closing a dialog). `_capture_closure_evidence` includes the recent
     trail in every dump, so a closure report reads as a short story
     ("opened state combobox -> typed 'Lagos' -> clicked option ->
     [CLOSED HERE]") instead of a single isolated fact.

  2. Optional `selector` / `attempted_value` / `extra_context`
     parameters on `_capture_closure_evidence` -- callers that know
     exactly what they were trying to do (which selector, which value)
     now pass that through instead of it being reconstructed after the
     fact. `extra_context` is a free-form dict for anything caller-
     specific worth recording (e.g. the Amount field's current value
     during a price edit).

  3. Richer, automatic page-state capture -- every currently-visible
     combobox trigger's own text and data-state (not just the one
     involved), and, if a listbox happens to be open, the full list of
     currently-visible option texts. This is the difference between
     "the state combobox didn't show what we expected" and "the state
     combobox didn't show what we expected, AND here are the 4 other
     options it was rendering at the time, which is why."

Every write here is wrapped so a diagnostics failure can never take down
the automation run itself, and everything is written twice, in the same
data, deliberately redundant: a `.txt` for a human to skim, and a `.json`
with the same fields for grepping/scripting a fix. A `.png` screenshot
and (if tracing is active) a saved trace `.zip` round out the bundle.
"""

from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import apps.sniper.utils.field_selectors as sel

# FIX (Sep 2026): generic Radix-style overlay/backdrop selector. Matches
# the exact element shape seen intercepting clicks in the field logs: a
# fixed, full-screen, z-indexed div carrying a data-state attribute.
# Deliberately broad (class-based, not id-based) since this is a
# defensive best-effort guard, not a required element -- if nothing
# matches, every use of it is a no-op.
_OVERLAY_SELECTOR = 'div.fixed.inset-0[data-state]'

# FIX (Sep 2026): bounded action trail shared across the whole process.
# This automation drives exactly one page at a time (see
# run_automation.py), so a single module-level deque is sufficient --
# no per-page keying needed. Capped so it can never grow unbounded over
# a long --lock run.
_BREADCRUMB_MAXLEN = 25
_breadcrumbs: "deque[str]" = deque(maxlen=_BREADCRUMB_MAXLEN)


def record_breadcrumb(cfg, message: str) -> None:
    """Drop a one-line note of a notable action (opening a combobox,
    clicking an option, advancing a wizard step, force-closing a
    dialog). Called from dialog_controls.py at each such point. Never
    raises -- a breadcrumb failure must never affect the automation."""
    try:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _breadcrumbs.append(f"[{ts}] {message}")
    except Exception:
        pass


def recent_breadcrumbs() -> list[str]:
    """The last N breadcrumbs, oldest first -- the action trail leading
    up to whatever moment this is read at."""
    return list(_breadcrumbs)


def _safe_label(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:80]


def _emit(cfg, message: str) -> None:
    try:
        cfg.on_progress(message)
    except Exception:
        pass


def _bump(cfg, key: str, by: int = 1) -> None:
    """FIX (strategic speed upgrade -- "measure before declaring
    success"): increments a named counter on cfg.perf_counters. Used
    across dialog_controls.py and add_products.py to count things like
    immediate option finds vs. render-waits actually needed vs. selection
    retries vs. dialog recoveries vs. price-cache hits/misses -- see each
    call site for what a given key means. Deliberately just a dict
    increment (no timing, no I/O) so it costs nothing measurable on the
    hot path; run_automation.py's print_performance_summary() reads
    cfg.perf_counters at the end of a run to report the totals. Never
    raises -- a counter miss must never affect the automation itself."""
    try:
        cfg.perf_counters[key] = cfg.perf_counters.get(key, 0) + 1
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Trace-window rotation (only ever active while --enable-diagnostics tracing
# is running)
# ---------------------------------------------------------------------------
# WHY: Playwright buffers a trace CHUNK in memory until it is stopped. The
# diagnostic mode opens one chunk at startup and (before this) only stopped it
# when a closure was captured or the browser closed -- so a 200+ order debug
# run accumulated the snapshots/screenshots of the WHOLE run in RAM. Rotating
# flushes the current window to a bounded two-file ring every
# cfg.trace_chunk_rotate_orders attempts, so peak memory depends on the
# rotation interval, not on the run length.
#
# The two slots alternate (trace_window_0.zip / trace_window_1.zip), so the
# most recent window is always on disk alongside the one before it -- a crash
# that never reaches a closure capture still leaves the trace of the orders
# leading up to it. Both files live under cfg.diagnostics_dir (already a
# replaceable, git-ignored generated directory) and are bounded at two files
# by construction.
_TRACE_ROTATION: dict = {
    "active": False,   # set only when tracing really started
    "since": 0,        # order attempts observed since the last rotation
    "slot": 0,         # which of the two window files to write next
    "dir": None,       # where those two window files live
}


def enable_trace_rotation(diagnostics_dir: str) -> None:
    """Arm the rotator for a run whose tracing actually started. Called by
    run_upload.py once the AutomationConfig exists, so windows land in the
    SAME directory the closure evidence goes to. Never raises -- a rotation
    problem must never affect the automation."""
    _TRACE_ROTATION["active"] = True
    _TRACE_ROTATION["since"] = 0
    _TRACE_ROTATION["slot"] = 0
    _TRACE_ROTATION["dir"] = str(diagnostics_dir or "")


def maybe_rotate_trace_chunk(page, cfg, row_number: Optional[int] = None) -> None:
    """Called once per order attempt by run_upload.py's _attempt_order.

    A no-op unless (a) the rotator was armed, which only happens when
    Playwright tracing really is running, and (b) at least
    cfg.trace_chunk_rotate_orders attempts have passed since the last
    rotation. On a normal run (diagnostics off) this is one dict lookup per
    order and nothing else.

    Every existing guarantee is preserved: the closure capture in
    _capture_closure_evidence still stops and SAVES the chunk covering the
    closure and still re-arms a fresh one immediately afterwards. Worst case
    (a tracing call fails) the rotator goes quiet for the rest of the run and
    the run itself is completely unaffected.
    """
    if not _TRACE_ROTATION["active"]:
        return
    try:
        every = int(getattr(cfg, "trace_chunk_rotate_orders", 0) or 0)
    except Exception:
        return
    if every <= 0:
        return
    _TRACE_ROTATION["since"] += 1
    if _TRACE_ROTATION["since"] < every:
        return
    _TRACE_ROTATION["since"] = 0

    slot = _TRACE_ROTATION["slot"]
    _TRACE_ROTATION["slot"] = 1 - slot
    target_dir = _TRACE_ROTATION["dir"]
    path = None
    try:
        if target_dir:
            Path(target_dir).mkdir(parents=True, exist_ok=True)
            path = os.path.join(target_dir,
                                "trace_window_%d_pid%d.zip" % (slot, os.getpid()))
        if path:
            page.context.tracing.stop_chunk(path=path)
        else:
            page.context.tracing.stop_chunk(discard=True)
    except Exception:
        _emit(cfg, "[diagnostics] trace window rotation could not flush its "
                   "window (continuing without rotation evidence).")
    try:
        page.context.tracing.start_chunk(name="window_after_row%s" % row_number)
    except Exception:
        # No fresh chunk means tracing is effectively over for this run; stop
        # trying so we never pay for a losing tracing call on every order.
        _TRACE_ROTATION["active"] = False
        return
    if path:
        _emit(cfg, "[diagnostics] trace window rotated at row %s -> %s "
                   "(bounded to %d order(s) per window)."
                   % (row_number, path, every))


def _describe_visible_combos(page, max_combos: int = 12) -> list[dict]:
    """Best-effort snapshot of every combobox trigger currently on the
    page: its own displayed text and data-state. Included in every
    closure dump so "what did the OTHER fields look like at this
    moment" doesn't require a second repro run to find out."""
    try:
        raw = page.locator('button[role="combobox"]').evaluate_all(
            "els => els.slice(0, %d).map(e => ({"
            "text: (e.textContent || '').trim().slice(0, 120), "
            "data_state: e.getAttribute('data-state'), "
            "aria_expanded: e.getAttribute('aria-expanded')"
            "}))" % max_combos
        )
        return raw
    except Exception:
        return []


def _describe_visible_options(page, max_options: int = 30) -> list[str]:
    """If a listbox happens to be open at capture time, the option
    texts it was showing -- this is what turns "no option matched" into
    something a human can actually diagnose without reproducing it."""
    try:
        listbox = page.locator(sel.LISTBOX_SELECTOR).first
        if listbox.count() == 0 or not listbox.is_visible():
            return []
        return page.locator(sel.OPTION_SELECTOR).evaluate_all(
            "els => els.slice(0, %d).map(e => (e.textContent || '').trim())" % max_options
        )
    except Exception:
        return []


def _capture_closure_evidence(page, cfg, reason: str,
                               row_number: Optional[int], step_label: str,
                               selector: Optional[str] = None,
                               attempted_value: Optional[str] = None,
                               extra_context: Optional[dict] = None) -> None:
    """Called the INSTANT a dialog closure is detected -- from
    dialog_controls.py's _require_dialog_open and from every
    _DialogClosed raise site -- so what gets captured reflects the page
    at the moment of the event, not several recovery steps later.

    Writes, best-effort, under cfg.diagnostics_dir:
      - a full-page screenshot (.png)
      - a human-readable state dump (.txt)
      - the SAME data as structured JSON (.json), for grepping/scripting
      - if a Playwright trace is running (context.tracing.start() +
        start_chunk(), set up once in run_automation.py), that chunk is
        stopped and SAVED as a .zip -- open it with
        `playwright show-trace <file>` for the actual click, DOM
        snapshots before/after, console and network -- then a fresh
        chunk is started immediately so later steps/orders keep being
        covered. Silently skipped if tracing was never started.

    `selector` / `attempted_value` / `extra_context` let a caller that
    knows exactly what it was doing (which field, which value) record
    that directly instead of it being reconstructed after the fact.

    Every individual step here is wrapped so a diagnostics failure can
    never take down the automation run itself.
    """
    if not getattr(cfg, "enable_closure_diagnostics", True):
        _emit(cfg, f"[closure] row {row_number}: dialog closed at step "
                   f"{step_label!r} -- {reason} (diagnostics disabled)")
        return

    diagnostics_dir = getattr(cfg, "diagnostics_dir", "recordings/closure_diagnostics")
    try:
        os.makedirs(diagnostics_dir, exist_ok=True)
    except Exception:
        _emit(cfg, f"[closure] row {row_number}: dialog closed at step "
                   f"{step_label!r} -- {reason} (could not create diagnostics dir)")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    prefix = os.path.join(diagnostics_dir, f"row{row_number}_{ts}_{_safe_label(step_label)}")

    try:
        page.screenshot(path=f"{prefix}.png", full_page=True)
    except Exception:
        pass

    info: dict = {
        "time": ts,
        "row_number": row_number,
        "step": step_label,
        "reason": reason,
        "selector": selector,
        "attempted_value": attempted_value,
    }
    try:
        info["url"] = page.url
    except Exception:
        info["url"] = None
    try:
        dlg = page.locator(sel.DIALOG_SELECTOR).first
        info["dialog_present"] = dlg.count() > 0
        info["dialog_data_state"] = dlg.get_attribute("data-state") if dlg.count() > 0 else None
    except Exception:
        pass
    try:
        overlay = page.locator(_OVERLAY_SELECTOR).first
        info["overlay_present"] = overlay.count() > 0
        info["overlay_data_state"] = overlay.get_attribute("data-state") if overlay.count() > 0 else None
    except Exception:
        pass
    try:
        listbox = page.locator(sel.LISTBOX_SELECTOR).first
        info["listbox_visible"] = listbox.is_visible() if listbox.count() > 0 else False
    except Exception:
        pass
    try:
        info["active_element"] = page.evaluate(
            "() => document.activeElement ? document.activeElement.outerHTML.slice(0, 400) : null"
        )
    except Exception:
        pass

    # FIX (Sep 2026): the three enrichments this version adds -- what
    # every other combobox on the page currently shows, what options a
    # still-open listbox was offering, and the action trail leading up
    # to this moment.
    info["visible_comboboxes"] = _describe_visible_combos(page)
    info["visible_options"] = _describe_visible_options(page)
    info["recent_actions"] = recent_breadcrumbs()

    if extra_context:
        try:
            info["extra_context"] = dict(extra_context)
        except Exception:
            info["extra_context"] = {"unserializable": str(extra_context)}

    try:
        with open(f"{prefix}.json", "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, default=str)
    except Exception:
        pass

    try:
        txt_lines = [f"{k}: {v}" for k, v in info.items()
                     if k not in ("visible_comboboxes", "visible_options", "recent_actions")]
        txt_lines.append("")
        txt_lines.append("visible_comboboxes:")
        for c in info["visible_comboboxes"]:
            txt_lines.append(f"  - {c}")
        txt_lines.append("visible_options (listbox open at capture time):")
        for o in info["visible_options"]:
            txt_lines.append(f"  - {o!r}")
        txt_lines.append("recent_actions (oldest first):")
        for a in info["recent_actions"]:
            txt_lines.append(f"  - {a}")
        with open(f"{prefix}.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines) + "\n")
    except Exception:
        pass

    trace_note = ""
    try:
        trace_path = f"{prefix}_trace.zip"
        page.context.tracing.stop_chunk(path=trace_path)
        page.context.tracing.start_chunk(
            name=f"row{row_number}_after_{_safe_label(step_label)}"
        )
        trace_note = f" / trace: {trace_path}"
    except Exception:
        pass

    _emit(cfg, f"[closure] row {row_number}: dialog closed at step {step_label!r} "
               f"-- {reason} -- evidence: {prefix}.png / .txt / .json{trace_note}")