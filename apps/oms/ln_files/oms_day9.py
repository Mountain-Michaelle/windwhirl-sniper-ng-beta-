# ==============================================================
# WINDWHIRL OMS — DAY 9: INTELLIGENT DUPLICATE DETECTION ENGINE
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → application/models/duplicate_result.py
#   FILE 2  → application/models/duplicate_group.py
#   FILE 3  → application/duplicate/similarity.py
#   FILE 4  → application/duplicate/phone_matcher.py
#   FILE 5  → application/duplicate/name_matcher.py
#   FILE 6  → application/duplicate/address_matcher.py
#   FILE 7  → application/duplicate/fingerprint_matcher.py
#   FILE 8  → application/duplicate/duplicate_store.py
#   FILE 9  → application/duplicate/duplicate_detection_engine.py
#   FILE 10 → application/duplicate/__init__.py
#   FILE 11 → application/models/__init__.py  (update)
#   FILE 12 → tests/test_duplicate_detection.py
#
# ENGINEERING DECISIONS:
#
#   1. Similarity is multi-dimensional, weighted.
#      Phone match contributes 0.60 to final score.
#      Name similarity contributes 0.25.
#      Address similarity contributes 0.15.
#      Score >= 0.85 → CONFIRMED_DUPLICATE.
#      Score >= 0.60 → LIKELY_DUPLICATE (human review needed).
#      Score >= 0.35 → POSSIBLE_DUPLICATE (flag only).
#      Score  < 0.35 → UNIQUE.
#
#   2. Levenshtein ratio for name and address comparison.
#      No external libraries — pure Python implementation included.
#      "Blessing Adeyemi" vs "Blessing Adeyemi-Okafor" → 0.88
#      "Blessing Adeyemi" vs "Emeka Okonkwo" → 0.12
#
#   3. Time window enforcement.
#      Orders outside the window cannot be duplicates.
#      Default window: 48 hours. Configurable per deployment.
#      A returning customer ordering same product 3 weeks later
#      is NOT a duplicate — it's a repeat sale.
#
#   4. Phone is the anchor signal.
#      Phone match alone at 0.60 triggers LIKELY_DUPLICATE.
#      This catches: same customer, different name spelling, no address yet.
#      Any phone match above threshold is always investigated.
#
#   5. DuplicateGroup clusters related orders.
#      All orders that match each other belong to one group.
#      The group tracks which order came first (canonical_order_id).
#      Groups never merge with other groups automatically.
#
#   6. Never delete, never block automatically.
#      Engine produces classifications only.
#      Day 10 storage and human review decide what to do.
#      ValidationFlag.DUPLICATE_PENDING (reserved in Day 8) is set here.
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/application/models/duplicate_result.py
# ================================================================
# PURPOSE:
#   The output of duplicate detection for one order comparison.
#   Carries the similarity score, matched signals, and classification.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class DuplicateClassification(str, Enum):
    '''
    The result of comparing two orders for duplication.

    CONFIRMED_DUPLICATE: Score >= 0.85. Very high confidence.
                         Strong signals across multiple dimensions.
                         System flags for human review immediately.

    LIKELY_DUPLICATE:    Score >= 0.60. Needs human review.
                         Usually a phone match with some name overlap.
                         Could be duplicate or returning customer.

    POSSIBLE_DUPLICATE:  Score >= 0.35. Weak signal.
                         One dimension matches, others unclear.
                         Log and continue — human reviews periodically.

    UNIQUE:              Score < 0.35. No meaningful overlap detected.
                         Order is treated as a new unique order.

    RETURNING_CUSTOMER:  Phone matches but time window expired.
                         Same customer but ordering again legitimately.
                         Never classified as duplicate.
    '''
    CONFIRMED_DUPLICATE = "CONFIRMED_DUPLICATE"
    LIKELY_DUPLICATE    = "LIKELY_DUPLICATE"
    POSSIBLE_DUPLICATE  = "POSSIBLE_DUPLICATE"
    UNIQUE              = "UNIQUE"
    RETURNING_CUSTOMER  = "RETURNING_CUSTOMER"

    @property
    def requires_review(self) -> bool:
        return self in (
            DuplicateClassification.CONFIRMED_DUPLICATE,
            DuplicateClassification.LIKELY_DUPLICATE,
        )

    @property
    def is_duplicate(self) -> bool:
        return self in (
            DuplicateClassification.CONFIRMED_DUPLICATE,
            DuplicateClassification.LIKELY_DUPLICATE,
        )


@dataclass
class DimensionScore:
    '''
    Similarity score for one matching dimension.

    dimension:  "phone", "name", "address", "fingerprint"
    score:      0.0 to 1.0 raw similarity for this dimension.
    weight:     Contribution weight in final score calculation.
    matched:    True if this dimension was considered matched.
    detail:     What was compared e.g. "08031234567 vs 08031234567"
    '''
    dimension: str
    score:     float
    weight:    float
    matched:   bool
    detail:    str = ""

    @property
    def weighted_score(self) -> float:
        return self.score * self.weight

    def __repr__(self):
        return (
            f"DimensionScore("
            f"{self.dimension}={self.score:.2f}×{self.weight:.2f}"
            f"={'✓' if self.matched else '✗'})"
        )


@dataclass
class DuplicateResult:
    '''
    The comparison result between two orders.

    result_id:         Unique identifier for this comparison.
    order_id_a:        The order being checked (new order).
    order_id_b:        The order it was compared against (existing).
    classification:    CONFIRMED, LIKELY, POSSIBLE, UNIQUE, or RETURNING.
    final_score:       Weighted sum of dimension scores (0.0 to 1.0).
    dimensions:        Individual DimensionScore breakdown.
    hours_apart:       Time between the two orders.
    within_window:     Whether both orders fall within the time window.
    group_id:          DuplicateGroup ID if assigned (set after grouping).
    detected_at:       When this comparison was made.
    '''
    order_id_a:      str
    order_id_b:      str
    classification:  DuplicateClassification
    final_score:     float
    dimensions:      list[DimensionScore]
    hours_apart:     float
    within_window:   bool
    result_id:       str      = field(default_factory=lambda: str(uuid.uuid4())[:8])
    group_id:        str      = ""
    detected_at:     datetime = field(default_factory=datetime.now)

    @property
    def is_duplicate(self) -> bool:
        return self.classification.is_duplicate

    @property
    def requires_review(self) -> bool:
        return self.classification.requires_review

    def dimension_by_name(self, name: str) -> Optional[DimensionScore]:
        return next((d for d in self.dimensions if d.dimension == name), None)

    def matched_dimensions(self) -> list[str]:
        return [d.dimension for d in self.dimensions if d.matched]

    def summary(self) -> str:
        matched = self.matched_dimensions()
        return (
            f"DuplicateResult("
            f"{self.order_id_a!r} vs {self.order_id_b!r}: "
            f"{self.classification.value} "
            f"score={self.final_score:.2f} "
            f"matched={matched})"
        )

    def __repr__(self):
        return self.summary()
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/application/models/duplicate_group.py
# ================================================================
# PURPOSE:
#   Groups related orders that are likely duplicates of each other.
#   The canonical_order_id is the first (oldest) order in the group.
#   All subsequent duplicates reference the canonical.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class DuplicateGroup:
    '''
    A cluster of orders believed to be duplicates of each other.

    canonical_order_id: The first / oldest order in the group.
                        All others are considered duplicates of this.
    member_order_ids:   All order IDs in this group including canonical.
    classification:     The strongest classification among all pairwise comparisons.
    created_at:         When this group was first formed.
    updated_at:         When the group was last modified (member added).
    resolved:           True if a human has reviewed and resolved this group.
    resolution_notes:   What the human decided.
    '''
    canonical_order_id: str
    group_id:           str      = field(default_factory=lambda: str(uuid.uuid4())[:8])
    member_order_ids:   list[str]= field(default_factory=list)
    classification:     str      = "LIKELY_DUPLICATE"
    created_at:         datetime = field(default_factory=datetime.now)
    updated_at:         datetime = field(default_factory=datetime.now)
    resolved:           bool     = False
    resolution_notes:   str      = ""

    def __post_init__(self):
        # Canonical is always a member
        if self.canonical_order_id not in self.member_order_ids:
            self.member_order_ids.insert(0, self.canonical_order_id)

    def add_member(self, order_id: str) -> None:
        '''Add an order to this group if not already present.'''
        if order_id not in self.member_order_ids:
            self.member_order_ids.append(order_id)
            self.updated_at = datetime.now()

    def has_member(self, order_id: str) -> bool:
        return order_id in self.member_order_ids

    @property
    def size(self) -> int:
        return len(self.member_order_ids)

    @property
    def duplicate_count(self) -> int:
        '''Number of duplicates (excluding the canonical).'''
        return max(0, self.size - 1)

    def resolve(self, notes: str) -> None:
        self.resolved          = True
        self.resolution_notes  = notes
        self.updated_at        = datetime.now()

    def __repr__(self):
        return (
            f"DuplicateGroup("
            f"id={self.group_id!r}, "
            f"canonical={self.canonical_order_id!r}, "
            f"members={self.size}, "
            f"resolved={self.resolved})"
        )
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/application/duplicate/similarity.py
# ================================================================
# PURPOSE:
#   Pure Python Levenshtein ratio for fuzzy string matching.
#   No external dependencies. Fast enough for order volumes.
#
# WHY LEVENSHTEIN NOT JUST CONTAINS:
#   "Blessing Adeyemi" and "Blessing Adeyemi-Okafor" → 0.88 similar
#   "Blessing Adeyemi" and "Mrs Blessing Adeyemi"    → 0.84 similar
#   "Blessing Adeyemi" and "Emeka Okonkwo"           → 0.12 similar
#   contains() would miss the first two patterns.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import re


def levenshtein_distance(s1: str, s2: str) -> int:
    '''
    Compute the Levenshtein edit distance between two strings.
    Pure Python — no external dependencies.

    Time:  O(len(s1) * len(s2))
    Space: O(min(len(s1), len(s2)))

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Integer edit distance (0 = identical, higher = more different).
    '''
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)

    # Use the shorter string as rows to save memory
    if len(s1) > len(s2):
        s1, s2 = s2, s1

    prev = list(range(len(s1) + 1))

    for j, ch2 in enumerate(s2, 1):
        curr = [j]
        for i, ch1 in enumerate(s1, 1):
            cost = 0 if ch1 == ch2 else 1
            curr.append(min(
                prev[i] + 1,      # deletion
                curr[i - 1] + 1,  # insertion
                prev[i - 1] + cost  # substitution
            ))
        prev = curr

    return prev[-1]


def levenshtein_ratio(s1: str, s2: str) -> float:
    '''
    Similarity ratio based on Levenshtein distance.
    Returns 1.0 for identical strings, 0.0 for completely different.

    Formula: 1 - (distance / max_possible_distance)

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Float in range [0.0, 1.0].
    '''
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    distance    = levenshtein_distance(s1, s2)
    max_dist    = max(len(s1), len(s2))
    return round(1.0 - (distance / max_dist), 4)


def normalize_for_comparison(text: str) -> str:
    '''
    Normalize text for comparison purposes ONLY.
    This normalization is NEVER applied to stored data.

    Steps:
        - Lowercase
        - Strip leading/trailing whitespace
        - Collapse multiple spaces
        - Remove common honorifics and prefixes
        - Remove punctuation except hyphens (compound names)
    '''
    if not text:
        return ""

    t = text.lower().strip()

    # Remove honorifics
    honorifics = r'^(mr\.?|mrs\.?|miss\.?|ms\.?|dr\.?|prof\.?|engr\.?|'
    honorifics += r'alhaji\.?|alhaja\.?|chief\.?|barr\.?\s+)'
    t = re.sub(honorifics, '', t, flags=re.IGNORECASE).strip()

    # Remove punctuation except hyphens
    t = re.sub(r'[^\w\s\-]', '', t)

    # Collapse whitespace
    t = re.sub(r'\s+', ' ', t).strip()

    return t


def phone_normalize(raw: str) -> str:
    '''
    Normalize a phone number to digits only starting from local format.
    Used only for comparison — never applied to stored data.

    Examples:
        "+2348031234567" → "08031234567"
        "2348031234567"  → "08031234567"
        "08031234567"    → "08031234567"
        "08031234567.0"  → "08031234567"   (Excel float artifact)
    '''
    if not raw:
        return ""

    # Remove everything except digits
    digits = re.sub(r'[^\d]', '', str(raw).strip())

    # Remove country code prefix
    if digits.startswith("234") and len(digits) == 13:
        return "0" + digits[3:]

    return digits
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/application/duplicate/phone_matcher.py
# ================================================================
# PURPOSE:
#   Compares phone numbers between two orders.
#   Phone is the highest-weight dimension (0.60).
#   A phone match alone is sufficient for LIKELY_DUPLICATE.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.duplicate.similarity import phone_normalize
from app.oms.application.models.duplicate_result import DimensionScore


class PhoneMatcher:
    '''
    Compares the phone numbers of two ParsedOrders.
    Weight: 0.60 — highest of all dimensions.

    Checks both phone_number and whatsapp_number fields.
    A match on either counts as a phone match.

    Score:
        1.0 if any phone from order A matches any phone from order B
        0.0 otherwise (phone comparison is binary — no partial credit)
    '''

    WEIGHT = 0.60

    def compare(
        self,
        order_a,
        order_b,
    ) -> DimensionScore:
        '''
        Compare phone numbers between two ParsedOrders.

        Args:
            order_a: ParsedOrder (the new order being checked).
            order_b: ParsedOrder (the existing order to compare against).

        Returns:
            DimensionScore with score 1.0 (match) or 0.0 (no match).
        '''
        # Collect all phone values from both orders
        phones_a = self._collect_phones(order_a)
        phones_b = self._collect_phones(order_b)

        if not phones_a or not phones_b:
            return DimensionScore(
                dimension="phone",
                score    =0.0,
                weight   =self.WEIGHT,
                matched  =False,
                detail   ="One or both orders missing phone number",
            )

        # Check all combinations — match if any pair matches
        for pa in phones_a:
            for pb in phones_b:
                if pa == pb:
                    return DimensionScore(
                        dimension="phone",
                        score    =1.0,
                        weight   =self.WEIGHT,
                        matched  =True,
                        detail   =f"{pa} == {pb}",
                    )

        return DimensionScore(
            dimension="phone",
            score    =0.0,
            weight   =self.WEIGHT,
            matched  =False,
            detail   =f"{phones_a} ≠ {phones_b}",
        )

    def _collect_phones(self, order) -> list[str]:
        '''
        Collect and normalize all phone values from a ParsedOrder.
        Returns unique normalized values only.
        '''
        raw_phones = []

        phone = getattr(order, 'phone_number', None)
        if phone:
            raw_phones.append(phone)

        wa = getattr(order, 'whatsapp_number', None)
        if wa:
            raw_phones.append(wa)

        normalized = []
        seen       = set()
        for p in raw_phones:
            n = phone_normalize(p)
            if n and n not in seen and len(n) >= 8:
                normalized.append(n)
                seen.add(n)

        return normalized
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/application/duplicate/name_matcher.py
# ================================================================
# PURPOSE:
#   Compares customer names using fuzzy Levenshtein ratio.
#   Weight: 0.25.
#   Threshold: 0.75 to count as a name match.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.duplicate.similarity import (
    levenshtein_ratio,
    normalize_for_comparison,
)
from app.oms.application.models.duplicate_result import DimensionScore


class NameMatcher:
    '''
    Compares customer names between two orders using fuzzy matching.
    Weight: 0.25.
    Match threshold: similarity >= 0.75 → matched=True.

    Handles:
        Honorifics stripped before comparison.
        "Blessing Adeyemi" vs "Blessing Adeyemi-Okafor" → 0.88 → match
        "Blessing Adeyemi" vs "Mrs Blessing Adeyemi"    → 0.84 → match
        "Blessing Adeyemi" vs "Emeka Okonkwo"           → 0.12 → no match
    '''

    WEIGHT          = 0.25
    MATCH_THRESHOLD = 0.75

    def compare(self, order_a, order_b) -> DimensionScore:
        '''
        Compare customer names between two ParsedOrders.

        Args:
            order_a: ParsedOrder (new order).
            order_b: ParsedOrder (existing order).

        Returns:
            DimensionScore with similarity ratio as score.
        '''
        name_a = getattr(order_a, 'customer_name', None)
        name_b = getattr(order_b, 'customer_name', None)

        if not name_a or not name_b:
            return DimensionScore(
                dimension="name",
                score    =0.0,
                weight   =self.WEIGHT,
                matched  =False,
                detail   ="One or both orders missing customer name",
            )

        norm_a  = normalize_for_comparison(name_a)
        norm_b  = normalize_for_comparison(name_b)
        ratio   = levenshtein_ratio(norm_a, norm_b)
        matched = ratio >= self.MATCH_THRESHOLD

        return DimensionScore(
            dimension="name",
            score    =ratio,
            weight   =self.WEIGHT,
            matched  =matched,
            detail   =f"{name_a!r} vs {name_b!r} → {ratio:.2f}",
        )
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/application/duplicate/address_matcher.py
# ================================================================
# PURPOSE:
#   Compares delivery addresses using fuzzy matching.
#   Weight: 0.15 — weakest signal, addresses vary most.
#   Threshold: 0.70 to count as an address match.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.duplicate.similarity import (
    levenshtein_ratio,
    normalize_for_comparison,
)
from app.oms.application.models.duplicate_result import DimensionScore


class AddressMatcher:
    '''
    Compares delivery addresses between two orders.
    Weight: 0.15 — lowest weight because addresses vary considerably.
    Match threshold: similarity >= 0.70.

    Addresses are more likely to differ even for the same customer
    (customer may type "Ikeja Lagos" vs "12 Allen Ave, Ikeja").
    Low weight prevents address similarity from dominating.
    '''

    WEIGHT          = 0.15
    MATCH_THRESHOLD = 0.70

    def compare(self, order_a, order_b) -> DimensionScore:
        '''
        Compare delivery addresses between two ParsedOrders.
        '''
        addr_a = getattr(order_a, 'delivery_address', None)
        addr_b = getattr(order_b, 'delivery_address', None)

        if not addr_a or not addr_b:
            return DimensionScore(
                dimension="address",
                score    =0.0,
                weight   =self.WEIGHT,
                matched  =False,
                detail   ="One or both orders missing delivery address",
            )

        norm_a  = normalize_for_comparison(addr_a)
        norm_b  = normalize_for_comparison(addr_b)
        ratio   = levenshtein_ratio(norm_a, norm_b)
        matched = ratio >= self.MATCH_THRESHOLD

        # Preview for detail (truncate long addresses)
        preview_a = addr_a[:30] + "..." if len(addr_a) > 30 else addr_a
        preview_b = addr_b[:30] + "..." if len(addr_b) > 30 else addr_b

        return DimensionScore(
            dimension="address",
            score    =ratio,
            weight   =self.WEIGHT,
            matched  =matched,
            detail   =f"{preview_a!r} vs {preview_b!r} → {ratio:.2f}",
        )
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/application/duplicate/fingerprint_matcher.py
# ================================================================
# PURPOSE:
#   Exact match on content fingerprint.
#   The Day 7 ParsedOrder inherits the fingerprint from the
#   Day 3 RawMessage. Two identical raw messages → same fingerprint.
#   Weight: treated as override — exact fingerprint match →
#   CONFIRMED_DUPLICATE regardless of other scores.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.application.models.duplicate_result import DimensionScore


class FingerprintMatcher:
    '''
    Exact content fingerprint comparison.
    Weight: not used in normal scoring — exact match overrides all.

    If two orders have the same fingerprint, they ARE the same message
    (same sender, same timestamp, same text). This is the strongest
    possible signal — instant CONFIRMED_DUPLICATE.
    '''

    WEIGHT = 1.0  # Not used in normal weighted sum — overrides

    def compare(self, order_a, order_b) -> DimensionScore:
        '''
        Compare content fingerprints.

        Fingerprint comes from: ParsedOrder → source message fingerprint.
        If the ParsedOrder doesn't have a fingerprint field directly,
        we use the order_id as a proxy (order_id is derived from fingerprint
        in the Day 4 parser: order_id = f"ORD-{fingerprint[:8].upper()}").

        Returns:
            DimensionScore with score 1.0 (exact match) or 0.0.
        '''
        # Try direct fingerprint attribute first
        fp_a = getattr(order_a, 'fingerprint', None)
        fp_b = getattr(order_b, 'fingerprint', None)

        # Fall back to order_id comparison
        if not fp_a:
            fp_a = getattr(order_a, 'order_id', "")
        if not fp_b:
            fp_b = getattr(order_b, 'order_id', "")

        matched = bool(fp_a and fp_b and fp_a == fp_b)

        return DimensionScore(
            dimension="fingerprint",
            score    =1.0 if matched else 0.0,
            weight   =self.WEIGHT,
            matched  =matched,
            detail   =(
                f"exact match: {fp_a!r}"
                if matched
                else f"{fp_a!r} ≠ {fp_b!r}"
            ),
        )
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/application/duplicate/duplicate_store.py
# ================================================================
# PURPOSE:
#   In-memory store of all processed orders and duplicate results.
#   The engine queries this store to find existing orders to compare
#   each new order against.
#   Day 10 persistence replaces this with a database query.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.oms.application.models.duplicate_group import DuplicateGroup
from app.oms.application.models.duplicate_result import DuplicateResult
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class DuplicateStore:
    '''
    In-memory registry of orders and duplicate detection results.

    Stores:
        Processed order snapshots (for comparison)
        All DuplicateResult pairwise comparisons
        All DuplicateGroup clusters

    Day 10 will provide a SQLite-backed implementation
    via IOrderRepository — same interface, persistent storage.

    Usage:
        store = DuplicateStore(window_hours=48)
        store.register_order(parsed_order)
        candidates = store.get_candidates(new_order)
        store.store_result(duplicate_result)
        store.store_group(group)
    '''

    def __init__(self, window_hours: float = 48.0):
        '''
        Args:
            window_hours: Only orders within this time window are
                          considered as potential duplicates.
                          Default: 48 hours.
        '''
        self._window_hours = window_hours
        self._orders:  list           = []   # List of ParsedOrder snapshots
        self._results: list[DuplicateResult] = []
        self._groups:  list[DuplicateGroup]  = []

    def register_order(self, order) -> None:
        '''
        Register a new order for future comparison.
        Called after an order passes validation.

        Args:
            order: ParsedOrder to register.
        '''
        self._orders.append(order)
        log.debug(
            f"DuplicateStore: registered order {order.order_id!r} "
            f"(total: {len(self._orders)})"
        )

    def get_candidates(self, new_order) -> list:
        '''
        Return existing orders within the time window.
        These are the orders to compare the new order against.

        Excludes the new order itself (by order_id).

        Args:
            new_order: ParsedOrder being checked.

        Returns:
            List of ParsedOrder objects within the time window.
        '''
        now            = datetime.now()
        cutoff         = now.timestamp() - (self._window_hours * 3600)
        candidates     = []

        for order in self._orders:
            if order.order_id == new_order.order_id:
                continue

            parsed_at  = getattr(order, 'parsed_at', now)
            order_time = parsed_at.timestamp() if hasattr(parsed_at, 'timestamp') else cutoff

            if order_time >= cutoff:
                candidates.append(order)

        log.debug(
            f"DuplicateStore: {len(candidates)} candidate(s) in window "
            f"({self._window_hours}h) for order {new_order.order_id!r}"
        )
        return candidates

    def get_returning_customers(self, new_order) -> list:
        '''
        Return orders OUTSIDE the time window with matching phone.
        These are classified as RETURNING_CUSTOMER, never duplicate.

        Args:
            new_order: ParsedOrder being checked.

        Returns:
            List of orders outside window with same phone.
        '''
        now        = datetime.now()
        cutoff     = now.timestamp() - (self._window_hours * 3600)
        returning  = []

        for order in self._orders:
            if order.order_id == new_order.order_id:
                continue

            parsed_at  = getattr(order, 'parsed_at', now)
            order_time = parsed_at.timestamp() if hasattr(parsed_at, 'timestamp') else cutoff

            if order_time < cutoff:
                returning.append(order)

        return returning

    def store_result(self, result: DuplicateResult) -> None:
        '''Store a pairwise comparison result.'''
        self._results.append(result)
        log.debug(f"DuplicateStore: stored result {result.summary()}")

    def store_group(self, group: DuplicateGroup) -> None:
        '''Store or update a duplicate group.'''
        existing = self.get_group_for_order(group.canonical_order_id)
        if existing:
            # Update existing group with new members
            for oid in group.member_order_ids:
                existing.add_member(oid)
        else:
            self._groups.append(group)
            log.info(
                f"DuplicateStore: new group {group.group_id!r} "
                f"({group.size} members)"
            )

    def get_group_for_order(self, order_id: str) -> Optional[DuplicateGroup]:
        '''Return the DuplicateGroup containing this order, or None.'''
        for group in self._groups:
            if group.has_member(order_id):
                return group
        return None

    def results_for_order(self, order_id: str) -> list[DuplicateResult]:
        '''All pairwise results involving a given order.'''
        return [
            r for r in self._results
            if r.order_id_a == order_id or r.order_id_b == order_id
        ]

    def stats(self) -> dict:
        return {
            "total_orders":   len(self._orders),
            "total_results":  len(self._results),
            "total_groups":   len(self._groups),
            "window_hours":   self._window_hours,
            "duplicates":     sum(1 for r in self._results if r.is_duplicate),
        }
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/app/oms/application/duplicate/duplicate_detection_engine.py
# ================================================================
# PURPOSE:
#   The core Duplicate Detection Engine.
#   Compares each new order against all existing orders in the
#   time window using the four matchers.
#   Produces DuplicateResult per comparison and groups duplicates.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.oms.application.duplicate.phone_matcher import PhoneMatcher
from app.oms.application.duplicate.name_matcher import NameMatcher
from app.oms.application.duplicate.address_matcher import AddressMatcher
from app.oms.application.duplicate.fingerprint_matcher import FingerprintMatcher
from app.oms.application.duplicate.duplicate_store import DuplicateStore
from app.oms.application.models.duplicate_result import (
    DuplicateResult, DuplicateClassification, DimensionScore
)
from app.oms.application.models.duplicate_group import DuplicateGroup
from app.oms.application.models.validation_report import ValidationFlag
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


# ── Score thresholds ─────────────────────────────────────────────
THRESHOLD_CONFIRMED = 0.85
THRESHOLD_LIKELY    = 0.60
THRESHOLD_POSSIBLE  = 0.35


class DuplicateDetectionEngine:
    '''
    Detects duplicate orders by comparing new orders against existing ones.

    For each new ValidatedOrder:
        1. Register the new order in the store
        2. Get all candidates within the time window
        3. Compare against each candidate using all four matchers
        4. Classify each comparison
        5. Group confirmed/likely duplicates
        6. Set ValidationFlag.DUPLICATE_PENDING on duplicates
        7. Emit events
        8. Return all DuplicateResult for this order

    Usage:
        engine = DuplicateDetectionEngine(window_hours=48)
        results = await engine.check(validated_order)
    '''

    def __init__(self, window_hours: float = 48.0):
        '''
        Args:
            window_hours: Time window for duplicate detection.
                          Orders older than this are RETURNING_CUSTOMER.
        '''
        self._store   = DuplicateStore(window_hours=window_hours)
        self._phone   = PhoneMatcher()
        self._name    = NameMatcher()
        self._address = AddressMatcher()
        self._finger  = FingerprintMatcher()
        self._window  = window_hours

    async def check(self, validated_order) -> list[DuplicateResult]:
        '''
        Check a ValidatedOrder for duplicates against existing orders.

        Args:
            validated_order: ValidatedOrder from Day 8.

        Returns:
            List of DuplicateResult — one per candidate compared.
            Empty if no candidates in window or no duplicates found.
        '''
        parsed = validated_order.parsed_order
        log.info(
            f"DuplicateDetection: checking order {parsed.order_id!r} "
            f"(window={self._window}h)"
        )

        # Register this order for future comparisons
        self._store.register_order(parsed)

        # Get candidates to compare against
        candidates = self._store.get_candidates(parsed)

        if not candidates:
            log.debug(
                f"DuplicateDetection: no candidates in window — "
                f"order {parsed.order_id!r} is UNIQUE"
            )
            await dispatcher.emit(
                "duplicate.check.unique",
                order_id   =parsed.order_id,
                candidates =0,
            )
            return []

        results = []

        for candidate in candidates:
            result = await self._compare(parsed, candidate)
            results.append(result)
            self._store.store_result(result)

            if result.is_duplicate:
                await self._handle_duplicate(validated_order, result)

        # Check outside window — returning customer detection
        await self._check_returning_customers(parsed)

        # Summary log
        duplicates = [r for r in results if r.is_duplicate]
        if duplicates:
            log.warning(
                f"DuplicateDetection: {len(duplicates)} duplicate(s) found "
                f"for order {parsed.order_id!r}"
            )
        else:
            log.info(
                f"DuplicateDetection: order {parsed.order_id!r} is UNIQUE "
                f"({len(candidates)} candidate(s) checked)"
            )

        await dispatcher.emit(
            "duplicate.check.complete",
            order_id          =parsed.order_id,
            candidates_checked=len(candidates),
            duplicates_found  =len(duplicates),
            results           =[r.summary() for r in results],
        )

        return results

    async def _compare(self, order_a, order_b) -> DuplicateResult:
        '''
        Compare two ParsedOrders across all four dimensions.
        Returns a single DuplicateResult with weighted final score
        and classification.
        '''
        parsed_at_a = getattr(order_a, 'parsed_at', datetime.now())
        parsed_at_b = getattr(order_b, 'parsed_at', datetime.now())

        hours_apart   = abs(
            (parsed_at_a - parsed_at_b).total_seconds()
        ) / 3600
        within_window = hours_apart <= self._window

        # ── Fingerprint check first (fast exit) ──────────────────
        fp_score = self._finger.compare(order_a, order_b)
        if fp_score.matched:
            # Exact same message — immediately CONFIRMED
            return DuplicateResult(
                order_id_a    =order_a.order_id,
                order_id_b    =order_b.order_id,
                classification=DuplicateClassification.CONFIRMED_DUPLICATE,
                final_score   =1.0,
                dimensions    =[fp_score],
                hours_apart   =hours_apart,
                within_window =within_window,
            )

        # ── Run all matchers ──────────────────────────────────────
        phone_score   = self._phone.compare(order_a, order_b)
        name_score    = self._name.compare(order_a, order_b)
        address_score = self._address.compare(order_a, order_b)

        dimensions = [fp_score, phone_score, name_score, address_score]

        # ── Compute weighted final score ──────────────────────────
        # Weights: phone=0.60, name=0.25, address=0.15
        # (fp_score excluded from weighted sum — handled separately)
        final_score = (
            phone_score.score   * phone_score.weight
            + name_score.score  * name_score.weight
            + address_score.score * address_score.weight
        )
        final_score = round(final_score, 4)

        # ── Classify ──────────────────────────────────────────────
        if not within_window:
            classification = DuplicateClassification.UNIQUE
        elif final_score >= THRESHOLD_CONFIRMED:
            classification = DuplicateClassification.CONFIRMED_DUPLICATE
        elif final_score >= THRESHOLD_LIKELY:
            classification = DuplicateClassification.LIKELY_DUPLICATE
        elif final_score >= THRESHOLD_POSSIBLE:
            classification = DuplicateClassification.POSSIBLE_DUPLICATE
        else:
            classification = DuplicateClassification.UNIQUE

        log.debug(
            f"  Compared {order_a.order_id!r} vs {order_b.order_id!r}: "
            f"score={final_score:.2f} → {classification.value} | "
            f"phone={phone_score.score:.2f} "
            f"name={name_score.score:.2f} "
            f"addr={address_score.score:.2f}"
        )

        return DuplicateResult(
            order_id_a    =order_a.order_id,
            order_id_b    =order_b.order_id,
            classification=classification,
            final_score   =final_score,
            dimensions    =dimensions,
            hours_apart   =hours_apart,
            within_window =within_window,
        )

    async def _handle_duplicate(
        self,
        validated_order,
        result: DuplicateResult,
    ) -> None:
        '''
        Handle a confirmed or likely duplicate.
        Creates or updates DuplicateGroup.
        Sets ValidationFlag.DUPLICATE_PENDING on the validated order.
        Emits events.
        '''
        parsed_order = validated_order.parsed_order
        order_id_a   = result.order_id_a
        order_id_b   = result.order_id_b

        # Find or create the duplicate group
        existing_group = (
            self._store.get_group_for_order(order_id_b)
            or self._store.get_group_for_order(order_id_a)
        )

        if existing_group:
            existing_group.add_member(order_id_a)
            existing_group.add_member(order_id_b)
            result.group_id = existing_group.group_id
            self._store.store_group(existing_group)
        else:
            # canonical = older order (order_id_b is already in store)
            group = DuplicateGroup(
                canonical_order_id=order_id_b,
                classification    =result.classification.value,
            )
            group.add_member(order_id_a)
            result.group_id = group.group_id
            self._store.store_group(group)

        # Set DUPLICATE_PENDING flag on the validation report
        if hasattr(validated_order, 'report') and validated_order.report:
            validated_order.report.add_flag(ValidationFlag.DUPLICATE_PENDING)

        # Emit event
        event_name = (
            "duplicate.confirmed"
            if result.classification == DuplicateClassification.CONFIRMED_DUPLICATE
            else "duplicate.likely"
        )

        await dispatcher.emit(
            event_name,
            order_id_a    =order_id_a,
            order_id_b    =order_id_b,
            classification=result.classification.value,
            final_score   =result.final_score,
            matched_on    =result.matched_dimensions(),
            hours_apart   =result.hours_apart,
            group_id      =result.group_id,
        )

        log.warning(
            f"{'🔴' if result.classification == DuplicateClassification.CONFIRMED_DUPLICATE else '🟡'} "
            f"Duplicate {result.classification.value}: "
            f"{order_id_a!r} matches {order_id_b!r} "
            f"(score={result.final_score:.2f}, "
            f"matched={result.matched_dimensions()})"
        )

    async def _check_returning_customers(self, parsed_order) -> None:
        '''
        Check orders outside the time window for phone matches.
        These are RETURNING_CUSTOMER — not duplicates.
        Emits an informational event.
        '''
        old_orders = self._store.get_returning_customers(parsed_order)
        if not old_orders:
            return

        for old_order in old_orders:
            phone_score = self._phone.compare(parsed_order, old_order)
            if phone_score.matched:
                log.info(
                    f"Returning customer detected: "
                    f"order {parsed_order.order_id!r} "
                    f"matches +{phone_score.detail} "
                    f"from order {old_order.order_id!r} "
                    f"(outside {self._window}h window)"
                )
                await dispatcher.emit(
                    "duplicate.returning_customer",
                    new_order_id =parsed_order.order_id,
                    old_order_id =old_order.order_id,
                    customer     =parsed_order.customer_name,
                )

    def stats(self) -> dict:
        return self._store.stats()
"""


# ==============================================================
# ================================================================
#  FILE 10
#  PATH: windwhirl/app/oms/application/duplicate/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.duplicate.similarity import (
    levenshtein_distance,
    levenshtein_ratio,
    normalize_for_comparison,
    phone_normalize,
)
from app.oms.application.duplicate.phone_matcher import PhoneMatcher
from app.oms.application.duplicate.name_matcher import NameMatcher
from app.oms.application.duplicate.address_matcher import AddressMatcher
from app.oms.application.duplicate.fingerprint_matcher import FingerprintMatcher
from app.oms.application.duplicate.duplicate_store import DuplicateStore
from app.oms.application.duplicate.duplicate_detection_engine import (
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
"""


# ==============================================================
# ================================================================
#  FILE 11
#  PATH: windwhirl/app/oms/application/models/__init__.py  (UPDATE)
# ================================================================
# Add Day 9 models to the existing models __init__.py
# ================================================================
# ==============================================================

"""
# ADD to existing models/__init__.py:

from app.oms.application.models.duplicate_result import (
    DuplicateResult,
    DuplicateClassification,
    DimensionScore,
)
from app.oms.application.models.duplicate_group import DuplicateGroup

# ADD to __all__:
# "DuplicateResult", "DuplicateClassification", "DimensionScore",
# "DuplicateGroup",
"""


# ==============================================================
# ================================================================
#  FILE 12
#  PATH: windwhirl/app/oms/tests/test_duplicate_detection.py
# ================================================================
# Unit tests. Run: python -m pytest app/oms/tests/test_duplicate_detection.py -v
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from app.oms.application.duplicate.similarity import (
    levenshtein_distance, levenshtein_ratio, normalize_for_comparison, phone_normalize
)
from app.oms.application.duplicate.phone_matcher import PhoneMatcher
from app.oms.application.duplicate.name_matcher import NameMatcher
from app.oms.application.duplicate.address_matcher import AddressMatcher
from app.oms.application.duplicate.duplicate_detection_engine import (
    DuplicateDetectionEngine, THRESHOLD_CONFIRMED, THRESHOLD_LIKELY
)
from app.oms.application.models.parsed_order import ParsedOrder, PackageInfo
from app.oms.application.models.validated_order import ValidatedOrder
from app.oms.application.models.validation_report import ValidationReport
from app.oms.application.models.duplicate_result import DuplicateClassification


# ── Helpers ──────────────────────────────────────────────────────

def make_order(
    order_id: str,
    customer: str = "Blessing Adeyemi",
    phone:    str = "08031234567",
    wa:       str = "08031234567",
    address:  str = "12 Allen Avenue, Ikeja Lagos",
    **kwargs,
) -> ParsedOrder:
    return ParsedOrder(
        order_id        =order_id,
        worker_number   ="2348XXX",
        customer_name   =customer,
        phone_number    =phone,
        whatsapp_number =wa,
        package         =PackageInfo("1 Combo Set", "", "#29,500", 29500.0),
        delivery_address=address,
        raw_text        ="raw text",
        parsed_at       =kwargs.get('parsed_at', datetime.now()),
    )


def make_validated(order: ParsedOrder) -> ValidatedOrder:
    return ValidatedOrder(
        parsed_order=order,
        report      =ValidationReport(),
    )


async def check(order_a, order_b=None, window=48.0):
    engine = DuplicateDetectionEngine(window_hours=window)
    va = make_validated(order_a)

    if order_b:
        # Register order_b first so it's in the store
        engine._store.register_order(order_b)

    return await engine.check(va)


# ── Levenshtein ───────────────────────────────────────────────────

def test_levenshtein_identical():
    assert levenshtein_distance("blessing", "blessing") == 0


def test_levenshtein_empty():
    assert levenshtein_distance("", "abc") == 3
    assert levenshtein_distance("abc", "") == 3


def test_levenshtein_ratio_identical():
    assert levenshtein_ratio("Blessing Adeyemi", "Blessing Adeyemi") == 1.0


def test_levenshtein_ratio_completely_different():
    assert levenshtein_ratio("Blessing Adeyemi", "Emeka Okonkwo") < 0.30


def test_levenshtein_ratio_similar_names():
    ratio = levenshtein_ratio(
        normalize_for_comparison("Blessing Adeyemi"),
        normalize_for_comparison("Blessing Adeyemi-Okafor"),
    )
    assert ratio >= 0.75, f"Expected >= 0.75, got {ratio}"


def test_levenshtein_ratio_honorific_stripped():
    ratio = levenshtein_ratio(
        normalize_for_comparison("Mrs Blessing Adeyemi"),
        normalize_for_comparison("Blessing Adeyemi"),
    )
    assert ratio >= 0.80, f"Expected >= 0.80, got {ratio}"


# ── Phone normalize ───────────────────────────────────────────────

def test_phone_normalize_international():
    assert phone_normalize("+2348031234567") == "08031234567"
    assert phone_normalize("2348031234567")  == "08031234567"


def test_phone_normalize_local():
    assert phone_normalize("08031234567") == "08031234567"


def test_phone_normalize_float():
    # Excel artifact
    assert phone_normalize("8031234567.0") == "8031234567"


# ── Phone matcher ─────────────────────────────────────────────────

def test_phone_match_same():
    pm = PhoneMatcher()
    a  = make_order("A", phone="08031234567")
    b  = make_order("B", phone="08031234567")
    r  = pm.compare(a, b)
    assert r.matched is True
    assert r.score == 1.0


def test_phone_match_different():
    pm = PhoneMatcher()
    a  = make_order("A", phone="08031234567")
    b  = make_order("B", phone="08099999999")
    r  = pm.compare(a, b)
    assert r.matched is False
    assert r.score == 0.0


def test_phone_match_via_whatsapp():
    '''Phone in A matches WhatsApp in B.'''
    pm = PhoneMatcher()
    a  = make_order("A", phone="08031234567", wa="08031234567")
    b  = make_order("B", phone="08099999999", wa="08031234567")
    r  = pm.compare(a, b)
    assert r.matched is True


# ── Name matcher ──────────────────────────────────────────────────

def test_name_match_identical():
    nm = NameMatcher()
    a  = make_order("A", customer="Blessing Adeyemi")
    b  = make_order("B", customer="Blessing Adeyemi")
    r  = nm.compare(a, b)
    assert r.matched is True
    assert r.score == 1.0


def test_name_match_similar():
    nm    = NameMatcher()
    a     = make_order("A", customer="Blessing Adeyemi")
    b     = make_order("B", customer="Blessing Adeyemi-Okafor")
    r     = nm.compare(a, b)
    assert r.score >= 0.75


def test_name_no_match_different():
    nm = NameMatcher()
    a  = make_order("A", customer="Blessing Adeyemi")
    b  = make_order("B", customer="Emeka Okonkwo")
    r  = nm.compare(a, b)
    assert r.matched is False


def test_name_match_with_honorific():
    nm = NameMatcher()
    a  = make_order("A", customer="Mrs Blessing Adeyemi")
    b  = make_order("B", customer="Blessing Adeyemi")
    r  = nm.compare(a, b)
    assert r.score >= 0.80


# ── Address matcher ───────────────────────────────────────────────

def test_address_match_identical():
    am = AddressMatcher()
    a  = make_order("A", address="12 Allen Avenue, Ikeja Lagos")
    b  = make_order("B", address="12 Allen Avenue, Ikeja Lagos")
    r  = am.compare(a, b)
    assert r.matched is True


def test_address_no_match_different():
    am = AddressMatcher()
    a  = make_order("A", address="12 Allen Avenue, Ikeja Lagos")
    b  = make_order("B", address="5 Broad Street, Lagos Island")
    r  = am.compare(a, b)
    assert r.matched is False


# ── Full engine — confirmed duplicate ─────────────────────────────

@pytest.mark.asyncio
async def test_confirmed_duplicate_same_order():
    '''Same phone + same name + same address → CONFIRMED.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")

    results = await check(a, b)

    assert len(results) == 1
    assert results[0].classification == DuplicateClassification.CONFIRMED_DUPLICATE
    assert results[0].final_score >= THRESHOLD_CONFIRMED


@pytest.mark.asyncio
async def test_likely_duplicate_phone_only():
    '''Same phone, different name → LIKELY.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")
    b = make_order("B", phone="08031234567", customer="Mrs Blessing",
                   address="Ikeja, Lagos")

    results = await check(a, b)

    assert len(results) == 1
    assert results[0].classification in (
        DuplicateClassification.CONFIRMED_DUPLICATE,
        DuplicateClassification.LIKELY_DUPLICATE,
    )


@pytest.mark.asyncio
async def test_unique_different_phone():
    '''Different phone, different name → UNIQUE.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")
    b = make_order("B", phone="07099999999", customer="Emeka Okonkwo",
                   address="5 Broad Street, Lagos Island")

    results = await check(a, b)

    assert len(results) == 1
    assert results[0].classification == DuplicateClassification.UNIQUE


@pytest.mark.asyncio
async def test_outside_window_not_duplicate():
    '''Same phone but outside time window → UNIQUE (returning customer).'''
    old_time = datetime.now() - timedelta(hours=72)
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi",
                   parsed_at=old_time)

    # b is outside the 48h window
    results = await check(a, b, window=48.0)

    assert len(results) == 0 or all(
        r.classification == DuplicateClassification.UNIQUE
        for r in results
    )


@pytest.mark.asyncio
async def test_no_candidates_returns_empty():
    '''New order with no existing orders → empty results.'''
    a       = make_order("A")
    engine  = DuplicateDetectionEngine(window_hours=48)
    va      = make_validated(a)
    results = await engine.check(va)
    assert results == []


@pytest.mark.asyncio
async def test_duplicate_flag_set_on_validated_order():
    '''DUPLICATE_PENDING flag set on ValidatedOrder when duplicate found.'''
    from app.oms.application.models.validation_report import ValidationFlag

    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi")

    engine = DuplicateDetectionEngine(window_hours=48)
    engine._store.register_order(b)
    va = make_validated(a)
    await engine.check(va)

    assert ValidationFlag.DUPLICATE_PENDING in va.report.flags


@pytest.mark.asyncio
async def test_group_created_for_duplicates():
    '''Duplicate group created when duplicates found.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi")

    engine = DuplicateDetectionEngine(window_hours=48)
    engine._store.register_order(b)
    va = make_validated(a)
    await engine.check(va)

    group = engine._store.get_group_for_order("A")
    assert group is not None
    assert group.has_member("A")
    assert group.has_member("B")


@pytest.mark.asyncio
async def test_stats():
    a = make_order("A")
    b = make_order("B")

    engine = DuplicateDetectionEngine()
    await engine.check(make_validated(a))
    await engine.check(make_validated(b))

    s = engine.stats()
    assert s["total_orders"] >= 2
"""


# ==============================================================
# DAY 9 VERIFICATION
# ==============================================================
#
# Test 1 — Imports:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.duplicate import (
#       DuplicateDetectionEngine, levenshtein_ratio,
#       PhoneMatcher, NameMatcher, AddressMatcher
#   )
#   from app.oms.application.models.duplicate_result import (
#       DuplicateResult, DuplicateClassification
#   )
#   from app.oms.application.models.duplicate_group import DuplicateGroup
#   print('All Day 9 imports OK')
#   "
#
# Test 2 — Quick similarity check:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.duplicate.similarity import (
#       levenshtein_ratio, normalize_for_comparison, phone_normalize
#   )
#
#   pairs = [
#       ('Blessing Adeyemi', 'Blessing Adeyemi'),
#       ('Blessing Adeyemi', 'Blessing Adeyemi-Okafor'),
#       ('Mrs Blessing Adeyemi', 'Blessing Adeyemi'),
#       ('Blessing Adeyemi', 'Emeka Okonkwo'),
#   ]
#
#   for a, b in pairs:
#       r = levenshtein_ratio(
#           normalize_for_comparison(a),
#           normalize_for_comparison(b)
#       )
#       print(f'{r:.2f}  {a!r} vs {b!r}')
#
#   print()
#   print('Phone normalize:')
#   for p in ['+2348031234567', '2348031234567', '08031234567']:
#       print(f'  {p!r} → {phone_normalize(p)!r}')
#   "
#
# Test 3 — Run all unit tests:
#   python -m pytest app/oms/tests/test_duplicate_detection.py -v
#   Expected: 22+ tests PASSED
#
# ==============================================================
# WHAT DAY 10 BUILDS
# ==============================================================
# Day 10: Persistence Layer (SQLite + Export)
#   Listens to "assignment.resolved" + "duplicate.confirmed" events.
#   Persists:
#     - ValidatedOrder fields to orders table
#     - AssignmentHistoryEntry to assignments table
#     - DuplicateGroup to duplicate_groups table
#   Queries:
#     - get_by_worker(number) → list of orders
#     - get_pending() → unresolved orders
#     - get_today() → today's orders
#     - export_excel(date_range) → Excel file
#   Day 10 replaces DuplicateStore's in-memory candidates
#   with database queries — same interface, persistent storage.
# ==============================================================