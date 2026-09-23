"""Pure order planning for the fast upload engine.

Planning deliberately has no Playwright dependency.  All deterministic work
is completed once, before a browser worker touches the order.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from apps.sniper.product_upload.product_matcher import (
    MatchResult, parse_order_products, product_for_price,
)
from apps.sniper.product_upload.state_engine import infer_state_with_search


@dataclass(frozen=True)
class ExecutionPlan:
    order: Mapping[str, object]
    matches: tuple[MatchResult, ...]
    state: str
    used_state_fallback: bool = False

    @property
    def matched_products(self) -> tuple[str, ...]:
        return tuple(m.matched_product or "" for m in self.matches)


class PlanningError(ValueError):
    """A deterministic input error that must never be retried."""


def build_execution_plan(
    order: Mapping[str, object],
    catalog: list[str],
    state_fallback: Optional[str] = "Lagos",
) -> ExecutionPlan:
    matches = tuple(parse_order_products(order.get("product", ""), catalog))
    if not matches or any(m.matched_product is None for m in matches):
        # LAST RESORT (Oct 2026, "price can tell the product"): the row's text
        # produced nothing usable (aliases + fuzzy already ran inside
        # parse_order_products), so infer the product from the row's PRICE.
        # Never overrides a real hit -- only fills a hole -- and the result is
        # flagged for manual review.
        inferred = product_for_price(order.get("price"), catalog)
        if inferred is not None:
            matches = (inferred,)
        elif not matches:
            raise PlanningError("no sellable product found in Excel product text")
        else:
            rejected = [m for m in matches if m.matched_product is None]
            reasons = "; ".join(f"{m.excel_text!r}: {m.reason}" for m in rejected)
            raise PlanningError(f"product matching failed: {reasons}")

    # Address default (Oct 2026): a blank address has nothing to infer from, so
    # use "Lagos" for BOTH the address field and the state instead of failing
    # the row with "could not infer state". The browser then fills a real
    # delivery address rather than an empty box.
    effective_order = dict(order)
    raw_address = effective_order.get("address", "")
    address = raw_address
    used_address_default = False
    if not isinstance(address, str) or not address.strip():
        address = "Lagos"
        effective_order["address"] = address
        used_address_default = True

    # State: local inference first (dictionaries/aliases/landmarks -- no
    # network); only when that misses does infer_state_with_search() run its
    # light, 2-attempt web lookup. Anything unresolved still lands on
    # state_fallback ("Lagos") exactly as before.
    state = infer_state_with_search(address)
    used_fallback = False
    if state is None:
        if state_fallback is None:
            raise PlanningError(
                f"could not infer state from address {address!r}"
            )
        state = state_fallback
        used_fallback = True
    elif used_address_default:
        used_fallback = True

    return ExecutionPlan(
        order=MappingProxyType(effective_order),
        matches=matches,
        state=state,
        used_state_fallback=used_fallback,
    )
