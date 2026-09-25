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
from apps.sniper.product_upload.state_engine import infer_state_memoized


@dataclass(frozen=True)
class ExecutionPlan:
    order: Mapping[str, object]
    matches: tuple[MatchResult, ...]
    # None = no state could be inferred from the address -> the caller
    # SKIPS the state field entirely and saves (the form allows it).
    state: Optional[str]
    used_state_fallback: bool = False

    @property
    def matched_products(self) -> tuple[str, ...]:
        return tuple(m.matched_product or "" for m in self.matches)


class PlanningError(ValueError):
    """A deterministic input error that must never be retried."""


class MissingAddressError(PlanningError):
    """The address cell is blank: there is nothing to infer a state from and
    nothing to type into the address field. The caller (run_upload.py) flags
    this for a human on the first sighting and proceeds with its own Lagos
    default on the second -- planning itself never guesses."""


def build_execution_plan(
    order: Mapping[str, object],
    catalog: list[str],
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

    # Address default (Oct 2026): a blank address has nothing to infer from.
    # "Lagos" is kept as the typed fill value (so the address box gets a
    # real, verifiable string instead of pandas NaN) but planning itself
    # raises MissingAddressError so the caller can flag it for a human first.
    effective_order = dict(order)
    raw_address = effective_order.get("address", "")
    address = raw_address
    if not isinstance(address, str) or not address.strip():
        effective_order["address"] = "Lagos"
        raise MissingAddressError(
            "address cell is blank -- nothing to infer a state from and "
            "nothing to type into the address field"
        )

    # State: local inference only (dictionaries/aliases/landmarks -- no
    # network, no web lookup, no Lagos fallback). A miss leaves state=None
    # and the caller SKIPS the state field entirely and saves.
    state = infer_state_memoized(address)

    return ExecutionPlan(
        order=MappingProxyType(effective_order),
        matches=matches,
        state=state,
        used_state_fallback=False,
    )
