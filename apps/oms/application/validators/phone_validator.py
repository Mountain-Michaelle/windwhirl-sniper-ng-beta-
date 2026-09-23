from __future__ import annotations

import re
from typing import Optional

from apps.oms.application.models.validation_error import (
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
