from __future__ import annotations

from apps.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from apps.oms.application.models.validation_warning import (
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
