from __future__ import annotations

from apps.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from apps.oms.application.models.validation_warning import (
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
