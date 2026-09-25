"""
add_products.py

Entry point INTO the order-filling engine: given one already-loaded
orders-list page and one parsed Excel order row, this decides the ORDER
of operations a staff member would follow -- open the wizard, walk each
product line, advance Products -> Charges -> Customer -> Summary, submit,
verify, report back a structured OrderResult -- and delegates every
mechanical detail to the two modules this file is split from:

  - dialog_controls.py -- how to open/close the dialog, drive a
    combobox, advance a wizard step, submit, and detect whether the
    dialog is still alive. Generic, form-agnostic Playwright mechanics.
  - field_values.py -- how to decide and enter the actual value for a
    field: quantity, price/amount (including the bundle trailing-digit
    edit), customer info, state. Generic in the sense that it only knows
    about values and verification, not about wizard steps or dialogs.
  - closure_diagnostics.py -- forensic capture (screenshot, state dump,
    saved trace) for the instant a dialog closure is detected. Called
    from dialog_controls.py's own guard checks, not from here directly.

This file stays deliberately thin: process_order() and its immediate
helpers (_fill_products_screen, _add_one_product_line,
_select_product_and_price) are the actual "recipe" for adding one order
-- reading it top to bottom should tell you the whole story of what
happens to one row of the spreadsheet, with the "how" pushed into the
two supporting modules. This is also why this file is the one to open
first when debugging a specific order: the sequence of steps that order
went through lives here, in one place, even though each step's mechanics
live elsewhere.

===============================================================================
FIX (Sep 2026, "process_order() got an unexpected keyword argument 'plan'"):
run_automation.py builds a per-row ExecutionPlan (product matches + resolved
state, computed once up front by planning.py, with zero Playwright
dependency) and passes it into process_order(plan=...). process_order()
now accepts an optional `plan: ExecutionPlan`. When given, it reuses the
plan's precomputed matches/state and skips redoing
parse_order_products()/infer_state() from scratch -- planning's
PlanningError already ruled out the "no product match" / "no state"
cases before the browser was ever touched. Each MatchResult pulled from
the plan is copied via dataclasses.replace() before use, since
process_order can be called multiple times against the *same* plan
object across lock-mode's transient-failure retries and mutates match
objects in place (needs_manual_review, reason) to record what happened
on that specific attempt -- without the copy, a failed attempt's notes
would stack onto the shared plan's matches and bleed into the next retry.
===============================================================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

import apps.sniper.utils.field_selectors as sel
from apps.sniper.product_upload.product_matcher import parse_order_products, MatchResult
from apps.sniper.product_upload.price_engine import decide_price_offer
from apps.sniper.product_upload.failure_logger import FailedOrder
from apps.sniper.product_upload.planning import ExecutionPlan

from apps.sniper.product_upload.dialog_controls import (
    _ComboState, _DialogClosed, _click_add_next_product, _click_final_submit,
    _click_next, _force_close_any_dialog, _open_add_order_wizard,
    _read_combobox_options, _require_dialog_open, _select_combobox,
    _wait_for_combo_ready, _wait_for_stable_value, _wait_stable,
    detect_save_outcome,
)
from apps.sniper.product_upload.field_values import (
    _amount_input, _apply_amount_edit, _apply_custom_price_replace,
    _clean_phone, _clean_text, _compensate_amount_for_stuck_quantity,
    _fill_custom_price, _fill_customer_info, _fill_quantity,
    _product_block, _quantity_input, _resolve_state, _safe_int,
)
from apps.sniper.product_upload.closure_diagnostics import _emit, _bump


@dataclass
class AutomationConfig:
    crm_catalog: list[str]                       # real CRM product names for matching

    # FIX (Sep 2026, "no human wait in the code"): every field that
    # existed purely to make automation *look* human-paced (typing
    # delay, step-to-step pauses, a pause after a successful save, a
    # pause between retry backoffs) has been removed outright, along
    # with every call site that used them -- see dialog_controls.py and
    # field_values.py. What's left below are only REAL functional
    # waits/timeouts: how long to block for an element, a value, or a
    # dialog state to actually change. Those are not "human" waits --
    # removing them would reopen the exact race conditions the Sep 2026
    # combobox-closure fixes closed. Nothing here ever sleeps for a fixed
    # or randomized duration just to "seem" human; every wait below is
    # tied to an actual, checkable page-state change.
    settle_wait_timeout_ms: int = 1000
    value_poll_timeout_ms: int = 600
    value_poll_interval_s: float = 0.015

    # FIX (Sep 2026, "must work on very low network"): every real
    # timeout below is sized generously so a slow round-trip doesn't get
    # mistaken for a genuine failure. These are ceilings, not durations
    # actually slept -- Playwright's own event-based .wait_for() returns
    # the instant the real condition is met, so a fast network still
    # gets a fast result; only a slow one pays anywhere near the ceiling.
    network_wait_timeout_ms: int = 30000
    success_wait_timeout_ms: int = 45000
    max_retries_on_inconclusive: int = 1
    state_fallback: Optional[str] = "Lagos"
    submit_click_retries: int = 3
    select_retries: int = 4
    quantity_price_compensation_retries: int = 3
    price_edit_retries: int = 3
    on_progress: Callable[[str], None] = print

    dialog_reopen_retries: int = 2
    combo_click_timeout_ms: int = 12000
    option_click_timeout_ms: int = 10000
    element_visible_timeout_ms: int = 20000

    # FIX (Sep 2026): how long to wait, in a single render-settle check,
    # for an option list to stop growing/changing before concluding it's
    # done rendering. Used by _wait_for_option_render, and now also by
    # the option-selection retry loop in _open_and_pick (previously only
    # used on the search-input path).
    option_render_wait_ms: int = 1200
    overlay_gone_timeout_ms: int = 5000

    # FIX (Sep 2026): forensic capture for any dialog closure. On, by
    # default -- screenshot + a state snapshot are always written when a
    # closure is detected; the trace-chunk save additionally requires
    # run_automation.py to have called context.tracing.start()/start_chunk()
    # (best-effort -- silently skipped if tracing was never started).
    enable_closure_diagnostics: bool = True
    diagnostics_dir: str = "recordings/closure_diagnostics"

    error_selectors: tuple[str, ...] = (
        '[role="alert"]',
        ".text-destructive",
        ".text-red-500",
        "text=already exists",
        "text=validation error",
    )

    # FIX (per request, "cache selection values loaded the first time so
    # it doesn't need network/DOM reads for every order"): runtime cache,
    # not configuration -- populated as the run goes, not set up front.
    # crm_catalog (the product-name list) was already fetched once at
    # startup and reused for every order; this extends the same idea to
    # the price-offer dropdown's OWN options, which were previously
    # re-opened and re-read from the live page for every single product
    # line of every single order. A product's set of priced bundles is
    # exactly the kind of data that "doesn't usually change" during a
    # run, so the first successful read for a given product (or, in the
    # single-combo product+price layout, the one and only read there
    # will ever need to be) is kept and reused for every later order that
    # needs the same product -- no live DOM read, no page round trip.
    # See _resolve_price_decision() for where this is read/written, and
    # its cache-invalidate-and-retry-once-live fallback for the rare case
    # where the live options genuinely did change mid-run.
    price_options_cache: dict = field(default_factory=dict)
    combined_options_cache: Optional[list] = None

    # FIX (strategic speed upgrade, Priority 3 -- verify the exact state
    # needed immediately, with a short bounded micro-poll, rather than
    # gating on a broader UI signal like "the listbox is hidden"): ceiling
    # for that micro-poll, in _poll_selection_landed(). Deliberately much
    # smaller than option_render_wait_ms -- this is polling a single
    # already-open element's own text for a React state update to land,
    # not waiting out a list re-render.
    selection_landed_poll_ms: int = 200

    # PERF (Sep 2026): outer retry budget for _select_combobox's
    # _retry_on_timeout wrapper. _open_and_pick already retries
    # internally up to select_retries times per outer attempt, so the
    # old outer=select_retries nesting meant a worst case of 16 full
    # render+click cycles for one stuck selection. outer=2 (2x4=8)
    # keeps every inner retry while cutting the pathological tail;
    # see dialog_controls.py's module docstring PERF note.
    outer_select_retries: int = 2

    # FIX (strategic speed upgrade -- "measure before declaring success"):
    # run-wide counters, incremented as the run goes, surfaced at the end
    # by run_automation.py's print_performance_summary(). One dict shared
    # across every order in this AutomationConfig's lifetime (one instance
    # per browser session -- see run_automation.py's _run_session), so
    # these accumulate for the whole run, not per-order. See
    # dialog_controls.py's _bump() for how these get incremented.
    perf_counters: dict = field(default_factory=dict)

@dataclass
class OrderResult:
    success: bool
    row_number: Optional[int]
    reason: str = ""
    matched_products: list[str] = field(default_factory=list)
    needs_manual_review: bool = False
    flagged_matches: list = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

@dataclass
class FlaggedForReview:
    customer: str
    phone: str
    address: str
    product_excel_text: str
    matched_product: str
    confidence: float
    quantity: int
    intended_price: str
    reason: str
    row_number: Optional[int] = None

def _resolve_price_decision(match: MatchResult, excel_price, options: list, index: int,
                             cfg: AutomationConfig):
    """Given a product's list of priced bundle options (live-read or
    cached -- this function doesn't care which), apply decide_price_offer
    and its unresolvable branches (highlight-and-replace custom-price maneuver,
    or closest-bundle-as-is for the degenerate cases). Factored
    out of _select_product_and_price so the cached path and the live
    path (and the invalidate-and-retry-once-live fallback below) share
    one copy of this logic instead of three.

    Returns (decision, price_matched_exactly). price_matched_exactly is
    computed BEFORE any fallback/manual-review branch can reassign
    `decision` -- see _add_one_product_line for what it's used for.
    """
    decision = decide_price_offer(excel_price, options)
    price_matched_exactly = (not decision.unresolvable) and (not decision.needs_amount_edit)
    if decision.unresolvable and decision.needs_custom_price:
        # FIX (Oct 2026, "ignore the quantity entirely; highlight the price
        # and paste/replace it"): the Excel price matches NO bundle, but the
        # site accepts a custom price via the highlight-and-replace maneuver
        # (see _apply_custom_price_replace). Keep the FIRST dropdown option
        # selected -- it is the "default price first selected" the order
        # falls back to if the maneuver refuses twice -- and let
        # _add_one_product_line run the maneuver. This replaces BOTH the
        # old behaviour of silently submitting the bundle's own price AND
        # the old body-lotion-@-28,500-only prefix-edit special case.
        _emit(cfg, f"[price] '{match.matched_product}' (line {index + 1}): "
                   f"price ₦{decision.target_price} not found in the dropdown -- "
                   f"keeping first dropdown option {decision.option_label!r} "
                   f"(₦{decision.bundle_price}) selected and highlighting the "
                   f"Amount to replace it with the custom price (Quantity is "
                   f"never touched); falls back to ₦{decision.bundle_price} if "
                   f"the edit refuses twice")
        return decision, price_matched_exactly
    if decision.unresolvable:
        # unresolvable WITHOUT a typeable custom price (unparseable Excel
        # price / dropdown with no priced bundles): nothing can be typed
        # anywhere -- keep the old closest-bundle-as-is + manual-review
        # behaviour.
        _emit(cfg, f"[price] '{match.matched_product}' (line {index + 1}): "
                   f"no safe price edit found either way ({decision.reason}) -- "
                   f"selecting closest bundle {decision.option_label!r} as-is "
                   f"and flagging for manual review")
        match.needs_manual_review = True
        match.reason = (f"{match.reason + ' | ' if match.reason else ''}"
                         f"price could not be safely corrected: {decision.reason} "
                         f"-- submitted at closest bundle price ₦{decision.bundle_price} "
                         f"instead of target ₦{decision.target_price}, please verify")
        decision = replace(decision, needs_amount_edit=False, unresolvable=False)
    return decision, price_matched_exactly


def _select_product_and_price(dialog, page, index: int, match: MatchResult,
                               excel_price, cfg: AutomationConfig, state: "_ComboState",
                               row_number: Optional[int] = None):
    block = _product_block(dialog, index)
    combos = block.locator('button[role="combobox"]')
    combos.first.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)
    combo_count = combos.count()

    if combo_count >= 2:
        product_combo = combos.nth(0)
        err = _select_combobox(dialog, page, product_combo, match.matched_product, cfg,
                                f"product '{match.matched_product}' (line {index + 1})", state,
                                row_number=row_number)
        if err:
            return err

        price_combo = combos.nth(1)
        price_combo.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)
        _wait_for_combo_ready(price_combo, cfg)
        guard_err = _require_dialog_open(dialog, f"before reading price options for line {index + 1}",
                                          page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            return guard_err

        # FIX (per request, "cache selection values loaded the first
        # time... data doesn't usually change"): a product's price-offer
        # options are the same every time that product comes up during
        # this run, so read them from the live page ONCE per product and
        # reuse the cached list for every later order that needs the same
        # one -- skipping the open/wait-render/read/close round trip
        # entirely on a cache hit.
        cache_key = match.matched_product
        options = cfg.price_options_cache.get(cache_key)
        used_cache = options is not None
        if used_cache:
            _bump(cfg, "price_cache_hit")
            _emit(cfg, f"[cache] '{match.matched_product}' (line {index + 1}): "
                       f"using cached price options -- skipping live re-read")
        else:
            try:
                options = _read_combobox_options(dialog, page, price_combo, cfg, state, row_number=row_number)
            except _DialogClosed as e:
                return f"'{match.matched_product}': {e}"
            guard_err = _require_dialog_open(dialog, f"reading price options for line {index + 1}",
                                              page=page, cfg=cfg, row_number=row_number)
            if guard_err:
                return guard_err
            _bump(cfg, "price_cache_miss")
            if not options:
                return f"'{match.matched_product}': no priced bundle options found in the price-offer dropdown"
            cfg.price_options_cache[cache_key] = options

        decision, price_matched_exactly = _resolve_price_decision(match, excel_price, options, index, cfg)

        err = _select_combobox(dialog, page, price_combo, decision.option_data_value, cfg,
                                f"price offer for '{match.matched_product}' (line {index + 1})", state,
                                row_number=row_number)
        if err:
            if not used_cache:
                return err
            # FIX: the cache is a speed optimization, never a source of
            # new failures -- if a cached option fails to select (the
            # live options genuinely changed since we cached them, or a
            # one-off UI hiccup), drop the stale entry and redo this
            # product's price step with a fresh live read before giving
            # up on the order.
            _bump(cfg, "price_cache_invalidation")
            _emit(cfg, f"[cache] '{match.matched_product}' (line {index + 1}): cached price "
                       f"option failed to select ({err}) -- invalidating cache and retrying "
                       f"with a live read")
            cfg.price_options_cache.pop(cache_key, None)
            guard_err = _require_dialog_open(dialog, f"before re-reading price options for line {index + 1}",
                                              page=page, cfg=cfg, row_number=row_number)
            if guard_err:
                return guard_err
            try:
                options = _read_combobox_options(dialog, page, price_combo, cfg, state, row_number=row_number)
            except _DialogClosed as e:
                return f"'{match.matched_product}': {e}"
            if not options:
                return (f"'{match.matched_product}': no priced bundle options found in the "
                        f"price-offer dropdown after cache invalidation")
            cfg.price_options_cache[cache_key] = options
            decision, price_matched_exactly = _resolve_price_decision(match, excel_price, options, index, cfg)
            err = _select_combobox(dialog, page, price_combo, decision.option_data_value, cfg,
                                    f"price offer for '{match.matched_product}' (line {index + 1}, "
                                    f"post-cache-invalidation retry)", state, row_number=row_number)
            if err:
                return err
        return decision, price_matched_exactly

    combo = combos.first
    guard_err = _require_dialog_open(dialog, f"before reading product+price options for line {index + 1}",
                                      page=page, cfg=cfg, row_number=row_number)
    if guard_err:
        return guard_err

    # FIX (per request, caching): in this layout every product's options
    # live in the SAME single dropdown -- the whole option list is one
    # session-wide constant, read from the live page at most once ever
    # per run rather than once per order.
    all_options = cfg.combined_options_cache
    used_cache = all_options is not None
    if used_cache:
        _bump(cfg, "price_cache_hit")
        _emit(cfg, f"[cache] line {index + 1}: using cached product+price option list -- "
                   f"skipping live re-read")
    else:
        try:
            all_options = _read_combobox_options(dialog, page, combo, cfg, state, row_number=row_number)
        except _DialogClosed as e:
            return f"'{match.matched_product}': {e}"
        guard_err = _require_dialog_open(dialog, f"reading product+price options for line {index + 1}",
                                          page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            return guard_err
        _bump(cfg, "price_cache_miss")
        if not all_options:
            return (f"'{match.matched_product}': no option in the product dropdown -- "
                    f"nothing to select")
        cfg.combined_options_cache = all_options

    matching = [o for o in all_options if match.matched_product.lower() in o.label.lower()]
    if not matching:
        return (f"'{match.matched_product}': no option in the product dropdown's "
                f"{len(all_options)} option(s) matched this product name")

    decision, price_matched_exactly = _resolve_price_decision(match, excel_price, matching, index, cfg)

    err = _select_combobox(dialog, page, combo, decision.option_data_value, cfg,
                            f"product+price offer for '{match.matched_product}' (line {index + 1})", state,
                            row_number=row_number)
    if err:
        if not used_cache:
            return err
        _bump(cfg, "price_cache_invalidation")
        _emit(cfg, f"[cache] line {index + 1}: cached product+price option failed to select "
                   f"({err}) -- invalidating cache and retrying with a live read")
        cfg.combined_options_cache = None
        guard_err = _require_dialog_open(dialog, f"before re-reading product+price options for line {index + 1}",
                                          page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            return guard_err
        try:
            all_options = _read_combobox_options(dialog, page, combo, cfg, state, row_number=row_number)
        except _DialogClosed as e:
            return f"'{match.matched_product}': {e}"
        if not all_options:
            return f"'{match.matched_product}': no options found after cache invalidation"
        cfg.combined_options_cache = all_options
        matching = [o for o in all_options if match.matched_product.lower() in o.label.lower()]
        if not matching:
            return (f"'{match.matched_product}': no option in the product dropdown's "
                    f"{len(all_options)} option(s) matched this product name, after cache invalidation")
        decision, price_matched_exactly = _resolve_price_decision(match, excel_price, matching, index, cfg)
        err = _select_combobox(dialog, page, combo, decision.option_data_value, cfg,
                                f"product+price offer for '{match.matched_product}' (line {index + 1}, "
                                f"post-cache-invalidation retry)", state, row_number=row_number)
        if err:
            return err
    return decision, price_matched_exactly

def _add_one_product_line(dialog, page, index: int, match: MatchResult,
                           excel_price, cfg: AutomationConfig, state: "_ComboState",
                           row_number: Optional[int] = None) -> Optional[str]:
    if index > 0:
        err = _click_add_next_product(dialog, page, cfg, row_number=row_number)
        if err:
            return err
        try:
            _wait_stable(_product_block(dialog, index))
        except Exception:
            pass

    result = _select_product_and_price(dialog, page, index, match, excel_price, cfg, state,
                                        row_number=row_number)
    if isinstance(result, str):
        return result
    decision, price_matched_exactly = result

    # FIX (per request, "when a price matched on the selection, ignore
    # the quantity entirely and move on"): if the bundle option selected
    # already carries the exact target price with NO editing needed at
    # all (see price_matched_exactly's computation in
    # _select_product_and_price -- it is only True for a genuine match,
    # never for the forced-manual-review approximation), the bundle
    # itself already encodes the correct quantity. Typing into Quantity
    # at that point doesn't confirm anything -- it fights a value the
    # bundle selection already set, which is exactly the kind of
    # quantity/amount interaction bug bugfix #7 (the wide-edit case
    # below) was written to avoid for one specific product. This makes
    # that avoidance the default for every genuine price match, not just
    # that one case: Quantity is left completely untouched (not even a
    # read/verify) and this product line is done -- move straight on to
    # the next one.
    #
    # FIX (strategic speed upgrade, "waits some longer time before going
    # to another field"): the settle-wait on the Amount field used to run
    # unconditionally, right here, before this check -- but its only
    # purpose is letting the field's value finish updating before code
    # further down reads or edits it. In this exact-match branch, nothing
    # below ever reads or edits Amount again, so the wait's result was
    # thrown away every time this branch was taken. Moved below, into
    # only the two paths that actually go on to look at the field.
    if price_matched_exactly:
        _emit(cfg, f"[quantity] '{match.matched_product}' (line {index + 1}): "
                   f"price matched the selected bundle exactly -- leaving Quantity "
                   f"untouched and moving on")
        return None

    try:
        _wait_for_stable_value(_amount_input(dialog, index), cfg, timeout_ms=250)
    except Exception:
        pass

    if decision.needs_custom_price:
        # FIX (Oct 2026, custom-price maneuver): the selected bundle's price
        # is NOT the customer's price and no in-dropdown edit can reach it --
        # HIGHLIGHT the Amount and replace it in one atomic paste with the
        # actual custom price. The Quantity is never touched and the field is
        # never Backspaced/cleared first (Quantity and Amount are linked, and
        # clearing/Backspacing drags the Quantity down and re-derives the
        # price from the bundle -- which is why the old quantity-first edit
        # kept reverting). On a double refusal the default bundle price first
        # selected is put back with the same atomic replace and the order is
        # flagged for manual review -- exactly what used to happen here
        # unconditionally before this maneuver existed. This REPLACES the old
        # body-lotion-@-28,500-only wide-edit special case (bugfix #7), which
        # this general path now covers.
        applied, detail = _apply_custom_price_replace(
            dialog, page, index, match, decision.target_price,
            decision.bundle_price, cfg)
        guard_err = _require_dialog_open(dialog,
                                          f"custom-price maneuver for line {index + 1}",
                                          page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            return guard_err
        if applied:
            _emit(cfg, f"[price] '{match.matched_product}' (line {index + 1}): "
                       f"custom price ₦{decision.target_price} landed via the "
                       f"highlight-and-replace edit")
            return None
        match.needs_manual_review = True
        match.reason = (f"{match.reason + ' | ' if match.reason else ''}"
                         f"{detail} -- please verify")
        _emit(cfg, f"[price] '{match.matched_product}' (line {index + 1}): {match.reason}")
        return None

    qty_field = _quantity_input(dialog, index)
    qty_field.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)
    qty_err, qty_landed = _fill_quantity(qty_field, match.quantity, cfg)

    guard_err = _require_dialog_open(dialog, f"filling quantity for line {index + 1}",
                                      page=page, cfg=cfg, row_number=row_number)
    if guard_err:
        return guard_err

    landed_qty = None
    quantity_mismatch = False
    if qty_err:
        landed_qty = _safe_int(qty_landed)
        if landed_qty == match.quantity:
            _emit(cfg, f"[quantity] '{match.matched_product}' (line {index + 1}): field "
                       f"already reads the intended quantity ({match.quantity}) -- "
                       f"ignoring the earlier verify hiccup ({qty_err})")
        elif landed_qty is None or landed_qty <= 0:
            return f"'{match.matched_product}': {qty_err}"
        else:
            quantity_mismatch = True

    if match.quantity > 1:
        if quantity_mismatch:
            comp_err = _compensate_amount_for_stuck_quantity(
                dialog, page, index, match, decision, landed_qty, cfg, qty_err,
            )
            note = (f"quantity stuck at {landed_qty} (wanted {match.quantity}) -- "
                    f"{qty_err}; ")
            note += (f"price compensation also failed: {comp_err} -- submitted as-is"
                     if comp_err else "compensated the Amount to bill the correct total instead")
            match.needs_manual_review = True
            match.reason = f"{match.reason + ' | ' if match.reason else ''}{note}"
            _emit(cfg, f"[quantity] '{match.matched_product}' (line {index + 1}): {note}")
            return None

        if decision.target_price:
            err = _fill_custom_price(dialog, page, index, decision.target_price, cfg)
            if err:
                match.needs_manual_review = True
                match.reason = (f"{match.reason + ' | ' if match.reason else ''}"
                                 f"custom price fill failed: {err} -- submitted with "
                                 f"whatever price is currently showing, please verify")
                _emit(cfg, f"[price] '{match.matched_product}' (line {index + 1}): {match.reason}")
            guard_err = _require_dialog_open(dialog, f"custom price fill for line {index + 1}",
                                          page=page, cfg=cfg, row_number=row_number)
            if guard_err:
                return guard_err
        return None

    if quantity_mismatch:
        comp_err = _compensate_amount_for_stuck_quantity(
            dialog, page, index, match, decision, landed_qty, cfg, qty_err,
        )
        note = (f"quantity stuck at {landed_qty} (wanted {match.quantity}) -- "
                f"{qty_err}; ")
        note += (f"price compensation also failed: {comp_err} -- submitted as-is"
                 if comp_err else "compensated the Amount to bill the correct total instead")
        match.needs_manual_review = True
        match.reason = f"{match.reason + ' | ' if match.reason else ''}{note}"
        _emit(cfg, f"[quantity] '{match.matched_product}' (line {index + 1}): {note}")
        return None

    if decision.needs_amount_edit:
        _apply_amount_edit(dialog, page, index, match, decision, cfg)
        guard_err = _require_dialog_open(dialog, f"amount edit for line {index + 1}",
                                          page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            return guard_err

    return None

def _fill_products_screen(page, matches: list[MatchResult], order: dict,
                           cfg: AutomationConfig, combo_state: "_ComboState",
                           row_number: Optional[int] = None):
    dialog = _open_add_order_wizard(page, cfg, row_number=row_number)

    err = _select_combobox(dialog, page, dialog.locator(sel.combo_trigger(sel.OWNING_FORM_LABEL)),
                            sel.OWNING_FORM_VALUE, cfg, "Owning Form", combo_state,
                            row_number=row_number)
    if err:
        dialog_closed = _require_dialog_open(dialog, "selecting Owning Form",
                                              page=page, cfg=cfg, row_number=row_number) is not None
        return None, err, [], dialog_closed

    matched_names = []
    for i, m in enumerate(matches):
        try:
            err = _add_one_product_line(dialog, page, i, m, order.get("price"), cfg, combo_state,
                                         row_number=row_number)
        except Exception as e:
            dialog_closed = _require_dialog_open(dialog, f"product line {i + 1}",
                                                  page=page, cfg=cfg, row_number=row_number) is not None
            return None, f"product line {i + 1}: unexpected error: {e}", matched_names, dialog_closed
        if err:
            dialog_closed = _require_dialog_open(dialog, f"product line {i + 1}",
                                                  page=page, cfg=cfg, row_number=row_number) is not None
            return None, err, matched_names, dialog_closed
        matched_names.append(m.matched_product)

    return dialog, None, matched_names, False


def _fill_order_for_plan(order: dict, plan: Optional[ExecutionPlan]) -> dict:
    """FIX (Oct 2026, "modal closes on the address field"): planning.py
    already defaults a blank/NaN address to "Lagos" in plan.order -- but the
    raw order still holds the pandas NaN, which _fill_customer_info can never
    verify (an empty read-back always fails -> 3 wasted attempts -> transient
    failure -> cleanup closes the modal -> identical retry). Merge the plan's
    effective address back into the dict used for filling; every other field
    comes from the raw order untouched. Without a plan this is just `order`."""
    if plan is None:
        return order
    try:
        planned_address = plan.order.get("address", "")
    except Exception:
        planned_address = ""
    if not isinstance(planned_address, str) or not planned_address.strip():
        return order
    fill_order = dict(order)
    fill_order["address"] = planned_address
    return fill_order


def process_order(page, order: dict, cfg: AutomationConfig,
                   row_number: Optional[int] = None,
                   plan: Optional[ExecutionPlan] = None) -> OrderResult:
    # FIX (strategic speed upgrade -- "measure before declaring
    # success"): total_ms (cumulative, since order start) is recorded on
    # EVERY return below, success or failure, so a failed order still
    # reports how long it took to get where it got. The three phase
    # markers (product_lines_ms, charges_and_customer_ms, submit_ms) are
    # each the DURATION of just that phase, not a cumulative timestamp --
    # _mark_phase() tracks its own checkpoint separately from t_start so
    # phase 2's number isn't phase 1's time counted twice.
    t_start = time.perf_counter()
    t_phase = t_start
    timings: dict[str, float] = {}

    def _elapsed_ms() -> float:
        return round((time.perf_counter() - t_start) * 1000, 1)

    def _mark_phase(label: str) -> None:
        nonlocal t_phase
        now = time.perf_counter()
        timings[label] = round((now - t_phase) * 1000, 1)
        t_phase = now

    if plan is not None:
        # Planning already ran parse_order_products()/infer_state() once,
        # up front, with no browser involved -- PlanningError would have
        # been raised there if there was no sellable product. plan.state
        # may be None: that means no state was inferable from the address
        # and the state field is SKIPPED entirely (the form saves fine
        # without it) -- no fallback, no web lookup, no wasted combobox
        # interaction.
        #
        # Each MatchResult is copied via replace() before use. This
        # function is called multiple times against the *same* plan
        # object across lock-mode's transient-failure retries
        # (run_upload.py's _attempt_order loop), and mutates match
        # objects in place (needs_manual_review, reason) to record what
        # happened on this specific attempt. Without a fresh copy, a
        # failed attempt's notes would keep stacking onto the shared
        # plan's matches and bleed into the next retry.
        matches = [replace(m) for m in plan.matches]
        state = plan.state
        used_state_fallback = False
        if state is None:
            # Oct 2026 speed pass: no state inferable from the address ->
            # the state field is SKIPPED entirely and the order saves
            # without it. No fallback, no web lookup.
            _emit(cfg, f"[speed] no state inferable from address "
                       f"{order.get('address')!r} -- skipping the state "
                       f"field and saving without it")
    else:
        matches = parse_order_products(order.get("product", ""), cfg.crm_catalog)
        if not matches:
            return OrderResult(False, row_number, reason="no sellable product found in Excel product text",
                                timings_ms={"total_ms": _elapsed_ms()})

        rejected = [m for m in matches if m.matched_product is None]
        if rejected:
            reasons = "; ".join(f"{m.excel_text!r}: {m.reason}" for m in rejected)
            return OrderResult(False, row_number, reason=f"product matching failed: {reasons}",
                                timings_ms={"total_ms": _elapsed_ms()})

        # Oct 2026 speed pass: local inference only. A miss no longer fails
        # the row -- the state field is skipped and the order saves without
        # it, exactly like the planned path above.
        state, used_state_fallback = _resolve_state(order.get("address", ""), cfg)
        if state is None:
            _emit(cfg, f"[speed] no state inferable from address "
                       f"{order.get('address')!r} -- skipping the state "
                       f"field and saving without it")

    matched_names = []
    try:
        dialog = None
        err = None
        combo_state = _ComboState()
        for attempt in range(cfg.dialog_reopen_retries + 1):
            # FIX (Sep 2026): a fresh _ComboState per dialog-open attempt --
            # if we're here because the dialog closed and is about to be
            # reopened from scratch, nothing is open in the new dialog yet,
            # so any tracked trigger from the previous (now-gone) dialog
            # must not carry over.
            combo_state = _ComboState()
            dialog, err, matched_names, dialog_closed = _fill_products_screen(
                page, matches, order, cfg, combo_state, row_number=row_number
            )
            if dialog is not None:
                break
            if not dialog_closed or attempt == cfg.dialog_reopen_retries:
                break
            _bump(cfg, "dialog_recovery")
            _emit(cfg, f"[recovery] Add Order dialog closed unexpectedly "
                       f"(attempt {attempt + 1}/{cfg.dialog_reopen_retries + 1}) -- "
                       f"reopening and re-entering product lines from scratch: {err}")

        if dialog is None:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)
        _mark_phase("product_lines_ms")

        err = _click_next(dialog, page, cfg, "Products -> Charges", row_number=row_number)
        if err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)
        guard_err = _require_dialog_open(dialog, "Products -> Charges", page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=guard_err, matched_products=matched_names,
                                timings_ms=timings)

        err = _select_combobox(dialog, page, dialog.locator(sel.combo_trigger(sel.PAYMENT_METHOD_LABEL)),
                                sel.PAYMENT_METHOD_VALUE, cfg, "payment method", combo_state,
                                row_number=row_number)
        if err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)
        guard_err = _require_dialog_open(dialog, "selecting payment method", page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=guard_err, matched_products=matched_names,
                                timings_ms=timings)

        err = _select_combobox(dialog, page, dialog.locator(sel.combo_trigger(sel.ORDER_SOURCE_LABEL)),
                                sel.ORDER_SOURCE_VALUE, cfg, "order source", combo_state,
                                row_number=row_number)
        if err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)
        guard_err = _require_dialog_open(dialog, "selecting order source", page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=guard_err, matched_products=matched_names,
                                timings_ms=timings)

        err = _click_next(dialog, page, cfg, "Charges -> Customer", row_number=row_number)
        if err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)
        guard_err = _require_dialog_open(dialog, "Charges -> Customer", page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=guard_err, matched_products=matched_names,
                                timings_ms=timings)

        # FIX (Oct 2026, "modal closes on the address field"): fill from the
        # plan's effective order -- planning already defaulted blank/NaN
        # addresses to "Lagos" in plan.order, but the raw order still holds
        # NaN, which can never verify (empty read-back always fails -> 3
        # wasted attempts -> transient failure -> cleanup closes the modal ->
        # identical retry). Every other field is untouched; without a plan
        # this is just `order`.
        err = _fill_customer_info(dialog, _fill_order_for_plan(order, plan), cfg)
        if err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)

        # Oct 2026 speed pass: select the state ONLY when one was inferred.
        # When state is None, skip the combobox entirely (open + search +
        # confirm + post-selection guard all avoided) and go straight to
        # the next step -- the form saves fine without a state.
        if state is not None:
            err = _select_combobox(dialog, page, dialog.locator(sel.combo_trigger(sel.STATE_LABEL)),
                                    state, cfg, "state", combo_state, match_mode="contains",
                                    row_number=row_number)
            if err:
                timings["total_ms"] = _elapsed_ms()
                return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                    timings_ms=timings)
            guard_err = _require_dialog_open(dialog, "selecting state", page=page, cfg=cfg, row_number=row_number)
            if guard_err:
                timings["total_ms"] = _elapsed_ms()
                return OrderResult(False, row_number, reason=guard_err, matched_products=matched_names,
                                    timings_ms=timings)

        err = _click_next(dialog, page, cfg, "Customer -> Summary", row_number=row_number)
        if err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)
        guard_err = _require_dialog_open(dialog, "Customer -> Summary", page=page, cfg=cfg, row_number=row_number)
        if guard_err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=guard_err, matched_products=matched_names,
                                timings_ms=timings)

        _mark_phase("charges_and_customer_ms")
        err = _click_final_submit(dialog, cfg)
        if err:
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(False, row_number, reason=err, matched_products=matched_names,
                                timings_ms=timings)

        outcome, reason = detect_save_outcome(dialog, cfg)
        retries_left = cfg.max_retries_on_inconclusive
        while outcome == "inconclusive" and retries_left > 0:
            # FIX (Sep 2026, "no human wait in the code"): immediate
            # re-check, no sleep -- detect_save_outcome() already spent
            # up to cfg.success_wait_timeout_ms blocking on the real
            # signal (dialog hidden / error selector visible) before
            # returning "inconclusive" at all, so there is nothing
            # useful to wait for here that a fixed sleep would add.
            outcome, reason = detect_save_outcome(dialog, cfg)
            retries_left -= 1

        flagged = [m for m in matches if m.needs_manual_review]

        if outcome == "success":
            final_reason = reason
            if used_state_fallback:
                final_reason = f"{reason} | [state fallback] used {state!r} for address {order.get('address')!r}"
            _mark_phase("submit_ms")
            timings["total_ms"] = _elapsed_ms()
            return OrderResult(True, row_number, reason=final_reason, matched_products=matched_names,
                                needs_manual_review=bool(flagged), flagged_matches=flagged,
                                timings_ms=timings)

        timings["total_ms"] = _elapsed_ms()
        return OrderResult(False, row_number, reason=f"[{outcome}] {reason}",
                            matched_products=matched_names, timings_ms=timings)

    except Exception as e:
        timings["total_ms"] = _elapsed_ms()
        return OrderResult(False, row_number, reason=f"unexpected error: {e}",
                            matched_products=matched_names, timings_ms=timings)


# FIX (per request, "clean up dead code"): process_order_with_retries used
# to live here -- an alternate order-retry wrapper that was never actually
# called anywhere in run_automation.py's real flow (that flow uses
# _attempt_order's own retry loop instead) and reloaded via
# page.wait_for_load_state("networkidle"), a slow/flaky wait pattern that
# can sit at its full timeout on any page with background polling. Since
# nothing called it, it was dead weight with a slow wait baked into unused
# code -- removed outright rather than fixed, since the live retry path
# (_attempt_order in run_automation.py) already does the real job.


def to_failed_order(order: dict, result: OrderResult) -> FailedOrder:
    return FailedOrder(
        customer=_clean_text(order.get("customer_name")),
        phone=_clean_phone(order.get("phone_number")),
        address=_clean_text(order.get("address")),
        product=_clean_text(order.get("product")),
        quantity=_clean_text(order.get("quantity")),
        intended_price=_clean_text(order.get("price")),
        reason=result.reason,
        row_number=result.row_number,
    )

def to_flagged_order(order: dict, result: OrderResult) -> FlaggedForReview:
    m = result.flagged_matches[0] if result.flagged_matches else None
    return FlaggedForReview(
        customer=_clean_text(order.get("customer_name")),
        phone=_clean_phone(order.get("phone_number")),
        address=_clean_text(order.get("address")),
        product_excel_text=_clean_text(order.get("product")),
        matched_product=(m.matched_product if m else ""),
        confidence=(round(m.confidence, 3) if m else 0.0),
        quantity=(m.quantity if m else 1),
        intended_price=_clean_text(order.get("price")),
        reason=(m.reason if m else ""),
        row_number=result.row_number,
    )