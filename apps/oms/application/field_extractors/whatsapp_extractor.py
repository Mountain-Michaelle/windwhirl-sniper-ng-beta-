from __future__ import annotations

import re
from typing import Optional


class WhatsAppExtractor:
    '''
    Extracts WhatsApp number — may differ from phone number.

    Label variants (normalized):
        "input whatsapp number"
        "whatsapp number"
        "whatsapp"
        "wa number"
    '''

    WHATSAPP_LABELS = [
        "input whatsapp number",
        "input your whatsapp number",
        "whatsapp number",
        "whatsapp no",
        "whatsapp",
        "wa number",
        "wa",
    ]

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''Extract WhatsApp number as written. No validation.'''
        raw = self._find_section(sections)
        if not raw:
            return None

        value = raw.strip().splitlines()[0].strip()
        if value and re.search(r'\d', value):
            return value

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.WHATSAPP_LABELS:
            if label in sections:
                return sections[label]
        return None
