from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.oms.application.models.parsed_order import ParsedOrder
    from app.oms.application.models.validation_report import ValidationReport


@dataclass
class ValidatedOrder:
    '''
    A ParsedOrder paired with its ValidationReport.

    The ParsedOrder is NEVER modified — it remains exactly as
    the Day 7 parser produced it. All validation results live
    in the ValidationReport.

    Downstream consumers (Day 9 duplicate detection, Day 10 storage)
    read both the parsed data and the validation report.

    Attributes:
        validated_id:    Unique ID for this ValidatedOrder.
        parsed_order:    Original ParsedOrder — immutable.
        report:          Complete ValidationReport.
        validated_at:    When validation completed.
    '''
    parsed_order:  "ParsedOrder"
    report:        "ValidationReport"
    validated_id:  str      = field(default_factory=lambda: str(uuid.uuid4())[:8])
    validated_at:  datetime = field(default_factory=datetime.now)

    # ── Convenience pass-throughs ───────────────────────────────
    # These delegate to parsed_order for clean consumer access
    # without requiring consumers to know the nested structure.

    @property
    def order_id(self) -> str:
        return self.parsed_order.order_id

    @property
    def customer_name(self):
        return self.parsed_order.customer_name

    @property
    def phone_number(self):
        return self.parsed_order.phone_number

    @property
    def whatsapp_number(self):
        return self.parsed_order.whatsapp_number

    @property
    def package(self):
        return self.parsed_order.package

    @property
    def delivery_address(self):
        return self.parsed_order.delivery_address

    @property
    def is_valid(self) -> bool:
        return self.report.is_valid

    @property
    def quality_score(self) -> float:
        return self.report.quality_score

    @property
    def flags(self) -> list[str]:
        return self.report.flag_values()

    def __repr__(self):
        return (
            f"ValidatedOrder("
            f"id={self.validated_id!r}, "
            f"order={self.order_id!r}, "
            f"valid={self.is_valid}, "
            f"quality={self.quality_score:.0%})"
        )
