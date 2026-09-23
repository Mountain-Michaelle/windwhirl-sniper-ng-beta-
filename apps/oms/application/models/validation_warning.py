from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class WarningCode(str, Enum):
    '''
    Standardized warning codes.
    Warnings do not block order processing.
    '''
    # Missing optional fields
    DELIVERY_MISSING      = "DELIVERY_MISSING"
    QUESTION_EMPTY        = "QUESTION_EMPTY"
    PACKAGE_DESC_MISSING  = "PACKAGE_DESC_MISSING"
    DATE_MISSING          = "DATE_MISSING"
    CAMPAIGN_MISSING      = "CAMPAIGN_MISSING"
    WHATSAPP_MISSING      = "WHATSAPP_MISSING"

    # Consistency observations
    PHONE_WHATSAPP_DIFFER = "PHONE_WHATSAPP_DIFFER"

    # Quality observations
    ADDRESS_SHORT         = "ADDRESS_SHORT"
    NAME_SINGLE_WORD      = "NAME_SINGLE_WORD"
    PRICE_UNEXTRACTED     = "PRICE_UNEXTRACTED"


@dataclass(frozen=True)
class ValidationWarning:
    '''
    One validation warning on a ParsedOrder.
    Immutable. Does not prevent order processing.

    code:        Standardized WarningCode.
    field:       Which field this concerns.
    description: Human-readable note.
    created_at:  When produced.
    '''
    code:        WarningCode
    field:       str
    description: str
    created_at:  datetime = field(default_factory=datetime.now)

    def __repr__(self):
        return (
            f"ValidationWarning("
            f"code={self.code.value}, "
            f"field={self.field!r})"
        )

    def to_dict(self) -> dict:
        return {
            "code":        self.code.value,
            "field":       self.field,
            "description": self.description,
        }
