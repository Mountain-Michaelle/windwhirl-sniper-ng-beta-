



class OMSException(Exception):
    '''
    Root exception for the entire OMS system.
    All OMS-specific exceptions inherit from this.
    Catch this to handle any OMS error in one place.
    '''
    def __init__(self, message: str, context: dict = None):
        super().__init__(message)
        self.message = message
        # Optional dict of extra context for logging/debugging
        # Example: {"order_id": "123", "group": "Nabeau Orders"}
        self.context = context or {}

    def __str__(self):
        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{self.message} [{ctx}]"
        return self.message


class ConfigurationException(OMSException):
    '''
    Raised when configuration is missing, invalid, or inconsistent.
    Examples:
        - Required setting not provided
        - Invalid timezone value
        - Incompatible setting combination
    '''
    pass


class InfrastructureException(OMSException):
    '''
    Raised when an external system fails.
    Examples:
        - Browser cannot connect to WhatsApp Web
        - Google Sheets API returns an error
        - Database connection fails
    Infrastructure errors are usually transient — they may
    resolve on retry. Business logic should not depend on them.
    '''
    pass


class ValidationException(OMSException):
    '''
    Raised when data does not meet business rules.
    Examples:
        - Order message missing required fields
        - Phone number in wrong format
        - Order total is negative
    Validation errors are not retryable — the data itself is bad.
    '''
    def __init__(self, message: str, field: str = None, context: dict = None):
        super().__init__(message, context)
        # The specific field that failed validation (if applicable)
        self.field = field
