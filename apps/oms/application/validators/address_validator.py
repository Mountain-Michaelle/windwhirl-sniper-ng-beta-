from __future__ import annotations

import re

from apps.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from apps.oms.application.models.validation_warning import (
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
