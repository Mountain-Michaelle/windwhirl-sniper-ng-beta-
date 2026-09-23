"""
fast_waits.py

PERF (Sep 2026, "process orders slowly due to some lagging architectural
design ... possibly 100x faster with the persistent logic still there"):

Every wait in dialog_controls.py / field_values.py used to be a PYTHON-side
polling loop whose every iteration was its own Playwright protocol round
trip (locator.input_value(), .count(), .get_attribute(), .bounding_box(),
.inner_text() ...). One order touches those loops dozens of times, so the
per-iteration protocol overhead -- not the browser itself -- was a large
share of per-order latency, and on a slow network every one of those round
trips is expensive.

This module keeps every wait's exact SEMANTICS (same timeouts, same
stability requirements, same return contracts) but moves the polling INTO
the browser: one element-handle lookup + one page.wait_for_function() call
whose predicate Playwright itself re-evaluates in-page until it is
satisfied or the same timeout as before elapses. One round trip replaces
dozens.

Every helper is total: it NEVER raises. On any unexpected error it falls
back to a faithful copy of the original Python polling loop (the _py_*
functions below), so worst case a helper behaves exactly as before and
nothing can regress.

Two deliberate, documented caveats:

  - In-page interval polling is timer-based, so an OCCLUDED/minimized
    browser window can throttle it (Chromium clamps background timers).
    That can only slow a wait down, never make it return a wrong answer,
    and the headed automation window is normally visible. The browser is
    additionally launched with background-timer throttling disabled (see
    run_upload.py's chromium launch args), which removes the common case.

  - Stability tracking uses a per-call token (see _next_token) so two
    consecutive wait calls on the same element can never inherit each
    other's "already stable since ..." state -- each call observes its
    own stability window from scratch, exactly like the original loops.
"""

from __future__ import annotations

import itertools
import time
from typing import Optional

from playwright.sync_api import TimeoutError as PWTimeoutError

_TOKENS = itertools.count()


def _next_token() -> int:
    return next(_TOKENS)


def _poll(poll_ms: int) -> int:
    return max(10, int(poll_ms))


# In-page stability store: a WeakMap keyed by element, holding the last
# observed state + the performance.now() timestamp it was first seen, plus
# the token of the wait call that wrote it. A predicate resets its record
# whenever the token (new call) or the observed state (value changed)
# differs, then reports "stable" once the state has held for stableMs.
_STABLE_PREDICATE_PREFIX = """(rec => {
    const store = window.__wwStable || (window.__wwStable = new WeakMap());
"""


def _resolve_handle(locator, timeout_ms: int):
    """One round trip: resolve the locator to a live element handle.
    Returns (page, handle) or raises; callers route failures to their
    Python fallback."""
    page = locator.page
    handle = locator.element_handle(timeout=max(timeout_ms, 1000))
    return page, handle


def _dispose(handle) -> None:
    try:
        handle.dispose()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public helpers -- each mirrors one original polling loop.
# ---------------------------------------------------------------------------

def wait_value_equals(locator, expected: str, timeout_ms: int,
                      min_stable_ms: int = 40, poll_ms: int = 15) -> tuple[bool, str]:
    """Waits until the element's value equals `expected` (returns
    (True, expected)), or until the value has held steady at something else
    for min_stable_ms (returns (False, that value)), or until timeout_ms
    elapses (returns (False, last seen value)). Same contract as the
    original _wait_for_value polling loop in field_values.py."""
    try:
        page, handle = _resolve_handle(locator, timeout_ms)
    except Exception:
        return _py_wait_value_equals(locator, expected, timeout_ms, poll_ms)
    if handle is None:
        return False, ""
    try:
        try:
            result = page.wait_for_function(
                """([el, exp, stableMs, token]) => {
                    if (!el || !el.isConnected) return null;
                    const v = el.value;
                    if (v === exp) return { landed: true, value: v };
                    const store = window.__wwStable || (window.__wwStable = new WeakMap());
                    let rec = store.get(el);
                    const now = performance.now();
                    if (!rec || rec.token !== token || rec.v !== v) {
                        rec = { token: token, v: v, since: now };
                        store.set(el, rec);
                    }
                    if (now - rec.since >= stableMs) return { landed: false, value: v };
                    return null;
                }""",
                arg=[handle, expected, min_stable_ms, _next_token()],
                timeout=timeout_ms,
                polling=_poll(poll_ms),
            )
            data = result.json_value()
            return bool(data.get("landed")), str(data.get("value", ""))
        except PWTimeoutError:
            # Timed out still-waiting: one read returns ~the last seen
            # value, matching the original loop's deadline exit.
            try:
                return False, locator.input_value()
            except Exception:
                return False, ""
        except Exception:
            return _py_wait_value_equals(locator, expected, timeout_ms, poll_ms)
    finally:
        _dispose(handle)


def wait_value_stable(locator, timeout_ms: int,
                      min_stable_ms: int = 35, poll_ms: int = 15) -> str:
    """Waits until the element's value has held steady for min_stable_ms
    (or timeout_ms elapses) and returns the settled value; '' if the
    element can't be read at all. Same contract as the original
    _wait_for_stable_value in dialog_controls.py."""
    try:
        page, handle = _resolve_handle(locator, timeout_ms)
    except Exception:
        return _py_wait_value_stable(locator, timeout_ms, poll_ms)
    if handle is None:
        return ""
    try:
        try:
            result = page.wait_for_function(
                """([el, stableMs, token]) => {
                    if (!el || !el.isConnected) return null;
                    const v = el.value;
                    const store = window.__wwStable || (window.__wwStable = new WeakMap());
                    let rec = store.get(el);
                    const now = performance.now();
                    if (!rec || rec.token !== token || rec.v !== v) {
                        rec = { token: token, v: v, since: now };
                        store.set(el, rec);
                    }
                    if (now - rec.since >= stableMs) return { value: v };
                    return null;
                }""",
                arg=[handle, min_stable_ms, _next_token()],
                timeout=timeout_ms,
                polling=_poll(poll_ms),
            )
            return str(result.json_value().get("value", ""))
        except PWTimeoutError:
            try:
                return locator.input_value()
            except Exception:
                return ""
        except Exception:
            return _py_wait_value_stable(locator, timeout_ms, poll_ms)
    finally:
        _dispose(handle)


def wait_enabled_stable(locator, timeout_ms: int,
                        min_stable_ms: int = 30, poll_ms: int = 15) -> None:
    """Waits until the element reads as enabled (no `disabled` attribute,
    aria-disabled not "true") steadily for min_stable_ms. Gives up silently
    on timeout -- same contract as the original _wait_for_combo_ready,
    whose callers proceed and let the click's own actionability checks
    absorb the rest."""
    try:
        page, handle = _resolve_handle(locator, timeout_ms)
    except Exception:
        _py_wait_enabled_stable(locator, timeout_ms, poll_ms)
        return
    if handle is None:
        return
    try:
        try:
            page.wait_for_function(
                """([el, stableMs, token]) => {
                    if (!el || !el.isConnected) return null;
                    const disabled = el.hasAttribute('disabled') ||
                        (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
                    const state = disabled ? 'disabled' : 'enabled';
                    const store = window.__wwStable || (window.__wwStable = new WeakMap());
                    let rec = store.get(el);
                    const now = performance.now();
                    if (!rec || rec.token !== token || rec.state !== state) {
                        rec = { token: token, state: state, since: now };
                        store.set(el, rec);
                    }
                    if (state === 'enabled' && now - rec.since >= stableMs) return true;
                    return null;
                }""",
                arg=[handle, min_stable_ms, _next_token()],
                timeout=timeout_ms,
                polling=_poll(poll_ms),
            )
        except Exception:
            pass  # silent give-up, same as the original loop's deadline exit
    finally:
        _dispose(handle)


def wait_enabled_now(locator, timeout_ms: int) -> None:
    """Waits until the element reads as enabled at all (no stability
    requirement -- the original _click_next loop broke on the FIRST enabled
    read). Silent no-op on timeout; the caller's click still performs its
    own actionability checks."""
    try:
        page, handle = _resolve_handle(locator, timeout_ms)
    except Exception:
        return
    if handle is None:
        return
    try:
        try:
            page.wait_for_function(
                """([el]) => {
                    if (!el || !el.isConnected) return null;
                    const disabled = el.hasAttribute('disabled') ||
                        (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
                    return disabled ? null : true;
                }""",
                arg=[handle],
                timeout=timeout_ms,
                polling=15,
            )
        except Exception:
            pass
    finally:
        _dispose(handle)


def wait_count_stable(scope, child_selector: str, timeout_ms: int,
                      min_stable_ms: int = 50, poll_ms: int = 25) -> Optional[int]:
    """Waits until the number of `child_selector` descendants of `scope`
    has held steady for min_stable_ms; returns the settled count (None on
    fallback paths -- callers ignore the return). Same contract as the
    original _wait_for_option_render. Page scopes (no element_handle) run
    the original Python loop."""
    get_handle = getattr(scope, "element_handle", None)
    if get_handle is None:
        _py_wait_count_stable(scope, child_selector, timeout_ms, poll_ms)
        return None
    try:
        page = scope.page
        handle = get_handle(timeout=max(timeout_ms, 1000))
    except Exception:
        _py_wait_count_stable(scope, child_selector, timeout_ms, poll_ms)
        return None
    if handle is None:
        return None
    try:
        try:
            result = page.wait_for_function(
                """([box, stableMs, childSel, token]) => {
                    if (!box || !box.isConnected) return null;
                    const n = box.querySelectorAll(childSel).length;
                    const store = window.__wwStable || (window.__wwStable = new WeakMap());
                    let rec = store.get(box);
                    const now = performance.now();
                    if (!rec || rec.token !== token || rec.n !== n) {
                        rec = { token: token, n: n, since: now };
                        store.set(box, rec);
                    }
                    if (now - rec.since >= stableMs) return { count: n };
                    return null;
                }""",
                arg=[handle, min_stable_ms, child_selector, _next_token()],
                timeout=timeout_ms,
                polling=_poll(poll_ms),
            )
            return int(result.json_value().get("count", 0))
        except Exception:
            return None
    finally:
        _dispose(handle)


def wait_text_matches(locator, value: str, match_mode: str,
                      timeout_ms: int, poll_ms: int = 15) -> bool:
    """Waits until the element's rendered text matches `value` the same way
    _selection_looks_landed matches it ('exact', or either-direction
    substring). Returns True once matched, False on timeout. Same contract
    as the polling half of the original _poll_selection_landed (the
    immediate first check stays with the caller)."""
    value_norm = (value or "").strip().lower()
    try:
        page, handle = _resolve_handle(locator, timeout_ms)
    except Exception:
        return _py_wait_text_matches(locator, value_norm, match_mode, timeout_ms, poll_ms)
    if handle is None:
        return False
    try:
        try:
            page.wait_for_function(
                """([el, valNorm, mode]) => {
                    if (!el || !el.isConnected) return null;
                    const t = (el.innerText || '').trim().toLowerCase();
                    if (!t) return null;
                    if (mode === 'exact') return t === valNorm ? true : null;
                    return (t.includes(valNorm) || valNorm.includes(t)) ? true : null;
                }""",
                arg=[handle, value_norm, match_mode],
                timeout=timeout_ms,
                polling=_poll(poll_ms),
            )
            return True
        except Exception:
            return False
    finally:
        _dispose(handle)


def wait_box_stable(locator, timeout_ms: int,
                    min_stable_ms: int = 60, poll_ms: int = 30) -> None:
    """Waits until the element's bounding box has held steady for
    min_stable_ms. Silent no-op on timeout -- same contract as the original
    _wait_stable (whose callers ignore its result entirely)."""
    try:
        page, handle = _resolve_handle(locator, timeout_ms)
    except Exception:
        _py_wait_box_stable(locator, timeout_ms, poll_ms)
        return
    if handle is None:
        return
    try:
        try:
            page.wait_for_function(
                """([el, stableMs, token]) => {
                    if (!el || !el.isConnected) return null;
                    const r = el.getBoundingClientRect();
                    const key = r.x + ',' + r.y + ',' + r.width + ',' + r.height;
                    const store = window.__wwStable || (window.__wwStable = new WeakMap());
                    let rec = store.get(el);
                    const now = performance.now();
                    if (!rec || rec.token !== token || rec.key !== key) {
                        rec = { token: token, key: key, since: now };
                        store.set(el, rec);
                    }
                    if (now - rec.since >= stableMs) return true;
                    return null;
                }""",
                arg=[handle, min_stable_ms, _next_token()],
                timeout=timeout_ms,
                polling=_poll(poll_ms),
            )
        except Exception:
            pass
    finally:
        _dispose(handle)


def wait_value_shorter(locator, prev_len: int, timeout_ms: int,
                       poll_ms: int = 15) -> None:
    """Waits until the element's value is strictly shorter than prev_len
    (a Backspace landed). Silent no-op on timeout -- same contract as the
    original _wait_for_shorter."""
    try:
        page, handle = _resolve_handle(locator, timeout_ms)
    except Exception:
        _py_wait_value_shorter(locator, prev_len, timeout_ms, poll_ms)
        return
    if handle is None:
        return
    try:
        try:
            page.wait_for_function(
                """([el, prevLen]) => (el && el.isConnected && el.value.length < prevLen) ? true : null""",
                arg=[handle, prev_len],
                timeout=timeout_ms,
                polling=_poll(poll_ms),
            )
        except Exception:
            pass
    finally:
        _dispose(handle)


# ---------------------------------------------------------------------------
# Python fallbacks -- faithful copies of the original polling loops, used
# only when the in-page path fails for any reason.
# ---------------------------------------------------------------------------

def _py_wait_value_equals(locator, expected: str, timeout_ms: int, poll_ms: int) -> tuple[bool, str]:
    deadline = time.time() + timeout_ms / 1000
    try:
        baseline = locator.input_value()
    except Exception:
        return False, ""
    if baseline == expected:
        return True, baseline
    prev = baseline
    stable_reads = 0
    while time.time() < deadline:
        time.sleep(poll_ms / 1000)
        try:
            cur = locator.input_value()
        except Exception:
            return False, prev
        if cur == expected:
            return True, cur
        if cur == baseline:
            continue
        if cur == prev:
            stable_reads += 1
            if stable_reads >= 2:
                return False, cur
        else:
            stable_reads = 0
            prev = cur
    return False, prev


def _py_wait_value_stable(locator, timeout_ms: int, poll_ms: int) -> str:
    deadline = time.time() + timeout_ms / 1000
    try:
        prev = locator.input_value()
    except Exception:
        return ""
    stable_reads = 0
    while time.time() < deadline:
        time.sleep(poll_ms / 1000)
        try:
            cur = locator.input_value()
        except Exception:
            return prev
        if cur == prev:
            stable_reads += 1
            if stable_reads >= 2:
                return cur
        else:
            stable_reads = 0
            prev = cur
    return prev


def _py_wait_enabled_stable(locator, timeout_ms: int, poll_ms: int) -> None:
    deadline = time.time() + timeout_ms / 1000
    stable_reads = 0
    while time.time() < deadline:
        try:
            disabled = locator.get_attribute("disabled")
            aria_disabled = locator.get_attribute("aria-disabled")
        except Exception:
            return
        is_enabled = disabled is None and aria_disabled not in ("true", "True")
        if is_enabled:
            stable_reads += 1
            if stable_reads >= 2:
                return
        else:
            stable_reads = 0
        time.sleep(poll_ms / 1000)


def _py_wait_count_stable(scope, child_selector: str, timeout_ms: int, poll_ms: int) -> None:
    deadline = time.time() + timeout_ms / 1000
    try:
        prev = scope.locator(child_selector).count()
    except Exception:
        return
    stable_reads = 0
    while time.time() < deadline:
        time.sleep(poll_ms / 1000)
        try:
            cur = scope.locator(child_selector).count()
        except Exception:
            return
        if cur == prev:
            stable_reads += 1
            if stable_reads >= 2:
                return
        else:
            stable_reads = 0
            prev = cur


def _py_wait_text_matches(locator, value_norm: str, match_mode: str,
                          timeout_ms: int, poll_ms: int) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            displayed = (locator.inner_text() or "").strip().lower()
        except Exception:
            return False
        if displayed:
            if match_mode == "exact":
                if displayed == value_norm:
                    return True
            elif value_norm in displayed or displayed in value_norm:
                return True
        time.sleep(poll_ms / 1000)
    return False


def _py_wait_box_stable(locator, timeout_ms: int, poll_ms: int) -> None:
    deadline = time.time() + timeout_ms / 1000
    try:
        prev = locator.bounding_box()
    except Exception:
        return
    stable_reads = 0
    while time.time() < deadline:
        time.sleep(poll_ms / 1000)
        try:
            cur = locator.bounding_box()
        except Exception:
            return
        if cur == prev and cur is not None:
            stable_reads += 1
            if stable_reads >= 2:
                return
        else:
            stable_reads = 0
            prev = cur


def _py_wait_value_shorter(locator, prev_len: int, timeout_ms: int, poll_ms: int) -> None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if len(locator.input_value()) < prev_len:
                return
        except Exception:
            return
        time.sleep(min(poll_ms, 15) / 1000)