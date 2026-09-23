from __future__ import annotations

from typing import Optional


class DeliveryExtractor:
    '''
    Extracts delivery timing request from the customer.
    Preserves exact customer wording — never interprets dates.

    Label variants (normalized):
        "when do you want us to deliver"
        "delivery date"
        "delivery time"
        "when to deliver"
    '''

    DELIVERY_LABELS = [
        "when do you want us to deliver",
        "when do you want delivery",
        "delivery date",
        "delivery time",
        "when to deliver",
        "preferred delivery date",
        "preferred delivery",
        "delivery",
        "when",
    ]

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''
        Extract delivery request as written.
        Examples: "Today", "Tomorrow", "Monday", "Next Week"
        '''
        raw = self._find_section(sections)
        if not raw:
            return None

        value = raw.strip().splitlines()[0].strip()
        if value:
            return value

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.DELIVERY_LABELS:
            if label in sections:
                return sections[label]
        return None
