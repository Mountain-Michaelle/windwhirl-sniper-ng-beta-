from apps.oms.application.models.parsed_order import (
    ParsedOrder, PackageInfo, ExtractionStatus
)
from apps.oms.application.models.validation_error import (
    ValidationError, ErrorCode, ErrorSeverity
)
from apps.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)
from apps.oms.application.models.validation_report import (
    ValidationReport, ValidationFlag
)
from apps.oms.application.models.validated_order import ValidatedOrder

from apps.oms.application.models.duplicate_result import (
    DuplicateResult,
    DuplicateClassification,
    DimensionScore,
)
from apps.oms.application.models.duplicate_group import DuplicateGroup

# ADD to __all__:
# "DuplicateResult", "DuplicateClassification", "DimensionScore",
# "DuplicateGroup",

__all__ = [
    "ParsedOrder", "PackageInfo", "ExtractionStatus",
    "ValidationError", "ErrorCode", "ErrorSeverity",
    "ValidationWarning", "WarningCode",
    "ValidationReport", "ValidationFlag",
    "ValidatedOrder", "DuplicateResult", 
    "DuplicateClassification", "DimensionScore", "DuplicateGroup",
]
