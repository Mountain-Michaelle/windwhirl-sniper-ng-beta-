from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ErrorSeverity(str, Enum):
    '''
    How serious a validation error is.

    CRITICAL: Order cannot proceed without this being resolved.
              Example: no customer name, invalid phone format.
    ERROR:    Order is likely problematic but may proceed.
              Example: package name missing, price invalid.
    '''
    CRITICAL = "CRITICAL"
    ERROR    = "ERROR"


class ErrorCode(str, Enum):
    '''
    Standardized error codes. Every validation error references one.
    This makes error handling deterministic downstream.
    '''
    # Required field errors
    NAME_MISSING       = "NAME_MISSING"
    PHONE_MISSING      = "PHONE_MISSING"
    PACKAGE_MISSING    = "PACKAGE_MISSING"
    ADDRESS_MISSING    = "ADDRESS_MISSING"

    # Format errors
    PHONE_INVALID      = "PHONE_INVALID"
    WHATSAPP_INVALID   = "WHATSAPP_INVALID"
    WHATSAPP_MISSING   = "WHATSAPP_MISSING"
    PRICE_INVALID      = "PRICE_INVALID"
    PRICE_MISSING      = "PRICE_MISSING"
    PRICE_NEGATIVE     = "PRICE_NEGATIVE"

    # Content errors
    ADDRESS_TOO_SHORT  = "ADDRESS_TOO_SHORT"
    ADDRESS_NO_TEXT    = "ADDRESS_NO_TEXT"
    PACKAGE_NO_NAME    = "PACKAGE_NO_NAME"


@dataclass(frozen=True)
class ValidationError:
    '''
    One validation error on a ParsedOrder field.
    Immutable — created once, never modified.

    code:        Standardized ErrorCode enum value.
    field:       Which field this error concerns.
    severity:    CRITICAL or ERROR.
    description: Human-readable explanation.
    created_at:  When this error was produced.
    '''
    code:        ErrorCode
    field:       str
    severity:    ErrorSeverity
    description: str
    created_at:  datetime = field(default_factory=datetime.now)

    def __repr__(self):
        return (
            f"ValidationError("
            f"code={self.code.value}, "
            f"field={self.field!r}, "
            f"severity={self.severity.value})"
        )

    def to_dict(self) -> dict:
        return {
            "code":        self.code.value,
            "field":       self.field,
            "severity":    self.severity.value,
            "description": self.description,
        }
