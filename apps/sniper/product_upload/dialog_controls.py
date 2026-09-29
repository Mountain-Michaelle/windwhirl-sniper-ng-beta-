"""
dialog_controls.py

Low-level, reusable Playwright mechanics for driving sniper.ng's Add
Order modal: opening/closing the dialog, opening/closing/verifying
Radix-style comboboxes, advancing wizard steps, and submitting. Nothing
in this file knows what a "product", "price", or "customer" is -- it
only knows how to click, wait, and verify against this SPA's dialog/
combobox widget shapes. Order-specific business logic (which value goes
in which field, how a price gets decided) lives in field_values.py;
top-level order orchestration (what order of operations one order goes
through) lives in add_products.py.

===============================================================================
FIX (Sep 2026, "click succeeds, then times out anyway" report): every
combobox/option/Next/Cancel click in this file except the final submit
passes no_wait_after=True.

Root cause: Playwright's .click() has two phases -- performing the click,
then (unless told not to) waiting to see whether that click triggered a
navigation, so it can wait for the new page/route to settle before
returning. On this SPA (Radix/shadcn comboboxes + a client-side router),
opening a combobox or selecting an option can trip something Playwright's
navigation-watcher misidentifies as a pending route change (a router
prefetch, a history.replaceState, etc.), even though nothing is actually
navigating. The click itself always completed -- every failing case in the
field logs showed "click action done" immediately before the timeout --
the timeout was entirely the post-click "waiting for scheduled navigations
to finish" phase blocking for the full remaining budget on a navigation
that was never coming.

The one click that intentionally still does NOT pass no_wait_after is the
final Add Order submit in _click_final_submit -- there, the dialog closing
IS the success signal the code is waiting to observe immediately
afterwards (detect_save_outcome, in add_products.py), so keeping
Playwright's own settle behavior there is deliberate, not an oversight.
===============================================================================

FIX (Sep 2026, "combobox selection doesn't retry / uploads are slow"):
two separate, compounding bugs:

  (1) A plain (non-searchable) combobox was checked for a matching
      <option> the instant its listbox *container* became visible, before
      its <option> children had necessarily rendered -- a false "no
      matching option" that a slightly later look would not have hit.
      Fixed: `_open_and_pick` always waits for the option list to settle
      before checking, and retries that render-wait/re-check cycle up to
      `cfg.select_retries` times before concluding an option is genuinely
      absent.

  (2) A successful-looking click was never actually verified -- "the
      listbox closed" was trusted as proof, but a stray click, a
      slightly-off click target, or a value the SPA silently rejects can
      also close the listbox. Fixed: after every click, the combobox
      trigger's own displayed text is read back and the selection is
      only accepted once it actually matches -- see
      `_selection_looks_landed` / `_SelectionNotVerified`.
===============================================================================

FIX (Sep 2026, "Add Order dialog itself closes mid-flow, forcing a full
restart"): root-caused to how a stray *open combobox listbox* was being
dismissed before opening the next one -- both prior approaches risked
touching the Dialog's own outside-click zone instead of the listbox:

  (3) Deriving "the" listbox to close from the trigger about to be
      opened next -- wrong whenever the stray listbox belonged to a
      DIFFERENT, earlier combobox; it silently found nothing and did
      nothing, leaving the real stray listbox intercepting the next click.

  (4) Clicking a raw page coordinate near the dialog's corner as a
      synthetic "outside click" -- correct only as long as the dialog
      hadn't shifted since its bounding box was measured; when it had,
      the click could land on the backdrop instead and close the WHOLE
      dialog.

  Fixed with `_ComboState` + `_close_open_combo`: every combobox open is
  tracked by the exact trigger locator that opened it, so a stray
  listbox is always closed the one guaranteed-safe way a Radix/shadcn
  combobox supports -- clicking its OWN trigger again. No page
  coordinate is ever guessed. A scoped Escape (fired with focus already
  inside the popover) is the only fallback, used solely when no trigger
  is tracked yet. Every close attempt is followed by an explicit
  `_dialog_alive(dialog)` check that raises `_DialogClosed` immediately
  if the whole dialog went down anyway, and reports forensic evidence
  via closure_diagnostics -- see that module.

PERF (Sep 2026, "process orders slowly due to some lagging architectural
design ... possibly 100x faster with the persistent logic still there"):
every Python-side polling loop in this file (stable-value reads,
combo-ready checks, option-render settles, bounding-box settles,
selection-landed polls, the Next-button enabled poll) now delegates to
fast_waits.py, which runs the SAME poll INSIDE the browser as a single
page.wait_for_function() call -- one protocol round trip instead of one
round trip per poll iteration. Each helper keeps its original semantics
(same timeouts, same stability requirements, same silent-give-up
behavior) and falls back to a faithful copy of the original Python loop
on any failure, so nothing can regress; see fast_waits.py's docstring
for the full reasoning and caveats.

One further hot-path change: _select_combobox's OUTER _retry_on_timeout
wrapper now retries cfg.outer_select_retries (2) times instead of
cfg.select_retries (4). _open_and_pick already retries internally up to
select_retries times per outer attempt (render-wait re-checks,
reopen+reselect), so the old nesting gave a worst case of 4x4 = 16 full
render+click cycles -- minutes of stall on a genuinely stuck selection.
2x4 = 8 keeps every inner retry while cutting the pathological tail.
===============================================================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from playwright.sync_api import TimeoutError as PWTimeoutError

import apps.sniper.utils.field_selectors as sel
from apps.sniper.product_upload.price_engine import parse_pricevar_option, PriceVarOption
from apps.sniper.product_upload.closure_diagnostics import (
    _emit, _capture_closure_evidence, record_breadcrumb, _bump,
    _OVERLAY_SELECTOR,
)
from apps.sniper.product_upload import fast_waits as _fw
from apps.sniper.product_upload import wait_profile


def _wait_for_stable_value(locator, cfg: AutomationConfig, timeout_ms: int = 300) -> str:
    """PERF: in-page stable-value wait (single round trip) via fast_waits;
    the original Python polling loop is preserved as the fallback there.
    Same signature and return contract as before."""
    return _fw.wait_value_stable(
        locator, timeout_ms=timeout_ms,
        min_stable_ms=35,
        poll_ms=max(10, int(cfg.value_poll_interval_s * 1000)),
    )

def _wait_for_combo_ready(locator, cfg: AutomationConfig,
                           timeout_ms: Optional[int] = None) -> None:
    timeout_ms = cfg.settle_wait_timeout_ms if timeout_ms is None else timeout_ms
    # PERF: in-page enabled-stability wait via fast_waits (its fallback
    # replicates the original two-attribute polling loop exactly).
    _fw.wait_enabled_stable(
        locator, timeout_ms=timeout_ms, min_stable_ms=30,
        poll_ms=max(10, int(cfg.value_poll_interval_s * 1000)),
    )

def _wait_for_option_render(scope, cfg: AutomationConfig, timeout_ms: Optional[int] = None) -> None:
    """Polls sel.OPTION_SELECTOR's count within `scope` until it stops
    changing for 2 consecutive reads (or timeout_ms elapses) -- the
    fallback synchronization for "has the option list finished
    rendering", used only once an immediate check for the specific
    target option has already missed. `scope` is normally a `listbox`
    Locator (see Priority 2: scoping this to the specific open listbox
    rather than the whole page, everywhere this is called) but accepts
    anything with a compatible .locator() method, including `page`."""
    timeout_ms = cfg.option_render_wait_ms if timeout_ms is None else timeout_ms
    # PERF (Phase 1): the page-scope branch that used to live here was a
    # hand-rolled Python polling loop (time.sleep(0.025) + a .count() round
    # trip per iteration). wait_count_stable() already handles BOTH scopes:
    # a Locator scope runs the in-page one-round-trip stability wait, and a
    # scope with no .element_handle (i.e. page) delegates to _py_wait_count_
    # stable -- which is the same loop, faithfully, with the same 25ms poll
    # and the same "2 consecutive equal reads" exit. So this is now ONE
    # code path instead of two, with identical semantics, and it is
    # instrumented (wait_count_stable / fallback.wait_count_stable) so the
    # page-scope cost shows up in the wait profile instead of hiding here.
    _fw.wait_count_stable(scope, child_selector=sel.OPTION_SELECTOR,
                           timeout_ms=timeout_ms, min_stable_ms=50, poll_ms=25)

def _wait_stable(locator, timeout_ms: int = 350, interval_s: float = 0.03) -> None:
    # PERF: in-page bounding-box stability wait via fast_waits (its
    # fallback replicates the original polling loop exactly).
    _fw.wait_box_stable(locator, timeout_ms=timeout_ms, min_stable_ms=60,
                         poll_ms=max(10, int(interval_s * 1000)))

def _wait_for_overlay_gone(page, cfg: AutomationConfig, timeout_ms: Optional[int] = None) -> None:
    """Best-effort wait for any leftover modal overlay/backdrop to finish
    animating out and disappear before we go clicking through where it
    used to be. Closing a dialog's own content locator (sel.DIALOG_SELECTOR)
    can resolve as done while the separate overlay sibling is still
    mid fade-out and still intercepting pointer events for a few hundred
    ms -- this is what caused the *next* order's very first "Add Order"
    click to time out against a leftover backdrop in the field logs.
    Harmless no-op if no such element is present or if this takes longer
    than the timeout -- callers still proceed and let their own
    click-retry logic absorb the rest."""
    timeout_ms = cfg.overlay_gone_timeout_ms if timeout_ms is None else timeout_ms
    t0 = time.perf_counter()
    try:
        overlay = page.locator(_OVERLAY_SELECTOR)
        if overlay.count() == 0:
            return
        overlay.first.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass
    finally:
        # One accounting point covering the no-overlay early return, the
        # normal hidden wait, and the exception path.
        wait_profile.add("overlay_gone", (time.perf_counter() - t0) * 1000.0)

def _retry_on_timeout(action, cfg: AutomationConfig, label: str,
                       retries: Optional[int] = None,
                       dialog=None, page=None, row_number: Optional[int] = None) -> Optional[str]:
    attempts = retries if retries is not None else cfg.submit_click_retries
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            action()
            return None
        except PWTimeoutError as e:
            last_err = e
            if dialog is not None:
                closed_reason = _require_dialog_open(dialog, label, page=page, cfg=cfg,
                                                       row_number=row_number)
                if closed_reason:
                    _emit(cfg, f"[abort] {label}: dialog closed while retrying "
                               f"(attempt {attempt}/{attempts}) -- not retrying "
                               f"further, {closed_reason}")
                    return closed_reason
            if attempt < attempts:
                # FIX (Sep 2026, "no human wait in the code"): retry
                # immediately -- a timeout already means we spent the
                # full timeout budget waiting; adding a further fixed
                # backoff on top of that only adds latency; it does not
                # make the retry any more likely to succeed.
                _emit(cfg, f"[retry] {label}: attempt {attempt}/{attempts} timed out, retrying...")
            continue
    _emit(cfg, f"[error] {label}: failed after {attempts} attempt(s) -- {last_err}")
    return f"{label}: timed out after {attempts} attempt(s) -- {last_err}"

def click_resilient(scope, candidates: list[Callable], label: str,
                     timeout_ms: int = 8000) -> None:
    """FIX (Sep 2026): click(no_wait_after=True) -- see module docstring.
    None of the buttons reached through this helper (Add Order, Multiple
    products, Add next product) trigger a real page navigation; the
    caller always verifies the actual outcome afterwards (dialog
    visibility, next field's own .wait_for, etc.), so there is nothing
    for Playwright's post-click navigation wait to usefully wait for
    here -- only dead time to pay when the SPA's router activity is
    mistaken for one."""
    last_err = None
    t0 = time.perf_counter()
    try:
        for make_locator in candidates:
            try:
                loc = make_locator(scope)
                loc.first.wait_for(state="visible", timeout=timeout_ms)
                loc.first.click(no_wait_after=True)
                return
            except PWTimeoutError as e:
                last_err = e
                continue
            except Exception as e:
                last_err = e
                continue
        raise PWTimeoutError(
            f"click_resilient({label}): none of {len(candidates)} selector strategy(ies) "
            f"worked within {timeout_ms}ms. Last error: {last_err}"
        )
    finally:
        # One accounting point covering the successful click and the
        # all-candidates-failed raise. Also bumped on failure so a run that
        # is quietly burning its full click budget per attempt is visible.
        wait_profile.add("click_resilient", (time.perf_counter() - t0) * 1000.0)

def _dialog_open_state(dialog) -> Optional[bool]:
    try:
        state = dialog.get_attribute("data-state")
    except Exception:
        return None
    if state == "closed":
        return False
    if state == "open":
        return True
    return None

def _require_dialog_open(dialog, step_label: str, page=None, cfg=None,
                          row_number: Optional[int] = None) -> Optional[str]:
    open_state = _dialog_open_state(dialog)
    if open_state is True:
        return None
    if open_state is False:
        reason = (f"Add Order dialog's data-state flipped to 'closed' after "
                  f"{step_label} -- aborting this order")
        if page is not None and cfg is not None:
            _capture_closure_evidence(page, cfg, reason, row_number, step_label)
        return reason
    try:
        if dialog.is_visible():
            return None
    except Exception:
        pass
    reason = f"Add Order dialog was no longer open after {step_label} -- aborting this order"
    if page is not None and cfg is not None:
        _capture_closure_evidence(page, cfg, reason, row_number, step_label)
    return reason

def _dialog_alive(dialog) -> bool:
    open_state = _dialog_open_state(dialog)
    if open_state is not None:
        return open_state
    try:
        return bool(dialog.is_visible())
    except Exception:
        return False

class _NoMatchingOption(Exception):
    pass

class _DialogClosed(Exception):
    pass

class _SelectionNotVerified(Exception):
    """FIX (Sep 2026): raised when an option was clicked and the listbox
    closed, but the combobox trigger's displayed text still doesn't match
    what we intended to select, even after retrying. A closed listbox is
    NOT proof a selection landed -- a stray click, a click that landed on
    the wrong option, or a value the SPA silently rejects/reverts can all
    close the listbox too. Treated the same way _NoMatchingOption is: a
    real, reportable failure rather than something silently swallowed."""
    pass

def _listbox_locator_for(page, combo_locator):
    try:
        controls_id = combo_locator.get_attribute("aria-controls")
    except Exception:
        controls_id = None
    if controls_id:
        return page.locator(f'[id="{controls_id}"]')
    return page.locator(sel.LISTBOX_SELECTOR).first

@dataclass
class _ComboState:
    """FIX (Sep 2026, "modal closes mid-flow"): tracks the single
    combobox trigger that is *currently open* within one Add Order
    dialog session (one instance is created per dialog -- see
    _fill_products_screen / process_order).

    This exists because both prior ways of dismissing a stray open
    listbox were guesses that could touch the Dialog's own
    outside-click zone instead of the listbox:

      - Deriving "the" listbox to close from the trigger about to be
        opened next (its aria-controls) -- wrong when the stray
        listbox belongs to a DIFFERENT, earlier combobox.
      - Clicking a raw page coordinate near the dialog's corner as a
        synthetic "outside click" -- correct only as long as the
        dialog hadn't shifted since its bounding box was measured;
        when it had, the click could land on the backdrop instead and
        close the whole dialog.

    With the exact trigger tracked here, closing a stray combobox is
    never a guess: it is always the same trigger element clicked a
    second time, which every combobox on this page already supports as
    its own built-in "close me" affordance.
    """
    current_trigger: object = None

def _close_open_combo(page, dialog, state: "_ComboState", cfg: AutomationConfig,
                      row_number: Optional[int] = None) -> bool:
    """Dismiss a stray open combobox listbox WITHOUT ever risking the Add
    Order Dialog itself. Returns True once no listbox overlay is observed,
    False when the overlay would not clear (the caller retries instead).

    FIX (Oct 2026, modal closes at the combo step then the order restarts):
    the old fallback fired a GLOBAL Escape with focus moved inside the
    modal. Radix dismisses the TOPMOST open layer for a global Escape --
    and when the stray listbox had already closed on its own (or never
    rendered), the topmost layer IS the Add Order Dialog, so the whole
    modal closed and the order restarted as a transient failure. The
    replacement dismisses the overlay by CLICKING A REAL SELECTION, never
    a keypress (mirrors click_resilient's pre-recovery click):
      (a) re-click the tracked trigger (toggle-close -- the click never
          leaves the trigger element itself);
      (b) click the previously-clicked element recorded in
          _ComboState.last_clicked (a real selection the SPA accepted
          before -- guarded to dialog scope);
      (c) click dead-space INSIDE the Dialog (its own label area -- a
          listbox outside-click that cannot touch the Dialog's outside
          zone).
    After every step the overlay is RE-CHECKED; a leftover that survives
    all three returns False so the caller retries the open-and-pick cycle
    (bounded by the existing select_retries) instead of killing the modal.
    NOTHING here presses Escape or any other key.
    """
    try:
        overlay_open = page.locator(sel.LISTBOX_SELECTOR).count() > 0 and \
            page.locator(sel.LISTBOX_SELECTOR).first.is_visible()
    except Exception:
        overlay_open = False
    if not overlay_open:
        state.current_trigger = None
        return True

    def _overlay_gone() -> bool:
        try:
            return not (page.locator(sel.LISTBOX_SELECTOR).count() > 0 and
                        page.locator(sel.LISTBOX_SELECTOR).first.is_visible())
        except Exception:
            return True

    # -- (a) re-click the tracked trigger (unchanged primary) --------------
    if state.current_trigger is not None:
        try:
            state.current_trigger.click(timeout=cfg.combo_click_timeout_ms,
                                        no_wait_after=True)
        except Exception:
            pass
        else:
            try:
                page.locator(sel.LISTBOX_SELECTOR).first.wait_for(
                    state="hidden", timeout=1000)
            except Exception:
                pass
        state.current_trigger = None
        if _overlay_gone():
            return True

    # -- (b) click the last real selection (click_resilient's pattern) -----
    last = state.last_clicked
    try:
        usable = last is not None and last.count() > 0
    except Exception:
        usable = False
    if usable:
        try:
            last.first.click(timeout=1500, no_wait_after=True)
        except Exception:
            pass
        if _overlay_gone():
            return True

    # -- (c) dead-space click INSIDE the Dialog -----------------------------
    # A visible header/label area is part of the Dialog's own layer: an
    # outside-click for the floating listbox, a no-op for the Dialog
    # (which only closes on ITS outside zone, Cancel, or submit). The
    # dialog's own X ("Close") is deliberately NEVER in this list.
    for get_target in (
        lambda s: s.locator("label").first,
        lambda s: s.locator("h2").first,
        lambda s: s.locator("fieldset").first,
    ):
        try:
            target = get_target(dialog)
            if target.count() > 0 and target.first.is_visible():
                target.first.click(timeout=1500, no_wait_after=True)
                break
        except Exception:
            continue
    state.current_trigger = None
    if not _overlay_gone():
        _emit(cfg, "[combo] stray listbox would not clear via trigger / "
                   "last-selection / in-dialog clicks -- leaving it for "
                   "the caller to retry instead of firing Escape")
        return False
    if not _dialog_alive(dialog):
        reason = ("Add Order dialog closed while dismissing a stray open "
                  "listbox via in-dialog clicks")
        _capture_closure_evidence(page, cfg, reason, row_number,
                                  "_close_open_combo: real-selection close")
        raise _DialogClosed(reason)
    return True



def _read_trigger_display_text(combo_locator) -> str:
    """FIX (Sep 2026): best-effort read of what a combobox trigger
    currently displays, used to verify a selection actually landed
    instead of trusting "the listbox closed" as proof."""
    try:
        return (combo_locator.inner_text() or "").strip()
    except Exception:
        return ""

def _selection_looks_landed(combo_locator, value: str, match_mode: str) -> bool:
    """FIX (Sep 2026): does the combobox trigger now display the value we
    intended to select? `exact` requires the trigger text to equal the
    value (case-insensitive); anything else (e.g. `contains`, used for
    state selection where the option text may be 'Lagos State' for a
    target of 'Lagos') accepts either direction of substring match."""
    displayed = _read_trigger_display_text(combo_locator)
    if not displayed:
        return False
    displayed_norm = displayed.strip().lower()
    value_norm = value.strip().lower()
    if match_mode == "exact":
        return displayed_norm == value_norm
    return value_norm in displayed_norm or displayed_norm in value_norm

def _poll_selection_landed(combo_locator, value: str, match_mode: str,
                            cfg: AutomationConfig, timeout_ms: int) -> bool:
    """FIX (strategic speed upgrade, Priority 3 -- "verify the exact
    state actually needed, immediately, instead of waiting for a broader
    UI signal first"): the state that actually matters after clicking an
    option is "does the trigger now display the value" -- not "has the
    listbox visually finished closing". Those are independent DOM
    regions; there was never a real reason the first had to wait on the
    second. This checks _selection_looks_landed() immediately, and only
    if it hasn't landed yet, polls it tightly (cfg.value_poll_interval_s)
    for a short bounded window -- a targeted micro-retry against the
    exact thing being waited for, not a fixed sleep and not a wait on a
    different element. Returns True/False; never raises (a detached
    trigger mid-poll just reads as "not landed yet", same as the
    Exception handling already inside _read_trigger_display_text)."""
    if _selection_looks_landed(combo_locator, value, match_mode):
        return True
    # PERF: in-page text-match wait via fast_waits (its fallback
    # replicates the original polling loop exactly).
    return _fw.wait_text_matches(
        combo_locator, value, match_mode, timeout_ms=timeout_ms,
        poll_ms=max(10, int(cfg.value_poll_interval_s * 1000)),
    )

def _open_and_pick(dialog, page, combo_locator, value: str, cfg: AutomationConfig,
                    state: "_ComboState", match_mode: str = "exact",
                    label: str = "", row_number: Optional[int] = None) -> None:
    """FIX (Sep 2026): rewritten to (a) always wait for the option list to
    finish rendering before checking for a match -- not just when a
    search box is present -- and retry that render-wait/re-check cycle up
    to cfg.select_retries times before concluding an option is genuinely
    absent, (b) verify the selection actually landed by reading back the
    trigger's displayed text after each click, retrying (reopening the
    combo) rather than trusting "the listbox closed" as proof of success,
    and (c) never dismiss a stray listbox by guessing a page coordinate --
    see _ComboState / _close_open_combo. See the module docstring for the
    field-log evidence this is fixing. Every point below that can
    discover the *whole dialog* has closed captures forensic evidence
    (screenshot / state dump / saved trace chunk) before raising
    _DialogClosed -- see _capture_closure_evidence."""
    value = value.strip()
    step = f"_open_and_pick({label or value!r})"

    if not _dialog_alive(dialog):
        reason = f"Add Order dialog was already closed before opening the combobox for value={value!r}"
        _capture_closure_evidence(page, cfg, reason, row_number, step,
                                   selector=label or None, attempted_value=value)
        raise _DialogClosed(reason)

    _close_open_combo(page, dialog, state, cfg, row_number=row_number)
    if label:
        _emit(cfg, f"[combo] opening {label} -> {value!r}")
        record_breadcrumb(cfg, f"opening combobox {label!r} -> {value!r}")

    # FIX (Sep 2026): click(no_wait_after=True) -- this is the trigger
    # click for EVERY combobox in the flow (Owning Form, product, price
    # offer, payment method, order source, state). Opening a combobox
    # never navigates; the very next line already waits for the
    # listbox itself to become visible, which is the real signal this
    # click succeeded. Without no_wait_after, Playwright's post-click
    # navigation wait was absorbing this call's whole
    # combo_click_timeout_ms budget on this SPA -- see module
    # docstring.
    try:
        combo_locator.click(timeout=cfg.combo_click_timeout_ms, no_wait_after=True)
    except PWTimeoutError:
        raise
    except Exception:
        if not _dialog_alive(dialog):
            reason = f"Add Order dialog closed while clicking the combobox trigger for value={value!r}"
            _capture_closure_evidence(page, cfg, reason, row_number, step,
                                       selector=label or None, attempted_value=value)
            raise _DialogClosed(reason)
        raise

    listbox = _listbox_locator_for(page, combo_locator)
    listbox.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)
    # FIX (Sep 2026): this combobox is now the one open listbox on the
    # page -- record its trigger so any dismissal needed from here
    # (either later in this function, or by the NEXT combobox's own
    # opening step) always has the exact, correct trigger to click
    # again, instead of guessing.
    state.current_trigger = combo_locator

    has_search = False
    try:
        search_input = page.locator(sel.COMBOBOX_SEARCH_INPUT_SELECTOR)
        has_search = search_input.count() > 0 and search_input.first.is_visible()
        if has_search:
            search_input.first.fill(value)
    except Exception:
        has_search = False

    # FIX (Sep 2026): previously this wait was only reached on the
    # search-input path (as a side effect of waiting out the re-render
    # triggered by typing). A plain select -- payment method, order
    # FIX (strategic speed upgrade, Priorities 1 & 2): two changes from
    # the previous version.
    #
    # (1) Scoped to `listbox`, not `page`. Every option lookup used to
    # search the WHOLE page for sel.combo_option(...)/sel.OPTION_SELECTOR
    # matches. Once this combobox's own listbox is known (it's confirmed
    # visible a few lines up), there's no reason to search anywhere else
    # -- it's both a smaller, faster query and, on the off chance
    # something odd is still rendered elsewhere on the page, a more
    # precise one. Falls back to a page-wide search only if
    # _listbox_locator_for() itself had to fall back (no aria-controls
    # found) -- see that function; in that case `listbox` is already the
    # least-precise thing available, so scoping to it changes nothing.
    #
    # (2) The immediate pre-wait check now also runs on the search-input
    # path, not just the no-search one -- but ONLY using the exact
    # data-value selector (sel.combo_option), never the fuzzy has_text
    # fallback. Typing into the search box does trigger a re-render that
    # may still be in flight, so a fuzzy text match right after typing
    # really could hit a stale, pre-filter option by coincidence -- but
    # an EXACT data-value match can't be a coincidence; if it's there,
    # it's the real option, rendered fast enough that there's no reason
    # to wait for anything further. Either way, when the check misses,
    # the very next line covers exactly the same "give it time to
    # render" case the unconditional wait always used to.
    quick_check = listbox.locator(sel.combo_option(value, match_mode))
    skip_wait = quick_check.count() > 0
    if not skip_wait and not has_search:
        quick_check = listbox.locator(sel.OPTION_SELECTOR).filter(has_text=value)
        skip_wait = quick_check.count() > 0
    if skip_wait:
        _bump(cfg, "combo_option_immediate_find")
    else:
        _bump(cfg, "combo_option_render_wait")
        _wait_for_option_render(listbox, cfg)

    attempts = max(1, cfg.select_retries)
    for attempt in range(1, attempts + 1):
        if not _dialog_alive(dialog):
            reason = f"Add Order dialog closed while selecting value={value!r} (attempt {attempt}/{attempts})"
            _capture_closure_evidence(page, cfg, reason, row_number, step)
            raise _DialogClosed(reason)

        option = listbox.locator(sel.combo_option(value, match_mode))
        target = option if option.count() > 0 else \
            listbox.locator(sel.OPTION_SELECTOR).filter(has_text=value)

        if target.count() == 0:
            if attempt < attempts:
                # FIX (Sep 2026, "no human wait in the code"): re-poll
                # immediately instead of sleeping first -- this is the
                # retry the field logs show never happening, and
                # _wait_for_option_render() itself already blocks (via
                # its own stable-read polling) until the option list
                # actually finishes changing, so a fixed sleep in front
                # of it only delays the re-check without giving the SPA
                # any real extra time it wasn't already getting.
                _bump(cfg, "combo_option_render_wait")
                _wait_for_option_render(listbox, cfg)
                continue
            # FIX (Oct 2026, modal-closing bug): the old code called
            # _close_open_combo() here and IGNORED its boolean -- and the
            # boolean is the whole point (overlay still open vs cleared).
            # A stale visible listbox at this point means the option
            # genuinely is not there, so close it best-effort and raise;
            # a FAILED close (False) is even more reason to raise rather
            # than push on into a click that will collide with it.
            _close_open_combo(page, dialog, state, cfg, row_number=row_number)
            raise _NoMatchingOption(
                f"no option matched value={value!r} (match_mode={match_mode!r}) "
                f"by data-value or by visible text -- nothing available to "
                f"click after {attempts} render-wait attempt(s)"
            )

        try:
            # REVERTED (per the "don't use force=True to bypass
            # application behavior" review): last turn's version tried
            # force=True here first. On reflection that's the wrong
            # trade -- force=True skips Playwright's own visible/stable/
            # enabled/receives-events checks, which means a click that
            # LANDS SOMEWHERE (Playwright won't throw) but not
            # necessarily on the intended option if it's mid-animation or
            # briefly covered by something else -- exactly the "assume a
            # click succeeded because Playwright didn't throw" trap. The
            # selection-landed check below would eventually catch that
            # via a retry, but "eventually catch a wrong click" is worse
            # than "don't risk a wrong click in the first place", and the
            # speed difference between a forced and a normal click here
            # is small next to what scoping the search to the listbox
            # (above) and not blocking on listbox-hidden (below) already
            # save. Back to a normal, fully-checked click.
            target.first.click(timeout=cfg.option_click_timeout_ms, no_wait_after=True)
        except PWTimeoutError:
            if not _dialog_alive(dialog):
                reason = f"Add Order dialog closed while clicking option value={value!r} (attempt {attempt}/{attempts})"
                _capture_closure_evidence(page, cfg, reason, row_number, step)
                raise _DialogClosed(reason)
            if attempt < attempts:
                continue
            raise
        except Exception:
            if not _dialog_alive(dialog):
                reason = f"Add Order dialog closed while clicking option value={value!r} (attempt {attempt}/{attempts})"
                _capture_closure_evidence(page, cfg, reason, row_number, step)
                raise _DialogClosed(reason)
            raise

        # FIX (strategic speed upgrade, Priority 3 -- "verify the exact
        # state needed, immediately, instead of waiting for a broader UI
        # signal first"): what actually matters after this click is
        # whether the TRIGGER now displays the value -- that's read via
        # _poll_selection_landed(), independent of whether the listbox
        # has visually finished closing (they're separate DOM regions).
        # Gating this check behind listbox.wait_for(state="hidden", ...)
        # first was always waiting on a signal the check never actually
        # needed. That wait is still useful -- just not as a mandatory
        # precondition on the success path; it now runs as best-effort
        # cleanup below, only once we already know a retry is coming.
        if _poll_selection_landed(combo_locator, value, match_mode, cfg,
                                   timeout_ms=cfg.selection_landed_poll_ms):
            if not _dialog_alive(dialog):
                reason = f"Add Order dialog closed right after selecting value={value!r} (attempt {attempt}/{attempts})"
                _capture_closure_evidence(page, cfg, reason, row_number, step)
                raise _DialogClosed(reason)
            state.current_trigger = None
            if label:
                _emit(cfg, f"[combo] {label} confirmed -> {value!r}")
                record_breadcrumb(cfg, f"confirmed {label!r} -> {value!r}")
            return

        # Not landed within the micro-poll -- a real dialog closure
        # explains that better than "just needs more time", so check for
        # one now; only after that, let the listbox finish closing
        # (best-effort) as part of getting ready to retry.
        if not _dialog_alive(dialog):
            reason = f"Add Order dialog closed right after selecting value={value!r} (attempt {attempt}/{attempts})"
            _capture_closure_evidence(page, cfg, reason, row_number, step)
            raise _DialogClosed(reason)
        try:
            listbox.wait_for(state="hidden", timeout=300)
        except Exception:
            pass

        _bump(cfg, "combo_selection_retry")
        if attempt < attempts:
            _emit(cfg, f"[select] value={value!r}: option was clicked but the "
                       f"trigger doesn't show it yet (attempt {attempt}/{attempts}) "
                       f"-- reopening and retrying instead of assuming success")
            # FIX (Oct 2026, modal-closing bug): the old code re-closed the
            # listbox best-effort and then RE-CLICKED THE SAME TRIGGER
            # unconditionally -- even when _close_open_combo reported the
            # overlay still open (False). Clicking a trigger while a stale
            # overlay is up is exactly the collision that used to cascade
            # into the Escape fallback and kill the modal. Now a failed
            # close aborts this attempt early and lets the loop's next
            # iteration (or the next outer retry) start from a clean
            # _close_open_combo at the top instead.
            cleared = True
            try:
                if listbox.is_visible():
                    cleared = _close_open_combo(page, dialog, state, cfg,
                                                row_number=row_number)
            except Exception:
                pass
            if not cleared:
                _emit(cfg, f"[select] value={value!r}: stray listbox would not "
                           f"clear (attempt {attempt}/{attempts}) -- backing off "
                           f"to the next attempt instead of clicking into it")
                continue
            try:
                combo_locator.click(timeout=cfg.combo_click_timeout_ms, no_wait_after=True)
                listbox.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)
                state.current_trigger = combo_locator
                if has_search:
                    try:
                        search_input = page.locator(sel.COMBOBOX_SEARCH_INPUT_SELECTOR)
                        if search_input.count() > 0 and search_input.first.is_visible():
                            search_input.first.fill(value)
                    except Exception:
                        pass
                _wait_for_option_render(listbox, cfg)
            except PWTimeoutError:
                if not _dialog_alive(dialog):
                    reason = (f"Add Order dialog closed while reopening the combobox "
                              f"to retry value={value!r} (attempt {attempt}/{attempts})")
                    _capture_closure_evidence(page, cfg, reason, row_number, step)
                    raise _DialogClosed(reason)
                raise
            continue

        raise _SelectionNotVerified(
            f"clicked a matching option for value={value!r} (match_mode={match_mode!r}) "
            f"{attempts} time(s), but the combobox never displayed it afterwards -- "
            f"refusing to assume the listbox closing means the value landed"
        )

def _select_combobox(dialog, page, trigger_locator, value: str, cfg: AutomationConfig,
                      label: str, state: "_ComboState", match_mode: str = "exact",
                      row_number: Optional[int] = None) -> Optional[str]:
    try:
        # PERF: outer retries capped at cfg.outer_select_retries (2) --
        # _open_and_pick already retries internally up to select_retries
        # times per outer attempt, so the old outer=select_retries nesting
        # meant a worst case of 16 full render+click cycles for one stuck
        # selection. See the module docstring's PERF note.
        return _retry_on_timeout(
            lambda: _open_and_pick(dialog, page, trigger_locator, value, cfg, state,
                                    match_mode=match_mode, label=label, row_number=row_number),
            cfg, f"selecting {label}", retries=cfg.outer_select_retries, dialog=dialog,
            page=page, row_number=row_number,
        )
    except (_NoMatchingOption, _SelectionNotVerified) as e:
        # FIX (Sep 2026): _open_and_pick already retries internally
        # (render-wait re-checks for _NoMatchingOption, reopen+reselect
        # for _SelectionNotVerified) up to cfg.select_retries times
        # before raising either of these -- so by the time we're here,
        # a genuine, repeated failure has already been established, not
        # a one-shot check. Reported as a real failure for this order
        # rather than silently retried again at a higher (slower) level.
        _emit(cfg, f"[error] selecting {label}: {e} -- already retried "
                   f"internally, giving up on this order")
        return f"selecting {label}: {e}"
    except _DialogClosed as e:
        _emit(cfg, f"[error] selecting {label}: {e} -- not retrying a closed dialog")
        return f"selecting {label}: {e}"

def _open_add_order_wizard(page, cfg: AutomationConfig, row_number: Optional[int] = None):
    # FIX (Sep 2026): make sure nothing from a previous (possibly failed)
    # dialog is still animating out and covering the page before we go
    # clicking where it used to be -- see _wait_for_overlay_gone's
    # docstring and the module docstring's bug (2). Cheap, bounded,
    # no-op if there's nothing there.
    _wait_for_overlay_gone(page, cfg)

    err = _retry_on_timeout(
        lambda: click_resilient(page, [
            lambda s: s.get_by_role("button", name=sel.ADD_ORDER_BUTTON_NAME, exact=True),
            lambda s: s.locator(f'button:has-text("{sel.ADD_ORDER_BUTTON_NAME}")'),
        ], "open Add Order"),
        cfg, "open Add Order", page=page, row_number=row_number,
    )
    if err:
        raise RuntimeError(err)

    dialog = page.locator(sel.DIALOG_SELECTOR).first
    dialog.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)

    err = _retry_on_timeout(
        lambda: click_resilient(dialog, [
            lambda s: s.get_by_role("button", name=sel.MULTI_PRODUCT_BUTTON_NAME),
            lambda s: s.locator(f'button:has-text("{sel.MULTI_PRODUCT_BUTTON_NAME}")'),
        ], "select Multiple products"),
        cfg, "select Multiple products", dialog=dialog, page=page, row_number=row_number,
    )
    if err:
        raise RuntimeError(err)

    dialog.locator(f'label:has-text("{sel.OWNING_FORM_LABEL}")').first.wait_for(
        state="visible", timeout=cfg.network_wait_timeout_ms
    )
    _wait_stable(dialog)
    return dialog

def _click_add_next_product(dialog, page, cfg: AutomationConfig,
                             row_number: Optional[int] = None) -> Optional[str]:
    return _retry_on_timeout(
        lambda: click_resilient(dialog, [
            lambda s: s.get_by_role("button", name=sel.ADD_NEXT_PRODUCT_BUTTON_NAME),
            lambda s: s.locator(f'button:has-text("{sel.ADD_NEXT_PRODUCT_BUTTON_NAME}")'),
        ], "Add next product"),
        cfg, "Add next product", retries=cfg.select_retries, dialog=dialog,
        page=page, row_number=row_number,
    )

def _read_combobox_options(dialog, page, combo, cfg: AutomationConfig,
                            state: "_ComboState", close_after: bool = True,
                            row_number: Optional[int] = None) -> list[PriceVarOption]:
    step = "_read_combobox_options"
    if not _dialog_alive(dialog):
        reason = "Add Order dialog was already closed before reading combobox options"
        _capture_closure_evidence(page, cfg, reason, row_number, step)
        raise _DialogClosed(reason)

    _close_open_combo(page, dialog, state, cfg, row_number=row_number)

    # FIX (Sep 2026): click(no_wait_after=True) -- this is the single
    # most-executed combobox-open in the whole flow (every price-offer
    # read goes through here), and was previously the ONE unguarded
    # combobox click in the file with no explicit timeout at all,
    # inheriting Playwright's 30000ms default navigation wait on top of
    # that. Opening this dropdown never navigates; the next line already
    # waits for the real signal (the listbox becoming visible).
    try:
        combo.click(timeout=cfg.combo_click_timeout_ms, no_wait_after=True)
    except PWTimeoutError:
        raise
    except Exception:
        if not _dialog_alive(dialog):
            reason = "Add Order dialog closed while opening the price-offer dropdown"
            _capture_closure_evidence(page, cfg, reason, row_number, step)
            raise _DialogClosed(reason)
        raise
    listbox = _listbox_locator_for(page, combo)
    listbox.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)
    state.current_trigger = combo
    # FIX (Sep 2026): same render-race guard as _open_and_pick -- reading
    # the option list back out the instant the listbox container is
    # visible could previously catch it mid-render and return a
    # truncated/empty set, which would then feed decide_price_offer bad
    # data. Wait for the option count to actually settle first.
    #
    # FIX (strategic speed upgrade, Priority 2 -- "scope to the active
    # listbox, not the whole page"): this is the read that populates
    # cfg.price_options_cache for a product -- every one of these options
    # ends up cached and reused for every later order needing that same
    # product, so precision here matters even more than usual. Scoping
    # both the render-settle check and the actual read to `listbox`
    # (instead of `page`) means this can't accidentally pick up an
    # option belonging to some other element still in the DOM, and it's
    # a smaller, faster query either way.
    _wait_for_option_render(listbox, cfg)
    raw = listbox.locator(sel.OPTION_SELECTOR).evaluate_all(
        "els => els.map(e => ({label: e.textContent.trim(), "
        "value: e.getAttribute('data-value') || e.textContent.trim()}))"
    )
    if close_after:
        _close_open_combo(page, dialog, state, cfg, row_number=row_number)
    return [parse_pricevar_option(o["label"], o["value"]) for o in raw]

def _click_next(dialog, page, cfg: AutomationConfig, label: str,
                 row_number: Optional[int] = None) -> Optional[str]:
    def do_click():
        next_btn = dialog.get_by_role("button", name=sel.NEXT_BUTTON_NAME, exact=True)
        next_btn.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)
        # PERF: the old Python is_enabled() polling loop (one round trip
        # per 15ms until the first enabled read) is now a single in-page
        # wait via fast_waits. Like the original, a timeout here is NOT an
        # error -- the click below still performs its own actionability
        # checks and the _retry_on_timeout wrapper handles failures.
        _fw.wait_enabled_now(next_btn, timeout_ms=cfg.element_visible_timeout_ms)
        # FIX (Sep 2026): click(no_wait_after=True) -- advancing a wizard
        # STEP (Products -> Charges -> Customer -> Summary) never
        # triggers a real browser navigation, it's an in-dialog step
        # change; the caller verifies the real outcome via
        # _require_dialog_open()/the next step's own field checks right
        # after this returns. Without no_wait_after this was one of the
        # calls burning its full timeout on the SPA's phantom
        # navigation. NOTE: this is distinct from the FINAL submit click
        # in _click_final_submit below, which intentionally keeps
        # Playwright's default wait -- there, the dialog closing really
        # is the expected signal being waited on.
        next_btn.click(timeout=cfg.combo_click_timeout_ms, no_wait_after=True)
        record_breadcrumb(cfg, f"clicked Next ({label})")

    return _retry_on_timeout(do_click, cfg, f"Next ({label})", dialog=dialog,
                              page=page, row_number=row_number)

def _click_final_submit(dialog, cfg: AutomationConfig) -> Optional[str]:
    def do_click():
        btn = dialog.get_by_role("button", name=sel.FINAL_SUBMIT_BUTTON_NAME, exact=True)
        btn.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)
        btn.click(no_wait_after=True, timeout=cfg.network_wait_timeout_ms)

    # NOTE: deliberately NOT passing `dialog=` here -- unlike every
    # other _retry_on_timeout call site, the dialog closing IS the
    # expected success signal for this specific click (see
    # detect_save_outcome, called right after this returns), so an
    # early-abort-on-closed-dialog check here would misfire on the
    # success path itself.
    return _retry_on_timeout(do_click, cfg, "final Add Order submit")

def detect_save_outcome(dialog, cfg: AutomationConfig) -> tuple[str, str]:
    # PERF (Phase 0): this is the single longest blocking wait in the whole
    # order -- it sits on dialog.wait_for(state="hidden") for up to
    # cfg.success_wait_timeout_ms. It is a genuine signal wait (it returns
    # the moment the dialog really closes), so it stays; but its cost is
    # now measured, so Phase 0 can say for FACT whether submit-detection is
    # the bottleneck instead of guessing.
    t0 = time.perf_counter()
    try:
        dialog.wait_for(state="hidden", timeout=cfg.success_wait_timeout_ms)
        return "success", "order dialog closed after submit"
    except PWTimeoutError:
        pass
    finally:
        wait_profile.add("save_outcome_dialog_hidden",
                         (time.perf_counter() - t0) * 1000.0)

    t1 = time.perf_counter()
    try:
        for err_sel in cfg.error_selectors:
            try:
                loc = dialog.locator(err_sel)
                if loc.count() > 0 and loc.first.is_visible():
                    text = loc.first.inner_text().strip()
                    return "failure", f"error indicator matched ({err_sel!r}): {text}"
            except Exception:
                continue
    finally:
        # Single accounting point: the finally also covers the early
        # `return` above, so an error-indicator hit is counted once, not
        # twice.
        wait_profile.add("save_outcome_error_scan",
                         (time.perf_counter() - t1) * 1000.0)

    return "inconclusive", "dialog still open after submit and no error indicator found"

_DISCARD_CONFIRM_BUTTON_NAMES = ("Discard", "Yes, discard", "Discard changes",
                                  "Leave", "Confirm", "Yes")

def _force_close_any_dialog(page, cfg: AutomationConfig) -> bool:
    """FIX (Sep 2026, "clicking Add Order while the modal is still open"):
    this used to be fire-and-forget -- try a Cancel button, then Escape,
    swallow any exception either way, and return with no indication of
    whether the dialog actually closed. A caller that then went straight
    on to click "Add Order" again had no way to know it was about to
    click behind a still-open modal, which is exactly the field-log
    symptom: two full 30s "open Add Order" timeouts burned retrying
    against a dialog this function silently failed to close.

    Two realistic ways the old best-effort attempt could fail silently
    on this app: (a) a multi-step wizard commonly intercepts Escape (or
    pops a "discard changes?" confirmation ON TOP of the Add Order
    dialog instead of closing it), and (b) if there's no button literally
    named "Cancel", that whole branch was a no-op.

    Now: try Cancel, then Escape, then -- if a nested discard/confirm
    dialog appeared on top as a result -- confirm through it too. After
    every attempt, explicitly re-check whether the dialog is actually
    gone rather than assuming success. Returns True only if a dialog
    (and its overlay) is confirmed closed/absent by the end; False if
    one is still open despite every attempt, so the caller can choose a
    stronger recovery (a full reload) instead of retrying blindly."""
    def dialog_now():
        try:
            return page.locator(sel.DIALOG_SELECTOR).first
        except Exception:
            return None

    dialog = dialog_now()
    if dialog is None:
        return True
    if not _dialog_alive(dialog):
        _wait_for_overlay_gone(page, cfg)
        record_breadcrumb(cfg, "_force_close_any_dialog: nothing to close")
        return True

    for _attempt in range(3):
        # FIX (Sep 2026): click(no_wait_after=True) -- Cancel closes the
        # dialog in-place, it doesn't navigate the page; the outcome is
        # verified explicitly below via _dialog_alive(), not assumed.
        try:
            cancel_btn = dialog.get_by_role("button", name="Cancel", exact=True)
            if cancel_btn.count() > 0:
                cancel_btn.first.click(timeout=cfg.combo_click_timeout_ms, no_wait_after=True)
                try:
                    dialog.wait_for(state="hidden", timeout=cfg.element_visible_timeout_ms)
                except Exception:
                    pass
        except Exception:
            pass

        if not _dialog_alive(dialog):
            _wait_for_overlay_gone(page, cfg)
            return True

        try:
            page.keyboard.press("Escape")
            try:
                dialog.wait_for(state="hidden", timeout=cfg.element_visible_timeout_ms)
            except Exception:
                pass
        except Exception:
            pass

        if not _dialog_alive(dialog):
            _wait_for_overlay_gone(page, cfg)
            return True

        # FIX (Sep 2026): the Escape/Cancel above may have popped a
        # nested "discard unsaved changes?" confirmation ON TOP of the
        # Add Order dialog rather than closing it -- confirm through
        # that before trying again, instead of leaving it stuck there.
        try:
            for discard_name in _DISCARD_CONFIRM_BUTTON_NAMES:
                btn = page.get_by_role("button", name=discard_name)
                if btn.count() > 0:
                    btn.first.click(timeout=cfg.combo_click_timeout_ms, no_wait_after=True)
                    try:
                        dialog.wait_for(state="hidden", timeout=cfg.element_visible_timeout_ms)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        if not _dialog_alive(dialog):
            _wait_for_overlay_gone(page, cfg)
            return True

    # Every attempt failed to close it -- confirmed still open, not
    # assumed closed. Caller must not treat this as safe to proceed.
    return not _dialog_alive(dialog)