from __future__ import annotations

from apps.oms.application.duplicate.similarity import (
    levenshtein_ratio,
    normalize_for_comparison,
)
from apps.oms.application.models.duplicate_result import DimensionScore


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
