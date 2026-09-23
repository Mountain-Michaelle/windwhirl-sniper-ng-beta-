from __future__ import annotations

from apps.oms.application.duplicate.similarity import (
    levenshtein_ratio,
    normalize_for_comparison,
)
from apps.oms.application.models.duplicate_result import DimensionScore


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
