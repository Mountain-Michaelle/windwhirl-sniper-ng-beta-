from apps.oms.shared.exceptions import OMSException, ValidationException


class OrderException(OMSException):
    '''
    Raised when an order-related business rule is violated.
    Examples:
        - Order already exists (duplicate)
        - Order in wrong state for the requested transition
        - Order belongs to a different staff member
    '''
    def __init__(self, message: str, order_id: str = None, context: dict = None):
        ctx = context or {}
        if order_id:
            ctx["order_id"] = order_id
        super().__init__(message, ctx)
        self.order_id = order_id


class DuplicateOrderException(OrderException):
    '''
    Raised when the same order is detected more than once.
    This is a normal business event — not a system error.
    The OMS should log it and skip, not crash.
    '''
    pass


class OrderParseException(ValidationException):
    '''
    Raised when a WhatsApp message cannot be parsed into an order.
    This means the message exists but its content does not match
    the expected order format.
    '''
    def __init__(self, message: str, raw_text: str = None, context: dict = None):
        ctx = context or {}
        if raw_text:
            # Store first 100 chars of the raw message for debugging
            ctx["raw_text_preview"] = raw_text[:100]
        super().__init__(message, context=ctx)
        self.raw_text = raw_text


class GroupNotFoundException(OMSException):
    '''
    Raised when the target WhatsApp group cannot be found.
    Could mean: wrong group name, not a member, group was deleted.
    '''
    def __init__(self, group_name: str):
        super().__init__(
            f"WhatsApp group not found: {group_name!r}",
            context={"group_name": group_name}
        )
        self.group_name = group_name


class StaffNotFoundException(OMSException):
    '''
    Raised when the configured staff number is not in the group.
    '''
    def __init__(self, staff_number: str, group_name: str):
        super().__init__(
            f"Staff number +{staff_number} not found in group {group_name!r}",
            context={"staff_number": staff_number, "group_name": group_name}
        )