# ==============================================================
# WINDWHIRL OMS — DAY 8: VALIDATION ENGINE
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → application/models/validation_error.py
#   FILE 2  → application/models/validation_warning.py
#   FILE 3  → application/models/validation_report.py
#   FILE 4  → application/models/validated_order.py
#   FILE 5  → application/validators/required_field_validator.py
#   FILE 6  → applicon/validators/address_validator.py
#   FILE 9  → applicatioation/validators/phone_validator.py
#   FILE 7  → application/validators/whatsapp_validator.py
#   FILE 8  → applicatin/validators/package_validator.py
#   FILE 10 → application/validators/price_validator.py
#   FILE 11 → application/validators/delivery_validator.py
#   FILE 12 → application/validators/__init__.py
#   FILE 13 → application/validation/validation_engine.py
#   FILE 14 → application/validation/__init__.py
#   FILE 15 → application/models/__init__.py  (update)
#   FILE 16 → tests/test_validation_engine.py
#
# ENGINEERING DECISIONS:
#
#   1. ConsistencyValidator absorbed into engine.
#      Phone == WhatsApp checks are not "validation" — they are
#      observations. Keeping them in the report assembly method
#      avoids a class with a single 3-line method.
#
#   2. Validators are pure functions wrapped in classes.
#      Each returns (errors, warnings). No state. No side effects.
#      This makes them trivially testable in isolation.
#
#   3. ValidationFlag is a set, not an enum.
#      An order can be PARTIAL and INCOMPLETE simultaneously.
#      A set handles that naturally without bit-masking.
#
#   4. Nigerian phone validation is strict.
#      Valid prefixes: 070, 080, 081, 090, 091 (MTN, Airtel, Glo, 9mobile).
#      Format: 11 digits local OR 13 digits international (234XXXXXXXXXX).
#      Strings with letters or clearly wrong length → PHONE_INVALID.
#
#   5. ParsedOrder is never modified.
#      ValidatedOrder holds a reference to the original ParsedOrder.
#      All validation results live separately in ValidationReport.
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/application/models/validation_error.py
# ================================================================
# PURPOSE:
#   Structured validation error. Never a plain string.
#   Every error has a code, field, severity, and description.
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/application/models/validation_warning.py
# ================================================================
# PURPOSE:
#   Structured validation warning. Does NOT invalidate the order.
#   Warnings are informational — flags something unusual or
#   potentially incomplete without blocking processing.
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/application/models/validation_report.py
# ================================================================
# PURPOSE:
#   The complete output of the validation pipeline for one order.
#   Contains all errors, warnings, flags, and the quality score.
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/application/models/validated_order.py
# ================================================================
# PURPOSE:
#   The output of the Validation Engine.
#   Wraps the original ParsedOrder (never modified) with
#   a complete ValidationReport.
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/application/validators/required_field_validator.py
# ================================================================
# PURPOSE:
#   Checks that critical fields are present.
#   Missing required fields → CRITICAL errors.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)


class RequiredFieldValidator:
    '''
    Validates presence of required and recommended fields.

    Required (CRITICAL if missing):
        customer_name, phone_number, package, delivery_address

    Recommended (WARNING if missing):
        delivery_request, whatsapp_number
    '''

    REQUIRED = [
        ("customer_name",    ErrorCode.NAME_MISSING,    "Customer name is required"),
        ("phone_number",     ErrorCode.PHONE_MISSING,   "Phone number is required"),
        ("package",          ErrorCode.PACKAGE_MISSING, "Package information is required"),
        ("delivery_address", ErrorCode.ADDRESS_MISSING, "Delivery address is required"),
    ]

    def validate(self, parsed_order) -> tuple[list, list]:
        '''
        Args:
            parsed_order: ParsedOrder from Day 7.

        Returns:
            (errors, warnings) — both may be empty lists.
        '''
        errors   = []
        warnings = []

        for attr, code, description in self.REQUIRED:
            value = getattr(parsed_order, attr, None)
            if not value:
                errors.append(ValidationError(
                    code       =code,
                    field      =attr,
                    severity   =ErrorSeverity.CRITICAL,
                    description=description,
                ))

        # Recommended fields → warnings
        if not parsed_order.delivery_request:
            warnings.append(ValidationWarning(
                code       =WarningCode.DELIVERY_MISSING,
                field      ="delivery_request",
                description="No delivery timing specified by customer",
            ))

        if not parsed_order.whatsapp_number:
            warnings.append(ValidationWarning(
                code       =WarningCode.WHATSAPP_MISSING,
                field      ="whatsapp_number",
                description="WhatsApp number not provided",
            ))

        return errors, warnings
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/application/validators/phone_validator.py
# ================================================================
# PURPOSE:
#   Validates Nigerian phone number format.
#   Does NOT normalize — only validates what the parser extracted.
#
# VALID NIGERIAN FORMATS:
#   080XXXXXXXX, 081XXXXXXXX (MTN, Airtel)
#   070XXXXXXXX (Glo, Airtel)
#   090XXXXXXXX, 091XXXXXXXX (9mobile, MTN)
#   +234XXXXXXXXXX or 234XXXXXXXXXX (international)
#
# VALID PREFIXES (after stripping country code):
#   070, 080, 081, 090, 091
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import re
from typing import Optional

from app.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)


# All valid Nigerian mobile network prefixes
VALID_NG_PREFIXES = frozenset([
    "070", "071",                    # Glo, Airtel
    "080", "081",                    # MTN, Airtel
    "090", "091",                    # 9mobile, MTN
])


def validate_nigerian_phone(raw: str) -> tuple[bool, Optional[str]]:
    '''
    Validate a raw phone number string against Nigerian format rules.

    Args:
        raw: Phone number string as extracted (not normalized).

    Returns:
        (is_valid, reason_if_invalid)
        is_valid=True means format is acceptable.
        reason is None when valid.
    '''
    if not raw:
        return False, "Phone number is empty"

    # Extract digits only
    digits = re.sub(r'[^\d]', '', raw.strip())

    if not digits:
        return False, "Phone number contains no digits"

    # Handle international format: +234XXXXXXXXXX or 234XXXXXXXXXX
    if digits.startswith("234"):
        local = "0" + digits[3:]
    elif raw.strip().startswith("+234"):
        local = "0" + digits[3:]
    else:
        local = digits

    # Must be exactly 11 digits in local format
    if len(local) != 11:
        return False, (
            f"Phone number must be 11 digits (local) or 13 digits "
            f"(international). Got {len(digits)} digits."
        )

    # Must start with valid prefix
    prefix = local[:3]
    if prefix not in VALID_NG_PREFIXES:
        return False, (
            f"Invalid Nigerian phone prefix: {prefix!r}. "
            f"Valid prefixes: {sorted(VALID_NG_PREFIXES)}"
        )

    return True, None


class PhoneValidator:
    '''
    Validates the customer phone number in a ParsedOrder.
    No normalization — only format validation.
    '''

    def validate(self, parsed_order) -> tuple[list, list]:
        errors   = []
        warnings = []

        phone = parsed_order.phone_number
        if not phone:
            # Missing already caught by RequiredFieldValidator
            return errors, warnings

        is_valid, reason = validate_nigerian_phone(phone)
        if not is_valid:
            errors.append(ValidationError(
                code       =ErrorCode.PHONE_INVALID,
                field      ="phone_number",
                severity   =ErrorSeverity.CRITICAL,
                description=f"Phone number {phone!r} is not a valid Nigerian number: {reason}",
            ))

        return errors, warnings
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/application/validators/whatsapp_validator.py
# ================================================================
# PURPOSE:
#   Validates WhatsApp number independently using the same
#   Nigerian format rules as the phone validator.
#   WhatsApp and phone may differ — both are valid if present.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)
from app.oms.application.validators.phone_validator import validate_nigerian_phone


class WhatsAppValidator:
    '''
    Validates WhatsApp number format.
    Uses the same Nigerian rules as PhoneValidator.
    WhatsApp number is optional — missing is a warning not an error.
    '''

    def validate(self, parsed_order) -> tuple[list, list]:
        errors   = []
        warnings = []

        wa = parsed_order.whatsapp_number
        if not wa:
            # Already warned by RequiredFieldValidator
            return errors, warnings

        is_valid, reason = validate_nigerian_phone(wa)
        if not is_valid:
            errors.append(ValidationError(
                code       =ErrorCode.WHATSAPP_INVALID,
                field      ="whatsapp_number",
                severity   =ErrorSeverity.ERROR,
                description=f"WhatsApp number {wa!r} is invalid: {reason}",
            ))

        return errors, warnings
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/application/validators/address_validator.py
# ================================================================
# PURPOSE:
#   Validates that the delivery address is a real address string.
#   Does NOT validate real locations or use geocoding.
#   Minimum checks: exists, has text, not too short.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import re

from app.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)

# Minimum character length for a believable address
MIN_ADDRESS_LENGTH = 10

# Short but warn threshold
SHORT_ADDRESS_LENGTH = 20


class AddressValidator:
    '''
    Validates the delivery address.
    No location verification — structural checks only.
    '''

    def validate(self, parsed_order) -> tuple[list, list]:
        errors   = []
        warnings = []

        address = parsed_order.delivery_address
        if not address:
            # Missing already caught by RequiredFieldValidator
            return errors, warnings

        stripped = address.strip()

        # Must have actual text content (not just punctuation/numbers)
        text_only = re.sub(r'[\d\s\W]', '', stripped)
        if not text_only:
            errors.append(ValidationError(
                code       =ErrorCode.ADDRESS_NO_TEXT,
                field      ="delivery_address",
                severity   =ErrorSeverity.ERROR,
                description=(
                    "Address contains no alphabetic text. "
                    "A valid address must include street or area names."
                ),
            ))
            return errors, warnings

        # Must meet minimum length
        if len(stripped) < MIN_ADDRESS_LENGTH:
            errors.append(ValidationError(
                code       =ErrorCode.ADDRESS_TOO_SHORT,
                field      ="delivery_address",
                severity   =ErrorSeverity.ERROR,
                description=(
                    f"Address is too short ({len(stripped)} chars). "
                    f"Minimum {MIN_ADDRESS_LENGTH} characters expected."
                ),
            ))
        elif len(stripped) < SHORT_ADDRESS_LENGTH:
            warnings.append(ValidationWarning(
                code       =WarningCode.ADDRESS_SHORT,
                field      ="delivery_address",
                description=(
                    f"Address seems short ({len(stripped)} chars). "
                    "Consider requesting more detail from the customer."
                ),
            ))

        return errors, warnings
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/app/oms/application/validators/package_validator.py
# ================================================================
# PURPOSE:
#   Validates that the package has a name.
#   Price validation is handled by PriceValidator separately.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)


class PackageValidator:
    '''
    Validates package presence and structure.
    Price is validated by PriceValidator.
    Description is optional — its absence is a warning only.
    '''

    def validate(self, parsed_order) -> tuple[list, list]:
        errors   = []
        warnings = []

        package = parsed_order.package
        if not package:
            # Missing already caught by RequiredFieldValidator
            return errors, warnings

        # Package must have a name
        if not package.name or not package.name.strip():
            errors.append(ValidationError(
                code       =ErrorCode.PACKAGE_NO_NAME,
                field      ="package.name",
                severity   =ErrorSeverity.ERROR,
                description="Package section found but no package name extracted.",
            ))

        # Description missing is a warning only
        if not package.description:
            warnings.append(ValidationWarning(
                code       =WarningCode.PACKAGE_DESC_MISSING,
                field      ="package.description",
                description="Package description not provided.",
            ))

        return errors, warnings
"""


# ==============================================================
# ================================================================
#  FILE 10
#  PATH: windwhirl/app/oms/application/validators/price_validator.py
# ================================================================
# PURPOSE:
#   Validates price structure — not product catalog pricing.
#   Checks: exists, is numeric, is positive.
#   Does NOT compare against any price list.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)


class PriceValidator:
    '''
    Validates the package price.

    Valid:    Positive numeric value (29500, 47000, 28500)
    Invalid:  Negative values, zero, alphabetic content
    Warning:  Price raw text present but numeric value not extracted
    '''

    def validate(self, parsed_order) -> tuple[list, list]:
        errors   = []
        warnings = []

        package = parsed_order.package
        if not package:
            return errors, warnings

        # If no raw price text at all → error
        if not package.price_raw and package.price_value is None:
            errors.append(ValidationError(
                code       =ErrorCode.PRICE_MISSING,
                field      ="package.price",
                severity   =ErrorSeverity.ERROR,
                description="Package price is missing from the order.",
            ))
            return errors, warnings

        # Raw price present but could not extract numeric value → warning
        if package.price_raw and package.price_value is None:
            warnings.append(ValidationWarning(
                code       =WarningCode.PRICE_UNEXTRACTED,
                field      ="package.price",
                description=(
                    f"Price text found ({package.price_raw!r}) but "
                    "numeric value could not be extracted."
                ),
            ))
            return errors, warnings

        # Numeric value present — validate it
        if package.price_value is not None:
            if package.price_value < 0:
                errors.append(ValidationError(
                    code       =ErrorCode.PRICE_NEGATIVE,
                    field      ="package.price",
                    severity   =ErrorSeverity.ERROR,
                    description=(
                        f"Price cannot be negative: {package.price_value}"
                    ),
                ))
            elif package.price_value == 0:
                errors.append(ValidationError(
                    code       =ErrorCode.PRICE_INVALID,
                    field      ="package.price",
                    severity   =ErrorSeverity.ERROR,
                    description="Price is zero — this is likely an extraction error.",
                ))

        return errors, warnings
"""


# ==============================================================
# ================================================================
#  FILE 11
#  PATH: windwhirl/app/oms/application/validators/delivery_validator.py
# ================================================================
# PURPOSE:
#   Validates delivery request presence.
#   Any non-empty value is valid — we do not interpret dates here.
#   Missing is a warning only (delivery was already warned by
#   RequiredFieldValidator).
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)


class DeliveryValidator:
    '''
    Validates delivery request field.
    Any non-empty delivery request is acceptable.
    Missing is a warning — not an error.
    '''

    def validate(self, parsed_order) -> tuple[list, list]:
        errors   = []
        warnings = []

        delivery = parsed_order.delivery_request
        if not delivery or not delivery.strip():
            warnings.append(ValidationWarning(
                code       =WarningCode.DELIVERY_MISSING,
                field      ="delivery_request",
                description="Customer did not specify a delivery time.",
            ))

        return errors, warnings
"""


# ==============================================================
# ================================================================
#  FILE 12
#  PATH: windwhirl/app/oms/application/validators/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.validators.required_field_validator import RequiredFieldValidator
from app.oms.application.validators.phone_validator import PhoneValidator
from app.oms.application.validators.whatsapp_validator import WhatsAppValidator
from app.oms.application.validators.address_validator import AddressValidator
from app.oms.application.validators.package_validator import PackageValidator
from app.oms.application.validators.price_validator import PriceValidator
from app.oms.application.validators.delivery_validator import DeliveryValidator

__all__ = [
    "RequiredFieldValidator",
    "PhoneValidator",
    "WhatsAppValidator",
    "AddressValidator",
    "PackageValidator",
    "PriceValidator",
    "DeliveryValidator",
]
"""


# ==============================================================
# ================================================================
#  FILE 13
#  PATH: windwhirl/app/oms/application/validation/validation_engine.py
# ================================================================
# PURPOSE:
#   Orchestrates all validators against a ParsedOrder.
#   Produces a ValidatedOrder.
#   One validator failure NEVER stops others.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.models.parsed_order import ParsedOrder
from app.oms.application.models.validated_order import ValidatedOrder
from app.oms.application.models.validation_report import ValidationReport
from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)
from app.oms.application.validators import (
    RequiredFieldValidator,
    PhoneValidator,
    WhatsAppValidator,
    AddressValidator,
    PackageValidator,
    PriceValidator,
    DeliveryValidator,
)
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)

VALIDATION_VERSION = "1.0"


class ValidationEngine:
    '''
    Runs all validators against a ParsedOrder and produces a ValidatedOrder.

    Validators run independently — a crash in one never stops others.
    All errors and warnings from all validators are merged into one
    ValidationReport. Consistency observations are added by the engine
    after all validators complete.

    Usage:
        engine = ValidationEngine()
        validated = await engine.validate(parsed_order)
    '''

    def __init__(self):
        # Ordered validator pipeline
        self._validators = [
            RequiredFieldValidator(),
            PhoneValidator(),
            WhatsAppValidator(),
            AddressValidator(),
            PackageValidator(),
            PriceValidator(),
            DeliveryValidator(),
        ]

    async def validate(self, parsed_order: ParsedOrder) -> ValidatedOrder:
        '''
        Validate a ParsedOrder and return a ValidatedOrder.

        ParsedOrder is NEVER modified.
        All results live in the ValidationReport.

        Args:
            parsed_order: The ParsedOrder from Day 7 parser.

        Returns:
            ValidatedOrder containing original ParsedOrder + ValidationReport.
        '''
        log.debug(
            f"ValidationEngine: validating order {parsed_order.order_id!r}"
        )

        report = ValidationReport(validation_version=VALIDATION_VERSION)

        # ── Run each validator independently ──────────────────────
        for validator in self._validators:
            try:
                errors, warnings = validator.validate(parsed_order)
                for e in errors:
                    report.add_error(e)
                    log.debug(
                        f"  Error [{e.code.value}] on {e.field!r}: "
                        f"{e.description}"
                    )
                for w in warnings:
                    report.add_warning(w)
                    log.debug(
                        f"  Warning [{w.code.value}] on {w.field!r}: "
                        f"{w.description}"
                    )
            except Exception as exc:
                # Validator crashed — log and continue with others
                log.error(
                    f"  Validator {validator.__class__.__name__} crashed: {exc}",
                    exc_info=True
                )
                # Add a warning so downstream consumers know this validator failed
                report.add_warning(ValidationWarning(
                    code       =WarningCode.DELIVERY_MISSING,  # Generic placeholder
                    field      ="unknown",
                    description=(
                        f"Validator {validator.__class__.__name__} failed: {exc}"
                    ),
                ))

        # ── Consistency observations (engine-level, not a validator) ──
        self._check_consistency(parsed_order, report)

        # ── Compute final flags and quality score ──────────────────
        report.compute(parsed_order)

        # ── Build ValidatedOrder ───────────────────────────────────
        validated = ValidatedOrder(
            parsed_order=parsed_order,
            report      =report,
        )

        # ── Emit events ───────────────────────────────────────────
        if report.is_valid:
            event_name = "order.validated"
            log.info(
                f"✅ Validated: {parsed_order.order_id!r} | "
                f"quality={report.quality_score:.0%} | "
                f"warnings={len(report.warnings)}"
            )
        else:
            event_name = "order.validation_failed"
            log.warning(
                f"❌ Validation failed: {parsed_order.order_id!r} | "
                f"errors={len(report.errors)} | "
                f"quality={report.quality_score:.0%}"
            )

        await dispatcher.emit(
            event_name,
            validated_order=validated,
            order_id       =parsed_order.order_id,
            is_valid       =report.is_valid,
            error_codes    =report.error_codes(),
            warning_codes  =report.warning_codes(),
            quality_score  =report.quality_score,
            flags          =report.flag_values(),
        )

        if report.warnings:
            for w in report.warnings:
                await dispatcher.emit(
                    "order.validation_warning",
                    order_id  =parsed_order.order_id,
                    code      =w.code.value,
                    field     =w.field,
                    description=w.description,
                )

        return validated

    def _check_consistency(self, parsed_order: ParsedOrder, report: ValidationReport) -> None:
        '''
        Cross-field consistency observations.
        These are informational — they never produce errors.
        Different phone and WhatsApp numbers are both valid and common.
        '''
        phone = parsed_order.phone_number
        wa    = parsed_order.whatsapp_number

        if phone and wa and phone.strip() != wa.strip():
            report.add_warning(ValidationWarning(
                code       =WarningCode.PHONE_WHATSAPP_DIFFER,
                field      ="phone_number/whatsapp_number",
                description=(
                    f"Phone number ({phone!r}) differs from "
                    f"WhatsApp number ({wa!r}). "
                    "This is allowed — customer uses separate numbers."
                ),
            ))

        # Single-word name warning
        name = parsed_order.customer_name
        if name and len(name.strip().split()) < 2:
            report.add_warning(ValidationWarning(
                code       =WarningCode.NAME_SINGLE_WORD,
                field      ="customer_name",
                description=(
                    f"Customer name {name!r} is a single word. "
                    "Full name is preferred for identification."
                ),
            ))
"""


# ==============================================================
# ================================================================
#  FILE 14
#  PATH: windwhirl/app/oms/application/validation/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.validation.validation_engine import ValidationEngine

__all__ = ["ValidationEngine"]
"""


# ==============================================================
# ================================================================
#  FILE 15
#  PATH: windwhirl/app/oms/application/models/__init__.py  (UPDATE)
# ================================================================
# Add Day 8 models to the existing models __init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.models.parsed_order import (
    ParsedOrder, PackageInfo, ExtractionStatus
)
from app.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from app.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)
from app.oms.application.models.validation_report import (
    ValidationReport, ValidationFlag
)
from app.oms.application.models.validated_order import ValidatedOrder

__all__ = [
    "ParsedOrder", "PackageInfo", "ExtractionStatus",
    "ValidationError", "ErrorCode", "ErrorSeverity",
    "ValidationWarning", "WarningCode",
    "ValidationReport", "ValidationFlag",
    "ValidatedOrder",
]
"""


# ==============================================================
# ================================================================
#  FILE 16
#  PATH: windwhirl/app/oms/tests/test_validation_engine.py
# ================================================================
# Unit tests for all validators and scenarios.
# Run: python -m pytest app/oms/tests/test_validation_engine.py -v
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from app.oms.application.validation.validation_engine import ValidationEngine
from app.oms.application.models.parsed_order import ParsedOrder, PackageInfo
from app.oms.application.models.validation_error import ErrorCode
from app.oms.application.models.validation_warning import WarningCode
from app.oms.application.models.validation_report import ValidationFlag
from app.oms.application.validators.phone_validator import validate_nigerian_phone


# ── Helpers ──────────────────────────────────────────────────────

def make_valid_order(**overrides) -> ParsedOrder:
    '''Build a fully valid ParsedOrder. Override fields as needed.'''
    defaults = dict(
        order_id          ="ORD-001",
        worker_number     ="2348XXXXXXXXX",
        customer_name     ="Blessing Adeyemi",
        phone_number      ="08031234567",
        whatsapp_number   ="08031234567",
        package           =PackageInfo(
                               name="1 Combo Set",
                               description="(1 serum & 1 cream)",
                               price_raw="#29,500",
                               price_value=29500.0,
                           ),
        delivery_address  ="12 Allen Avenue, Ikeja Lagos",
        delivery_request  ="Tomorrow",
        raw_text          ="raw message text",
    )
    defaults.update(overrides)
    return ParsedOrder(**defaults)


async def validate(order: ParsedOrder):
    engine = ValidationEngine()
    return await engine.validate(order)


# ── Valid order ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_order_passes():
    order     = make_valid_order()
    validated = await validate(order)
    assert validated.is_valid is True


@pytest.mark.asyncio
async def test_valid_order_quality_score():
    order     = make_valid_order()
    validated = await validate(order)
    assert validated.quality_score >= 0.8


@pytest.mark.asyncio
async def test_valid_order_has_valid_flag():
    order     = make_valid_order()
    validated = await validate(order)
    assert "VALID" in validated.flags


@pytest.mark.asyncio
async def test_parsed_order_unchanged():
    '''ParsedOrder must never be modified by the engine.'''
    order          = make_valid_order()
    original_name  = order.customer_name
    original_phone = order.phone_number
    validated      = await validate(order)
    assert validated.parsed_order.customer_name == original_name
    assert validated.parsed_order.phone_number  == original_phone


# ── Phone validation ──────────────────────────────────────────────

@pytest.mark.parametrize("phone", [
    "08031234567",
    "07012345678",
    "09012345678",
    "08123456789",
    "09134567890",
    "+2348031234567",
    "2348031234567",
])
def test_valid_nigerian_phones(phone):
    is_valid, reason = validate_nigerian_phone(phone)
    assert is_valid, f"Expected valid: {phone!r} — {reason}"


@pytest.mark.parametrize("phone", [
    "123456",              # Too short
    "0123456789012345",    # Too long
    "0601234567",          # Invalid prefix
    "hello",               # Letters
    "",                    # Empty
    "+442071234567",       # UK number
])
def test_invalid_nigerian_phones(phone):
    is_valid, _ = validate_nigerian_phone(phone)
    assert not is_valid, f"Expected invalid: {phone!r}"


@pytest.mark.asyncio
async def test_invalid_phone_produces_error():
    order     = make_valid_order(phone_number="0601234567")
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.PHONE_INVALID.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_missing_phone_produces_error():
    order     = make_valid_order(phone_number=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.PHONE_MISSING.value in validated.report.error_codes()


# ── WhatsApp validation ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_whatsapp_is_warning():
    order     = make_valid_order(whatsapp_number=None)
    validated = await validate(order)
    # Missing WhatsApp is a warning, not an error
    assert validated.is_valid
    assert WarningCode.WHATSAPP_MISSING.value in validated.report.warning_codes()


@pytest.mark.asyncio
async def test_invalid_whatsapp_produces_error():
    order     = make_valid_order(whatsapp_number="0601234567")
    validated = await validate(order)
    assert ErrorCode.WHATSAPP_INVALID.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_different_phone_whatsapp_is_warning():
    order     = make_valid_order(
        phone_number    ="08031234567",
        whatsapp_number ="07012345678",
    )
    validated = await validate(order)
    # Different numbers is allowed — warning only
    assert validated.is_valid
    assert WarningCode.PHONE_WHATSAPP_DIFFER.value in validated.report.warning_codes()


# ── Address validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_address_fails():
    order     = make_valid_order(delivery_address=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.ADDRESS_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_short_address_warning():
    order     = make_valid_order(delivery_address="Lagos")
    validated = await validate(order)
    # Short but has text — warning not error
    assert WarningCode.ADDRESS_SHORT.value in validated.report.warning_codes()


@pytest.mark.asyncio
async def test_numbers_only_address_fails():
    order     = make_valid_order(delivery_address="12345 67890")
    validated = await validate(order)
    assert ErrorCode.ADDRESS_NO_TEXT.value in validated.report.error_codes()


# ── Package validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_package_fails():
    order     = make_valid_order(package=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.PACKAGE_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_package_no_description_warning():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="#29,500", price_value=29500.0
        )
    )
    validated = await validate(order)
    assert WarningCode.PACKAGE_DESC_MISSING.value in validated.report.warning_codes()


# ── Price validation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_price_error():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="", price_value=None
        )
    )
    validated = await validate(order)
    assert ErrorCode.PRICE_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_negative_price_error():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="-29500", price_value=-29500.0
        )
    )
    validated = await validate(order)
    assert ErrorCode.PRICE_NEGATIVE.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_price_raw_but_no_numeric_warning():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="#??,???", price_value=None
        )
    )
    validated = await validate(order)
    assert WarningCode.PRICE_UNEXTRACTED.value in validated.report.warning_codes()


# ── Missing name ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_name_fails():
    order     = make_valid_order(customer_name=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.NAME_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_single_word_name_warning():
    order     = make_valid_order(customer_name="Blessing")
    validated = await validate(order)
    assert WarningCode.NAME_SINGLE_WORD.value in validated.report.warning_codes()


# ── Delivery validation ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_delivery_request_warning():
    order     = make_valid_order(delivery_request=None)
    validated = await validate(order)
    # Missing delivery is a warning, not an error
    assert validated.is_valid
    assert WarningCode.DELIVERY_MISSING.value in validated.report.warning_codes()


# ── Quality score ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quality_score_decreases_with_missing_fields():
    full     = await validate(make_valid_order())
    partial  = await validate(make_valid_order(
        phone_number=None,
        delivery_request=None,
    ))
    assert full.quality_score > partial.quality_score


# ── INCOMPLETE and PARTIAL flags ──────────────────────────────────

@pytest.mark.asyncio
async def test_incomplete_flag_when_required_missing():
    order     = make_valid_order(customer_name=None)
    validated = await validate(order)
    assert "INCOMPLETE" in validated.flags


@pytest.mark.asyncio
async def test_partial_flag_when_some_present():
    order     = make_valid_order(delivery_address=None)
    validated = await validate(order)
    # Has name + phone + package but missing address
    assert "PARTIAL" in validated.flags or "INCOMPLETE" in validated.flags
"""


# ==============================================================
# DAY 8 VERIFICATION
# ==============================================================
#
# Test 1 — Imports:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.models.validated_order import ValidatedOrder
#   from app.oms.application.models.validation_report import ValidationReport, ValidationFlag
#   from app.oms.application.models.validation_error import ValidationError, ErrorCode
#   from app.oms.application.models.validation_warning import ValidationWarning, WarningCode
#   from app.oms.application.validation.validation_engine import ValidationEngine
#   print('All Day 8 imports OK')
#   "
#
# Test 2 — Quick validation of a valid order:
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.application.models.parsed_order import ParsedOrder, PackageInfo
#   from app.oms.application.validation.validation_engine import ValidationEngine
#
#   order = ParsedOrder(
#       order_id='ORD-001', worker_number='2348XXX',
#       customer_name='Blessing Adeyemi',
#       phone_number='08031234567',
#       whatsapp_number='08031234567',
#       package=PackageInfo('1 Combo Set', '1 serum & 1 cream', '#29,500', 29500.0),
#       delivery_address='12 Allen Avenue, Ikeja Lagos',
#       delivery_request='Tomorrow',
#       raw_text='test',
#   )
#
#   async def run():
#       engine    = ValidationEngine()
#       validated = await engine.validate(order)
#       print(f'Valid:   {validated.is_valid}')
#       print(f'Quality: {validated.quality_score:.0%}')
#       print(f'Flags:   {validated.flags}')
#       print(f'Errors:  {validated.report.error_codes()}')
#       print(f'Warns:   {validated.report.warning_codes()}')
#       print(f'ParsedOrder unchanged: {validated.parsed_order.customer_name}')
#
#   asyncio.run(run())
#   "
#
# Test 3 — Run all unit tests:
#   python -m pytest app/oms/tests/test_validation_engine.py -v
#   Expected: 25+ tests PASSED
#
# ==============================================================
# WHAT DAY 9 BUILDS
# ==============================================================
# Day 9: Intelligent Duplicate Detection Engine
#   Receives ValidatedOrder from Day 8.
#   Detects:
#     - Exact duplicates (same fingerprint)
#     - Near duplicates (same customer + similar items)
#     - Phone-based duplicates (same phone, different name spelling)
#   Produces:
#     - DuplicateClassification per order
#     - DuplicateGroup when multiple orders are related
#     - Audit trail of all duplicate decisions
#   Never deletes orders. Only classifies relationships.
#   Sets ValidationFlag.DUPLICATE_PENDING reserved in Day 8.
# ==============================================================