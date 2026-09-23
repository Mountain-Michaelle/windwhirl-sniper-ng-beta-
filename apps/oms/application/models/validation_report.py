from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ValidationFlag(str, Enum):
    '''
    High-level classification flags. An order may have multiple.
    Stored as a set — PARTIAL and INCOMPLETE can coexist.

    VALID:            All critical fields present and valid.
    INVALID:          One or more CRITICAL errors present.
    PARTIAL:          Some fields missing but order is processable.
    INCOMPLETE:       Key required fields missing.
    DUPLICATE_PENDING: Reserved for Day 9 duplicate detection.
    '''
    VALID             = "VALID"
    INVALID           = "INVALID"
    PARTIAL           = "PARTIAL"
    INCOMPLETE        = "INCOMPLETE"
    DUPLICATE_PENDING = "DUPLICATE_PENDING"


@dataclass
class ValidationReport:
    '''
    Complete validation report for one ParsedOrder.

    Never modified after compute() is called.
    Created by the ValidationEngine and attached to ValidatedOrder.

    Attributes:
        errors:             List of ValidationError (may be empty).
        warnings:           List of ValidationWarning (may be empty).
        flags:              Set of ValidationFlag values.
        quality_score:      Float 0.0 to 1.0 — extraction completeness.
        is_valid:           True if no CRITICAL errors.
        validation_version: Engine version that produced this.
        validated_at:       When validation ran.
    '''
    errors:             list = field(default_factory=list)
    warnings:           list = field(default_factory=list)
    flags:              set  = field(default_factory=set)
    quality_score:      float = 0.0
    is_valid:           bool  = True
    validation_version: str   = "1.0"
    validated_at:       datetime = field(default_factory=datetime.now)

    def add_error(self, error) -> None:
        self.errors.append(error)

    def add_warning(self, warning) -> None:
        self.warnings.append(warning)

    def add_flag(self, flag: ValidationFlag) -> None:
        self.flags.add(flag)

    def compute(self, parsed_order) -> None:
        '''
        Finalize the report after all validators have run.
        Sets is_valid, flags, and quality_score based on accumulated
        errors and warnings.

        Called by ValidationEngine after all validators complete.
        '''
        from app.oms.application.models.validation_error import ErrorSeverity

        # is_valid: True only if no CRITICAL errors
        critical_errors = [
            e for e in self.errors
            if e.severity == ErrorSeverity.CRITICAL
        ]
        self.is_valid = len(critical_errors) == 0

        # Compute flags
        self.flags.clear()

        if not self.is_valid:
            self.flags.add(ValidationFlag.INVALID)
        else:
            self.flags.add(ValidationFlag.VALID)

        # INCOMPLETE: required fields missing
        required_missing = [
            e for e in self.errors
            if e.field in ("customer_name", "phone_number", "package", "delivery_address")
        ]
        if required_missing:
            self.flags.add(ValidationFlag.INCOMPLETE)

        # PARTIAL: some (not all) key fields present
        has_some = bool(
            parsed_order.customer_name
            or parsed_order.phone_number
            or parsed_order.package
        )
        missing_some = bool(required_missing)
        if has_some and missing_some:
            self.flags.add(ValidationFlag.PARTIAL)

        # Reserve DUPLICATE_PENDING for Day 9
        # self.flags.add(ValidationFlag.DUPLICATE_PENDING) ← Day 9

        # Quality score: ratio of key fields present
        key_fields = [
            "customer_name", "phone_number",
            "package", "delivery_address",
            "delivery_request", "whatsapp_number",
        ]
        present = sum(
            1 for f in key_fields
            if getattr(parsed_order, f, None) is not None
        )
        self.quality_score = round(present / len(key_fields), 2)

    def error_codes(self) -> list[str]:
        return [e.code.value for e in self.errors]

    def warning_codes(self) -> list[str]:
        return [w.code.value for w in self.warnings]

    def flag_values(self) -> list[str]:
        return [f.value for f in self.flags]

    def summary(self) -> str:
        return (
            f"ValidationReport("
            f"valid={self.is_valid}, "
            f"errors={len(self.errors)}, "
            f"warnings={len(self.warnings)}, "
            f"quality={self.quality_score:.0%}, "
            f"flags={self.flag_values()})"
        )

    def __repr__(self):
        return self.summary()
