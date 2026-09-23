from __future__ import annotations

from apps.oms.application.duplicate.similarity import phone_normalize
from apps.oms.application.models.duplicate_result import DimensionScore


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
