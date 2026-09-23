from __future__ import annotations

import re
from typing import Optional


class CustomerExtractor:
    '''
    Extracts customer full name from order text.

    Label variants (normalized):
        "input your full name"
        "full name"
        "customer name"
        "name"
        "customer"
    '''

    CUSTOMER_LABELS = [
        "input your full name",
        "input full name",
        "full name",
        "customer name",
        "customer",
        "name",
        "client name",
        "client",
        "buyer",
    ]

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''
        Extract customer name. Returns the value as-is (never normalized).

        Args:
            text:     Full raw message text.
            sections: Pre-parsed sections dict.

        Returns:
            Customer name string, or None if not found.
        '''
        raw = self._find_section(sections)
        if not raw:
            return None

        # Take only the first line (name is never multi-line)
        name = raw.strip().splitlines()[0].strip() if raw.strip() else None

        # Basic sanity: must have at least 2 characters
        if name and len(name.strip()) >= 2:
            return name.strip()

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.CUSTOMER_LABELS:
            if label in sections:
                return sections[label]
        return None
