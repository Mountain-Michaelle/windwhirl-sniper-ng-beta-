from __future__ import annotations

from apps.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from apps.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)
from apps.oms.application.validators.phone_validator import validate_nigerian_phone


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
