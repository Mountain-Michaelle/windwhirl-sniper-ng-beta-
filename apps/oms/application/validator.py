from apps.oms.domain.entities import Order
from apps.oms.domain.interfaces import IValidator
from apps.oms.shared.logger import get_logger 
log = get_logger(__name__)


class OrderValidator(IValidator):
    '''
    Validates a parsed Order against Nabeau Store business rules.
    Implements the IValidator domain interface.

    Usage:
        validator = OrderValidator()
        errors    = validator.validate(order)
        if not errors:
            # Order is valid — proceed to assignment
        else:
            log.warning(f"Invalid order: {errors}")
    '''

    def validate(self, order: Order) -> list[str]:
        '''
        Validate an order. Returns list of error strings.
        Empty list means the order is valid and ready to process.

        Args:
            order: The Order entity to validate.

        Returns:
            List of human-readable error messages.
            Empty list means valid.
        '''
        errors = []

        # ── Rule 1: Customer name must be present ────────────────
        if not order.customer_name or len(order.customer_name.strip()) < 2:
            errors.append(
                "Customer name is missing or too short. "
                "Cannot process order without customer identification."
            )

        # ── Rule 2: At least one product item must be identified ─
        if not order.items:
            errors.append(
                "No products identified in the order message. "
                "Parser could not find a recognizable product name."
            )

        # ── Rule 3: All item quantities must be positive ─────────
        for item in order.items:
            if item.quantity <= 0:
                errors.append(
                    f"Invalid quantity for {item.product!r}: {item.quantity}. "
                    f"Quantity must be at least 1."
                )

        # ── Rule 4: Phone number validity IF provided ─────────────
        if order.customer and order.customer.phone:
            phone = order.customer.phone
            if phone.normalized and not phone.is_valid:
                errors.append(
                    f"Phone number appears invalid: {phone.normalized!r}. "
                    f"Expected Nigerian format (13 digits starting with 234)."
                )

        # ── Rule 5: Reasonable customer name (no obviously bad data) ─
        if order.customer_name:
            name = order.customer_name.strip()
            # Reject names that are clearly noise
            noise_patterns = [
                r"^\d+$",          # All digits
                r"^[^a-zA-Z]+$",   # No letters at all
            ]
            import re
            for pattern in noise_patterns:
                if re.match(pattern, name):
                    errors.append(
                        f"Customer name looks like noise: {name!r}. "
                        "Please verify the order manually."
                    )
                    break

        if errors:
            log.debug(
                f"Validation failed for order {order.order_id!r}: "
                f"{len(errors)} error(s)"
            )
        else:
            log.debug(f"Validation passed for order {order.order_id!r}")

        return errors