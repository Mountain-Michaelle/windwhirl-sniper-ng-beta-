from __future__ import annotations

from apps.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from apps.oms.application.models.validation_warning import (
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
