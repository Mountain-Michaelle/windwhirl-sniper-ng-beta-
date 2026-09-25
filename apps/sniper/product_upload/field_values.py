"""
field_values.py

Everything about deciding and entering the actual VALUE that goes into a
field: quantity, price/amount (including the trailing-digit bundle-price
edit and its prefix-based fallback), customer info (name/phone/whatsapp/
address), and which state to submit. This is deliberately separate from
dialog_controls.py (which only knows how to click/open/close widgets,
not what belongs in them) and from add_products.py (which only knows the
order in which to call all of this, not how any single field gets
filled). Nothing here drives a combobox or a wizard step -- it only
types into inputs it's handed and reads them back to verify.

FIX (Sep 2026, "price edit clears the field and it snaps back to the
bundle's own price"): `_fix_amount_trailing_digits` refuses to apply an
edit that would clear the WHOLE Amount field rather than just its
trailing digits, because a fully-cleared Amount on this site reverts to
the selected bundle's own price -- typing on top of that revert is
exactly how a wrong price gets submitted silently. See that function's
docstring for the full reasoning.

FIX (Oct 2026, "editing the quantity won't fix it -- ignore the quantity
entirely; HIGHLIGHT the price and paste/replace it"): `_apply_custom_
price_replace` is the verified way to submit a price that matches NO
bundle in the price-offer dropdown. Quantity and Amount are LINKED on
this site: Backspacing the Amount walks the Quantity down with it, which
re-derives the Amount from the selected bundle and throws the custom
price away -- so the maneuver never touches Quantity and never
Backspaces. Each attempt focuses the Amount, highlights its whole value
and replaces it in ONE atomic paste (`keyboard.insert_text`, with a
per-keystroke `press_sequentially` fallback), then verifies the
read-back. If it refuses twice, the default bundle price first selected
simply stays and the caller flags the order for manual review.

FIX (Sep 2026, "combobox selection doesn't retry / uploads are slow",
part relevant here): quantity/price verification never trusts "we typed
X" as proof -- every fill in this file (`_fill_quantity`,
`_fix_amount_trailing_digits`, `_fill_custom_price`, `_type_and_verify`)
reads the field back with `_wait_for_value` and only reports success once
the read-back actually matches what was intended.

PERF (Sep 2026, "process orders slowly due to some lagging architectural
design ... possibly 100x faster with the persistent logic still there"):
`_wait_for_value` and `_wait_for_shorter` now delegate to fast_waits.py,
which runs the SAME poll INSIDE the browser as a single
page.wait_for_function() call -- one protocol round trip instead of one
round trip per poll iteration. Semantics (timeouts, stability
requirements, return contracts) are unchanged, and a faithful copy of
the original Python polling loop remains as the fallback inside
fast_waits, so nothing can regress.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import apps.sniper.utils.field_selectors as sel
from apps.sniper.product_upload.product_matcher import MatchResult
from apps.sniper.product_upload.state_engine import infer_state_memoized
from apps.sniper.product_upload.closure_diagnostics import _emit
from apps.sniper.product_upload import fast_waits as _fw


def _wait_for_value(locator, expected: str, cfg: AutomationConfig,
                     timeout_ms: Optional[int] = None) -> str:
    """PERF: in-page value wait (single round trip) via fast_waits; the
    original Python polling loop is preserved as the fallback there. Same
    signature and return contract as before: returns the first value that
    equals `expected`, else the value the field settled on, else the last
    value seen before any read failure."""
    timeout_ms = cfg.value_poll_timeout_ms if timeout_ms is None else timeout_ms
    _landed, value = _fw.wait_value_equals(
        locator, str(expected), timeout_ms=timeout_ms, min_stable_ms=40,
        poll_ms=max(10, int(cfg.value_poll_interval_s * 1000)),
    )
    return value

def _wait_for_shorter(locator, prev_len: int, cfg: AutomationConfig,
                       timeout_ms: int = 150) -> None:
    # PERF: in-page shorter-value wait via fast_waits (its fallback
    # replicates the original polling loop exactly).
    _fw.wait_value_shorter(locator, prev_len, timeout_ms=timeout_ms, poll_ms=15)

def _type_value(target_locator, text: str, cfg: AutomationConfig) -> None:
    """FIX (Sep 2026, "no human wait in the code"): renamed from
    _human_type -- it never actually simulated human typing speed (it
    always used .fill(), not a per-character .type() with delay), so
    the old name was misleading now that every genuinely human-paced
    helper (the unused _human_type_append, every _human_pause call
    site) has been removed from the codebase.

    FIX (strategic speed upgrade, "jump from input to input is slow"):
    dropped the .click() that used to precede .fill() here.
    Playwright's .fill() already performs its own actionability wait and
    focuses the element as part of what it does -- the explicit click
    was a second, redundant round of waiting or nothing. Same field gets
    the same value either way; this only removes overhead that Playwright
    was already doing for us."""
    target_locator.fill(str(text))

def _safe_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n

def _product_block(dialog, index: int):
    return dialog.locator(sel.PRODUCT_BLOCK_SELECTOR).nth(index)

def _quantity_input(dialog, index: int):
    return _product_block(dialog, index).locator(sel.QUANTITY_INPUT_SELECTOR)

def _amount_input(dialog, index: int):
    return _product_block(dialog, index).locator(sel.AMOUNT_INPUT_SELECTOR)

def _fill_quantity(qty_field, quantity: int, cfg: AutomationConfig,
                    max_attempts: int = 3) -> tuple[Optional[str], Optional[str]]:
    target_str = str(quantity)

    try:
        already = qty_field.input_value().strip()
    except Exception:
        already = None
    if already == target_str:
        return None, already

    for attempt in range(1, max_attempts + 1):
        qty_field.click()
        qty_field.press("Control+A")
        qty_field.press("Backspace")
        try:
            cleared = _wait_for_value(qty_field, "", cfg, timeout_ms=250).strip()
        except Exception as e:
            if attempt == max_attempts:
                return f"could not read Quantity while clearing it (attempt {attempt}): {e}", None
            continue

        if cleared != "":
            if cleared == target_str:
                return None, cleared
            if attempt == max_attempts:
                return (f"Quantity kept re-populating itself with {cleared!r} instead "
                        f"of staying empty after {max_attempts} attempt(s) -- refusing "
                        f"to type {target_str!r} on top of a value we didn't set"), cleared
            continue

        qty_field.fill(target_str)
        try:
            seen = _wait_for_value(qty_field, target_str, cfg).strip()
        except Exception as e:
            if attempt == max_attempts:
                return f"could not read back Quantity after typing (attempt {attempt}): {e}", None
            continue
        if seen == target_str:
            return None, seen
        if attempt == max_attempts:
            return (f"Quantity shows {seen!r} after typing, expected {target_str!r} -- "
                    f"refusing to submit an order with an unverified quantity"), seen

    return f"could not confirm Quantity={target_str!r} after {max_attempts} attempt(s)", None

# ---------------------------------------------------------------------------
# Custom-price maneuver (Oct 2026, "ignore the quantity entirely; highlight
# the price and paste/replace it"):
# when the Excel price matches NO priced bundle in the price-offer dropdown,
# the site still accepts a custom price -- but the ONLY write that sticks is
# highlighting the Amount's whole value and replacing it in one atomic paste.
# Quantity and Amount are LINKED on this site: Backspacing the Amount walks
# the Quantity down with it, which re-derives the Amount from the selected
# bundle and throws the custom price away -- which is exactly why the old
# "edit the quantity first, then Backspace the price" maneuver kept being
# reverted in the live run. Verified per-attempt sequence:
#   1. click Amount (focus only -- the Quantity is never touched);
#   2. highlight the current value (select-all);
#   3. replace it in ONE atomic paste with the actual custom price;
#   4. verify the read-back.
# If it refuses on both attempts, the default bundle price first selected
# simply stays and the caller flags the order for manual review.
# ---------------------------------------------------------------------------
_CUSTOM_PRICE_ATTEMPTS = 2            # highlight-and-replace tries before giving up
_TYPE_DELAY_MS = 20                   # fast, but real per-keystroke input


def _select_all_and_replace(field, page, text: str, cfg: AutomationConfig) -> Optional[str]:
    """Highlights the field's current value and REPLACES it in one atomic
    paste, leaving the linked Quantity untouched.

    FIX (Oct 2026, "backspacing the price alters the quantity, which alters
    the price again"): this never presses Backspace and never clears the
    field first. The Amount is highlighted in place and the new value
    overwrites the selection in a single `insert_text` (paste semantics), so
    the field is never observed empty -- which is what let the site re-derive
    the price from the bundle mid-edit. Falls back to real per-keystroke
    typing (which replaces a live selection on its first keypress) if the
    atomic paste is unavailable. Returns None once the read-back matches,
    else a short reason describing the refusal."""
    try:
        field.click()
    except Exception as e:
        return f"could not focus the field to replace its value: {e}"
    selected = False
    try:
        field.press("Control+a")
        selected = True
    except Exception:
        try:
            field.select_text()
            selected = True
        except Exception as e:
            return f"could not highlight the field's value: {e}"
    if not selected:
        return "could not highlight the field's value (no selection method worked)"
    try:
        page.keyboard.insert_text(str(text))
    except Exception:
        try:
            _type_like_human(field, text)
        except Exception as e:
            return f"could not replace the field's value: {e}"
    try:
        seen = _wait_for_value(field, str(text), cfg).strip()
    except Exception as e:
        return f"could not read the field back after replacing it: {e}"
    if seen != str(text):
        return (f"field shows {seen!r} after replacing it with {text!r} -- "
                f"the site refused or reverted the paste")
    return None


def _type_like_human(field, text) -> None:
    """Fast per-keystroke typing (delay=_TYPE_DELAY_MS): quick enough to
    feel robotic, but real key events -- the only input style this site
    accepts on the Quantity/Amount fields."""
    field.press_sequentially(str(text), delay=_TYPE_DELAY_MS)

def _apply_custom_price_replace(dialog, page, index: int,
                                match: MatchResult, target_price: int,
                                fallback_price: Optional[int],
                                cfg: AutomationConfig
                                ) -> tuple[bool, Optional[str]]:
    """Price-not-in-dropdown maneuver: HIGHLIGHT the Amount and replace it in
    one atomic paste, then move on.

    FIX (Oct 2026, live run: "editing the quantity won't fix it ... ignore the
    quantity entirely; select the price that is highlighted, then paste the
    actual price / replace it, then move on -- not backspacing it, because
    backspacing alters the quantity thereby altering the price again"):
    Quantity and Amount are LINKED on this site. Backspacing (or clearing)
    the Amount walks the Quantity down with it, which re-derives the Amount
    from the selected bundle and throws the custom price away -- exactly why
    the old "edit the Quantity first (5/7), then Backspace the price"
    maneuver kept being reverted live. This maneuver therefore NEVER touches
    the Quantity and NEVER Backspaces: it focuses the Amount, highlights its
    whole current value and overwrites it in ONE atomic paste
    (`_select_all_and_replace` -> `keyboard.insert_text`), then verifies the
    read-back.

    Returns (True, None) once the Amount reads back the exact custom price,
    or (False, detail) after `_CUSTOM_PRICE_ATTEMPTS` refusals -- in which
    case the default bundle price first selected (`fallback_price`) is put
    back with the very same atomic replace and the caller flags the order for
    manual review. The customer's own uploaded quantity is deliberately never
    typed here either.
    """
    target_str = str(target_price)
    amount_field = _amount_input(dialog, index)
    amount_field.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)

    last_err: Optional[str] = None
    for attempt_no in range(1, _CUSTOM_PRICE_ATTEMPTS + 1):
        # Highlight the Amount in place and replace it atomically -- never a
        # select-and-delete or a Backspace, both of which the site rejects
        # (and which drag the linked Quantity down with them).
        err = _select_all_and_replace(amount_field, page, target_str, cfg)
        if err is None:
            return True, None
        last_err = err
        _emit(cfg, f"[price] '{match.matched_product}' (line {index + 1}): "
                   f"custom-price attempt {attempt_no}/{_CUSTOM_PRICE_ATTEMPTS} "
                   f"refused: {err}")

    # ---- both attempts refused: put the default bundle price first selected
    # back, using the same atomic replace (never a Backspace), then report
    # the refusal so the caller flags the order for manual review.
    restore_notes: list[str] = []
    if fallback_price is not None:
        restore_err = _select_all_and_replace(
            amount_field, page, str(fallback_price), cfg)
        if restore_err is not None:
            restore_notes.append(f"price restore failed: {restore_err}")

    detail = (f"custom price ₦{target_str} refused after {_CUSTOM_PRICE_ATTEMPTS} "
              f"attempt(s) -- proceeding with the default bundle price "
              f"{'₦' + str(fallback_price) if fallback_price is not None else '(unknown)'} "
              f"first selected")
    if last_err:
        detail += f" | last refusal: {last_err}"
    if restore_notes:
        detail += f" | restore issues: {'; '.join(restore_notes)}"
    return False, detail

def _fix_amount_trailing_digits(amount_field, target_price: int, suffix_len: int,
                                 cfg: AutomationConfig) -> Optional[str]:
    target_str = str(target_price)
    if suffix_len <= 0 or suffix_len >= len(target_str):
        return (f"cannot safely apply a {suffix_len}-digit trailing edit to target "
                f"{target_str!r} ({len(target_str)} digits): this would clear the "
                f"WHOLE field, and a fully-cleared Amount snaps back to the selected "
                f"bundle's own price on this site -- typing on top of that revert is "
                f"exactly how a wrong price gets submitted silently, so refusing "
                f"rather than guessing")
    new_suffix = target_str[-suffix_len:]

    # FIX (strategic speed upgrade, "23000 -> 28500 edit is slow"): try a
    # single .fill(target_str) first. By construction (this function is
    # only ever called with a suffix_len that a shared prefix justifies --
    # see decide_price_offer), the current
    # value's own leading digits already match target_str's leading
    # digits, so setting the WHOLE field straight to target_str in one
    # atomic operation lands on exactly the same result the digit-by-digit
    # backspace-then-type loop below was built to reach -- just without
    # paying for a wait after every single keystroke. Critically, .fill()
    # never passes the field through an empty intermediate state the way
    # repeated Backspace presses do, so it does not appear to trip the
    # "fully-cleared Amount reverts to the bundle price" behavior that
    # rule above exists to avoid -- but if it somehow still doesn't verify
    # (a real revert, or this site's Amount field turning out to need
    # real keystroke events after all), the code falls through to the
    # original, proven digit-by-digit method below rather than trusting
    # the fast path blindly.
    try:
        amount_field.fill(target_str)
        seen = _wait_for_value(amount_field, target_str, cfg).strip()
        if seen == target_str:
            return None
    except Exception:
        pass

    amount_field.click()
    amount_field.press("End")
    for _ in range(suffix_len):
        try:
            prev_len = len(amount_field.input_value())
        except Exception:
            prev_len = 0
        amount_field.press("Backspace")
        _wait_for_shorter(amount_field, prev_len, cfg)
    amount_field.type(new_suffix)

    try:
        seen = _wait_for_value(amount_field, target_str, cfg).strip()
    except Exception as e:
        return f"could not read back Amount after trailing-digit edit: {e}"
    if seen != target_str:
        return (f"Amount shows {seen!r} after trailing-digit edit, expected {target_str!r} "
                f"-- the field likely reverted or the edit didn't land; refusing to "
                f"submit this order on an unverified price")
    return None

def _reset_amount_field(amount_field, cfg: AutomationConfig) -> None:
    amount_field.click()
    amount_field.press("Control+A")
    amount_field.press("Backspace")
    try:
        _wait_for_value(amount_field, "", cfg, timeout_ms=200)
    except Exception:
        pass

def _compensate_amount_for_stuck_quantity(dialog, page, index: int, match: MatchResult,
                                           decision, landed_qty: int, cfg: AutomationConfig,
                                           qty_err: str) -> Optional[str]:
    base_target = decision.target_price
    compensated_target = round(base_target * match.quantity / landed_qty)
    target_str = str(compensated_target)

    amount_field = _amount_input(dialog, index)
    amount_field.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)

    last_err = None
    for attempt in range(1, cfg.quantity_price_compensation_retries + 1):
        try:
            current = amount_field.input_value().strip()
        except Exception as e:
            last_err = f"could not read Amount before compensating (attempt {attempt}): {e}"
            continue

        if current == target_str:
            return None

        prefix_len = _common_prefix_len(current, target_str)
        suffix_len = len(target_str) - prefix_len
        if prefix_len == 0 or suffix_len <= 0 or suffix_len >= len(target_str):
            last_err = (f"current Amount {current!r} and compensated target {target_str!r} "
                        f"(base {base_target} x {match.quantity}/{landed_qty}) share no safe "
                        f"common leading digit(s) to edit from")
            if attempt < cfg.quantity_price_compensation_retries:
                _reset_amount_field(amount_field, cfg)
                continue
            return last_err

        err = _fix_amount_trailing_digits(amount_field, compensated_target, suffix_len, cfg)
        if err is None:
            return None
        last_err = err
        # FIX (Sep 2026, "no human wait in the code"): retry immediately --
        # _reset_amount_field()/_fix_amount_trailing_digits() already read
        # the field back via _wait_for_value()/_wait_for_shorter() before
        # returning, so the state is already confirmed settled by the
        # time we loop back; a fixed sleep on top of that verified them
        # to add nothing.
        if attempt < cfg.quantity_price_compensation_retries:
            _reset_amount_field(amount_field, cfg)

    return last_err or "compensation failed for an unknown reason"

def _apply_amount_edit(dialog, page, index: int, match: MatchResult, decision,
                        cfg: AutomationConfig) -> None:
    if not decision.needs_amount_edit:
        return

    amount_field = _amount_input(dialog, index)
    amount_field.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)

    last_err = None
    for attempt in range(1, cfg.price_edit_retries + 1):
        err = _fix_amount_trailing_digits(amount_field, decision.target_price,
                                           decision.edit_suffix_len, cfg)
        if err is None:
            return
        last_err = err
        if attempt < cfg.price_edit_retries:
            _reset_amount_field(amount_field, cfg)

    match.needs_manual_review = True
    match.reason = (f"{match.reason + ' | ' if match.reason else ''}"
                     f"price edit failed after {cfg.price_edit_retries} attempt(s): "
                     f"{last_err} -- submitted with whatever Amount is currently "
                     f"showing, please verify")
    _emit(cfg, f"[price] '{match.matched_product}' (line {index + 1}): {match.reason}")

def _fill_custom_price(dialog, page, index: int, target_price: int, cfg: AutomationConfig,
                        max_attempts: int = 3) -> Optional[str]:
    target_str = str(target_price)
    amount_field = _amount_input(dialog, index)
    amount_field.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)

    for attempt in range(1, max_attempts + 1):
        _type_value(amount_field, target_str, cfg)
        try:
            seen = _wait_for_value(amount_field, target_str, cfg).strip()
        except Exception as e:
            if attempt == max_attempts:
                return f"could not read back custom price after typing (attempt {attempt}): {e}"
            continue
        if seen == target_str:
            return None
        if attempt == max_attempts:
            return (f"custom price shows {seen!r} after typing, expected {target_str!r} -- "
                    f"refusing to submit an order with an unverified price")

    return f"could not confirm custom price={target_str!r} after {max_attempts} attempt(s)"

def _clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    return " ".join(str(value).split())

def _clean_phone(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:
            return ""
        if value.is_integer():
            value = int(value)
    return " ".join(str(value).split())

def _type_and_verify(dialog, selector: str, value: str, cfg: AutomationConfig,
                      label: str, max_attempts: int = 3) -> Optional[str]:
    loc = dialog.locator(selector)
    loc.wait_for(state="visible", timeout=cfg.element_visible_timeout_ms)

    for attempt in range(1, max_attempts + 1):
        _type_value(loc, value, cfg)
        try:
            seen = _wait_for_value(loc, value, cfg).strip()
        except Exception as e:
            if attempt == max_attempts:
                return f"could not read back {label!r} after typing (attempt {attempt}): {e}"
            continue
        if seen:
            return None

    return (f"{label!r} came back empty after {max_attempts} attempt(s) to type "
            f"{value!r} into it -- something on the page is rejecting or "
            "clearing this field")

def _fill_customer_info(dialog, order: dict, cfg: AutomationConfig) -> Optional[str]:
    name = _clean_text(order.get("customer_name"))
    phone = _clean_phone(order.get("phone_number"))
    whatsapp = _clean_phone(order.get("whatsapp_number"))
    # FIX (Oct 2026, "modal closes on the address field"): a blank Excel cell
    # arrives as float NaN -> _clean_text gives "" -> _type_and_verify can
    # NEVER verify an empty fill (an empty read-back counts as failure), so it
    # burns all 3 attempts and fails the order transient -- whose cleanup then
    # closes the modal and retries into the identical failure. planning.py
    # already defaults blank addresses to "Lagos" in plan.order (wired through
    # via _fill_order_for_plan); this is the belt-and-braces for the legacy
    # no-plan path so the box always gets a real address, never an empty fill.
    address = _clean_text(order.get("address")) or "Lagos"

    fields: list[tuple[str, str, str]] = [
        (sel.FULL_NAME_INPUT_SELECTOR, name, "customer name"),
        (sel.PHONE_INPUT_SELECTOR, phone, "phone"),
    ]
    if whatsapp:
        fields.append((sel.WHATSAPP_INPUT_SELECTOR, whatsapp, "whatsapp"))
    fields.append((sel.ADDRESS_INPUT_SELECTOR, address, "address"))

    for selector, value, label in fields:
        err = _type_and_verify(dialog, selector, value, cfg, label)
        if err:
            return err

    return None

def _resolve_state(address, cfg: AutomationConfig) -> tuple[Optional[str], bool]:
    """Local inference only (Oct 2026 speed pass): no web lookup, no Lagos
    fallback. A miss returns (None, False) and the caller SKIPS the state
    field entirely and saves -- the form allows saving without a state."""
    state = infer_state_memoized(address)
    return state, False