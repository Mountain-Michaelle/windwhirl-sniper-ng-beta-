from apps.oms.application.duplicate.similarity import (
    levenshtein_distance,
    levenshtein_ratio,
    normalize_for_comparison,
    phone_normalize,
)
from apps.oms.application.duplicate.phone_matcher import PhoneMatcher
from apps.oms.application.duplicate.name_matcher import NameMatcher
from apps.oms.application.duplicate.address_matcher import AddressMatcher
from apps.oms.application.duplicate.fingerprint_matcher import FingerprintMatcher
from apps.oms.application.duplicate.duplicate_store import DuplicateStore
from apps.oms.application.duplicate.duplicate_detection_engine import (
    DuplicateDetectionEngine,
    THRESHOLD_CONFIRMED,
    THRESHOLD_LIKELY,
    THRESHOLD_POSSIBLE,
)

__all__ = [
    "levenshtein_distance",
    "levenshtein_ratio",
    "normalize_for_comparison",
    "phone_normalize",
    "PhoneMatcher",
    "NameMatcher",
    "AddressMatcher",
    "FingerprintMatcher",
    "DuplicateStore",
    "DuplicateDetectionEngine",
    "THRESHOLD_CONFIRMED",
    "THRESHOLD_LIKELY",
    "THRESHOLD_POSSIBLE",
]
