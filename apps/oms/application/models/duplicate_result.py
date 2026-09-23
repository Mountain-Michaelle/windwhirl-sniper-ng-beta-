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
