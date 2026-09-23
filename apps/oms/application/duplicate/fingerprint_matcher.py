from __future__ import annotations

from apps.oms.application.models.duplicate_result import DimensionScore


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
