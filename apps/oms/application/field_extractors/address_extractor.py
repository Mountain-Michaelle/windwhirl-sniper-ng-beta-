from __future__ import annotations

from typing import Optional


class AddressExtractor:
    '''
    Extracts full delivery address. May be multi-line.

    Label variants (normalized):
        "input full address"
        "full address"
        "delivery address"
        "address"
        "location"
    '''

    ADDRESS_LABELS = [
        "input full address",
        "input your full address",
        "input address",
        "full address",
        "delivery address",
        "address",
        "location",
        "deliver to",
        "delivery location",
    ]

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''
        Extract delivery address as written.
        Preserves multi-line addresses exactly.
        Never truncates.
        '''
        raw = self._find_section(sections)
        if not raw:
            return None

        value = raw.strip()
        if len(value) >= 3:
            return value

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.ADDRESS_LABELS:
            if label in sections:
                return sections[label]
        return None
