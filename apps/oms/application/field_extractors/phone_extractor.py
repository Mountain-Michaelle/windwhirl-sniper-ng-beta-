from __future__ import annotations

import re
from typing import Optional


class PhoneExtractor:
    '''
    Extracts phone number from order text. No validation — extraction only.

    Label variants (normalized):
        "input phone number"
        "phone number"
        "phone"
        "tel"
        "telephone"
        "mobile"
    '''

    PHONE_LABELS = [
        "input phone number",
        "phone number",
        "phone no",
        "phone",
        "telephone",
        "tel",
        "mobile",
        "mobile number",
        "contact number",
        "contact",
    ]

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''
        Extract phone number as written. No normalization of value.

        Returns:
            Phone number string as found, or None.
        '''
        raw = self._find_section(sections)
        if not raw:
            return None

        # Take first line only
        value = raw.strip().splitlines()[0].strip()

        # Must contain at least some digits to be a phone number
        if value and re.search(r'\d', value):
            return value

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.PHONE_LABELS:
            if label in sections:
                return sections[label]
        return None
