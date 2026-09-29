"""
price_engine.py

------------------------------------------------------------------------
MIGRATION NOTE (Sep 2026): rewritten for sniper.ng. The old #pricevar
native <select> always offered a literal "Custom" option that could be
filled with any exact qty/price -- that option doesn't exist on the new
site. The new "price-offer" dropdown only offers a fixed list of priced
bundles, so business logic changed accordingly (confirmed directly by
the business):

  1. If a bundle's price exactly matches the Excel price -> select it.
     Done, never touch the Amount field.
  2. Otherwise -> select whichever bundle can be safely corrected to the
     Excel price by editing ONLY its trailing digits. The Amount field
     auto-fills from whichever bundle is selected.
  3. The Amount field cannot simply be cleared and retyped -- confirmed
     that fully clearing it makes it snap back to the selected bundle's
     price (some blur/empty-state listener re-syncs it). The verified
     workaround is to edit ONLY the trailing digits that actually differ
     (business's own example: 21,500 or 29,500 -> edit just the last two
     digits), never letting the field go empty at any point.
  4. This trailing-digit edit is capped at MAX_TRAILING_EDIT_DIGITS
     (default 2, matching exactly what's been verified). If NO bundle in
     the dropdown differs from the Excel price by 2 trailing digits or
     fewer, this module refuses to guess -- it reports unresolvable=True
     and add_products.py fails (or flags) that order rather than risk
     submitting a wrong price to a customer. Raise
     MAX_TRAILING_EDIT_DIGITS (and re-verify the edit still works at
     that width) if this cap turns out too tight in practice.

  UPDATE (Sep 2026, "when the price is not found in the dropdown, edit
  the quantity first, then type the actual custom price"): an
  unresolvable price is no longer a dead end. When the Excel price
  matches NO bundle, this module still reports the numerically closest
  bundle as the fallback selection, but now ALSO sets
  needs_custom_price=True -- add_products.py responds by running the
  quantity-first custom-price maneuver (fast-Backspace the Quantity,
  type 5 then 7 on the two attempts, then fast-Backspace the Amount and
  type the real price), falling back to the closest bundle's own price
  only if the site refuses twice. Only the two degenerate cases
  (unparseable Excel price, no priced bundles in the dropdown) keep the
  old "flag and submit as-is" behaviour, because there is nothing to
  type.

  OPTIMIZATION (Sep 2026, round 2 -- per Mike's "price mismatch" report):
  step 2 above used to pick a single bundle -- whichever was numerically
  CLOSEST to the Excel price -- and test ONLY that one bundle against
  the trailing-digit cap. If that one bundle happened to fail the check,
  the order was reported unresolvable even when a DIFFERENT bundle,
  still sitting right there in the same dropdown, would have passed the
  identical check.

  This under-counted resolvable orders because "numerically closest"
  and "trailing-digit-safe" are NOT the same thing whenever a digit
  carry is involved. Example: target ₦21,500 with bundles at ₦21,450
  and ₦21,599 in the dropdown -- ₦21,450 is numerically closer (50 vs
  99 naira away) but requires changing THREE trailing digits, because
  450 -> 500 carries into the hundreds place ("21450" vs "21500" first
  differ at the hundreds digit). ₦21,599 is farther in raw naira terms
  but differs from ₦21,500 in ONLY its last two digits ("99" -> "00"),
  which is exactly the edit this module has verified is safe. The old
  "pick closest, then test only that one" logic would refuse this order
  outright; it is actually completely resolvable within the existing,
  unchanged 2-digit cap.

  The fix: test EVERY priced bundle in the dropdown against the exact
  same MAX_TRAILING_EDIT_DIGITS cap that has always applied, and among
  the ones that pass, choose the bundle needing the FEWEST trailing
  digits changed (ties broken by raw naira distance, for the smallest,
  least-risky edit). This is strictly a wider search over the SAME
  safety rule -- it cannot select an edit wider than
  MAX_TRAILING_EDIT_DIGITS, it cannot ever touch a case that was
  correctly unresolvable before (see decide_price_offer's docstring),
  and an exact-price match is still always preferred first, unchanged.
------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Matches a Naira amount in an option label, e.g. "...— ₦29,500" or
# "...— N29,500" (in case the ₦ glyph doesn't render/transmit cleanly
# somewhere in the pipeline). Takes the LAST match in the label, same
# defensive reasoning as the old NGN regex: some labels show a struck-
# through original price before the real one.
_PRICE_RE = re.compile(r"[₦N]\s?([\d,]+)", re.IGNORECASE)

MAX_TRAILING_EDIT_DIGITS = 2

# The Excel price cell is NOT the same shape for every client, and the shape
# decides whether the row uploads at all:
#   * a numeric cell (openpyxl/xlsx, or a CSV pandas inferred as int64) ->
#     28500 or 28500.0
#   * a text cell -> "₦28,500", "N28,500", "#28,500", "28,500"
# client_michael's CSV arrives in the second form for EVERY row (69/69
# verified), while client_ephraim's xlsx arrives in the first. That is the
# entire reason only one client failed: decide_price_offer used to strip only
# commas, so float("₦28500") raised and the row could never resolve a price.
#
# One canonical parser, shared with product_matcher.product_for_price, so the
# two entry points can never drift apart again -- they already had (one
# stripped the currency glyph, the other did not).
#
# Anchored to the START of the value on purpose: a price is a currency token
# followed by digits, and only that leading token is removed. Stripping
# currency characters anywhere would also mangle junk like "abc" into
# something that looks parseable, which is the opposite of what we want.
_CURRENCY_PREFIX_RE = re.compile(
    r"^\s*(?:ngn|₦|n|\$|€|£|#|rupees?|rs\.?)?\s*", re.IGNORECASE)


def parse_excel_price(value) -> Optional[int]:
    """Normalize an Excel price cell to a whole-naira int, or None.

    Accepts every shape the source files actually produce:
        28500, 28500.0, "28500", "28,500", "₦28,500", "N28,500", "#28,500",
        "NGN 28,500", "₦ 28,500", and whitespace around any of those.

    Returns None (never raises) for genuinely unparseable input such as
    "abc", "", None or NaN, so callers keep their existing
    flag-for-manual-review branch instead of crashing.
    """
    if value is None:
        return None
    if isinstance(value, bool):          # bool is an int subclass; not a price
        return None
    if isinstance(value, (int, float)):
        # NaN/inf reach here from blank Excel cells read as float.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return int(round(float(value)))

    text = _CURRENCY_PREFIX_RE.sub("", str(value), count=1)
    text = text.replace(",", "").replace(" ", "").strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


@dataclass
class PriceVarOption:
    label: str
    value: str                    # the option's data-value attribute
    price: Optional[int]          # None if no parseable ₦ amount in the label


@dataclass
class PriceOfferDecision:
    option_data_value: str
    option_label: str
    bundle_price: Optional[int]
    needs_amount_edit: bool
    edit_suffix_len: int = 0      # how many trailing digits to fix, when needs_amount_edit
    target_price: int = 0
    reason: str = ""
    unresolvable: bool = False    # True -> no safe in-dropdown edit exists for this price
    # FIX (Oct 2026, "prices not found in the price dropdown"): when
    # unresolvable is set because the Excel price matches NO bundle, the
    # order is no longer a dead end -- needs_custom_price tells
    # add_products.py to run the highlight-and-replace custom-price maneuver
    # (highlight the Amount, replace it in one atomic paste -- the Quantity
    # is never touched; see field_values._apply_custom_price_replace), with
    # the closest bundle as the fallback. It stays False for the two
    # degenerate unresolvable cases (unparseable Excel price / no priced
    # bundles in the dropdown) where there is nothing to type -- those
    # keep the old closest-bundle-as-is + manual-review behaviour.
    needs_custom_price: bool = False


def parse_pricevar_option(label: str, value: str) -> PriceVarOption:
    matches = _PRICE_RE.findall(label)
    price = int(matches[-1].replace(",", "")) if matches else None
    return PriceVarOption(label=label, value=value, price=price)


def _trailing_diff_len(bundle_price: int, target_price: int,
                        max_digits: int = MAX_TRAILING_EDIT_DIGITS) -> Optional[int]:
    """How many trailing digits differ between the two prices, treated as
    equal-width digit strings. Returns None (unresolvable via THIS
    bundle) if:
      - any digit OUTSIDE the trailing `max_digits` differs (this isn't a
        simple trailing-digit edit, it's a different price entirely --
        including cases where a digit CARRY, e.g. ...450 -> ...500,
        makes a numerically small difference require changing a digit
        well before the last `max_digits` positions), or
      - the required edit is wider than `max_digits`.
    Returns 0 if the prices are already identical (callers normally
    handle the exact-match case earlier and won't hit this path, but
    it's a safe no-op if they ever do).

    This function's cap and semantics are UNCHANGED from before the
    round-2 optimization -- what changed is that decide_price_offer now
    calls this once per candidate bundle instead of once for a single
    pre-selected one. The safety boundary enforced here is identical
    either way.
    """
    a, b = str(bundle_price), str(target_price)
    width = max(len(a), len(b))
    a, b = a.zfill(width), b.zfill(width)

    first_diff = next((i for i in range(width) if a[i] != b[i]), width)
    diff_len = width - first_diff
    if diff_len > max_digits:
        return None
    return diff_len


def decide_price_offer(excel_price, options: list[PriceVarOption]) -> PriceOfferDecision:
    """Business rule, applied to whatever priced bundles the price-offer
    dropdown actually shows for this product right now (never a static
    price list).

    Selection order:
      1. Exact price match -> select it, never touch Amount. (unchanged)
      2. No exact match -> among ALL priced bundles, find every one whose
         price differs from the Excel price by AT MOST
         MAX_TRAILING_EDIT_DIGITS trailing digits (the one and only edit
         this module has verified is safe on this site). Among those
         survivors, pick the one needing the FEWEST digits changed --
         ties broken by raw naira distance -- since fewer keystrokes on
         a live field is strictly lower-risk for the same outcome.
      3. If NO bundle survives that filter, this is genuinely
         unresolvable: report it against the numerically closest bundle
         (for a clear, human-readable reason and a sane fallback
         selection) with unresolvable=True, exactly as before. Nothing
         is ever guessed or forced past the verified cap.
    """
    target = parse_excel_price(excel_price)
    if target is None:
        return PriceOfferDecision(
            option_data_value="", option_label="", bundle_price=None,
            needs_amount_edit=False, unresolvable=True,
            reason=f"could not parse Excel price {excel_price!r}",
        )

    priced = [o for o in options if o.price is not None]
    if not priced:
        return PriceOfferDecision(
            option_data_value="", option_label="", bundle_price=None,
            needs_amount_edit=False, unresolvable=True,
            reason="no priced bundle options were found in the price-offer dropdown",
        )

    # ---- 1. Exact match -- select it, never touch Amount. ----
    for opt in priced:
        if opt.price == target:
            return PriceOfferDecision(
                option_data_value=opt.value, option_label=opt.label,
                bundle_price=opt.price, needs_amount_edit=False,
                target_price=target,
                reason=f"exact price match: {opt.label!r}",
            )

    # ---- 2. No exact match -- search EVERY bundle for one that passes
    # the same trailing-digit safety cap that has always applied, and
    # keep the cheapest (fewest-digit) safe edit among survivors.
    #
    # (diff_len, abs_distance, option) so min() naturally prefers fewer
    # digits changed first, then the closer naira amount as a tiebreak.
    candidates: list[tuple[int, int, PriceVarOption]] = []
    for opt in priced:
        diff_len = _trailing_diff_len(opt.price, target)
        if diff_len is not None:
            candidates.append((diff_len, abs(opt.price - target), opt))

    if candidates:
        best_diff_len, _, best = min(candidates, key=lambda c: (c[0], c[1]))
        return PriceOfferDecision(
            option_data_value=best.value, option_label=best.label,
            bundle_price=best.price, needs_amount_edit=(best_diff_len > 0),
            edit_suffix_len=best_diff_len, target_price=target,
            reason=(f"no exact match -- using bundle {best.label!r} "
                    f"(₦{best.price}), editing last {best_diff_len} digit(s) "
                    f"of Amount to reach ₦{target} (chosen from "
                    f"{len(candidates)} dropdown option(s) that could safely "
                    f"reach this price within {MAX_TRAILING_EDIT_DIGITS} "
                    f"trailing digit(s))") if best_diff_len > 0 else
                   f"bundle {best.label!r} already equals target ₦{target}",
        )

    # ---- 3. Nothing in the dropdown can safely reach this price. ----
    # FIX (Oct 2026, "for every product whose price is not found, ALWAYS
    # select the FIRST price dropdown option -- don't go looking for a
    # related or pre-defined price"): deliberate simplification of this
    # branch. When the Excel price matches no bundle, take the FIRST priced
    # option in live dropdown order (priced[0]) -- no closest-price search,
    # no pre-defined mapping. The highlight-and-replace maneuver then writes
    # the real custom price into the Amount, and this first option's own
    # price is the value the order falls back to if the maneuver refuses
    # twice. Exact-match (step 1) and the <=2-digit trailing edit (step 2)
    # are untouched -- they still run first and still win when they apply.
    first = priced[0]
    return PriceOfferDecision(
        option_data_value=first.value, option_label=first.label,
        bundle_price=first.price, needs_amount_edit=True,
        target_price=target, unresolvable=True, needs_custom_price=True,
        reason=(f"checked all {len(priced)} priced bundle(s) in the dropdown -- "
                f"none differs from target ₦{target} by {MAX_TRAILING_EDIT_DIGITS} "
                f"trailing digit(s) or fewer; keeping the FIRST dropdown option "
                f"{first.label!r} at ₦{first.price} selected and replacing the "
                f"Amount with the custom price"),
    )