from __future__ import annotations

from apps.oms.application.models.validation_warning import (
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
