from __future__ import annotations

import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.oms.application.models.parsed_order import PackageInfo


class PackageExtractor:
    '''
    Extracts package name, description, and price from order text.

    Handles multi-line package descriptions:
        1 Combo set -(1 serum & 1 Cream)
        + Free Doorstep Delivery = #29,500
    '''

    # Normalized label variants that introduce the package field
    PACKAGE_LABELS = [
        "select your package",
        "select package",
        "package",
        "order",
        "product",
        "item",
        "choose your package",
    ]

    # Price pattern: optional currency symbol, digits, optional commas
    PRICE_PATTERN = re.compile(
        r'[#₦Nn]?\s*(\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?'
    )

    def extract(self, text: str, sections: dict) -> Optional["PackageInfo"]:
        '''
        Extract package information from message sections.

        Args:
            text:     Full raw message text.
            sections: Pre-parsed label → value dict from OrderParser.

        Returns:
            PackageInfo if package section found, else None.
        '''
        from app.oms.application.models.parsed_order import PackageInfo

        raw_value = self._find_section(sections)
        if not raw_value:
            return None

        lines = [l.strip() for l in raw_value.strip().splitlines() if l.strip()]
        if not lines:
            return None

        # First line is the package name
        name = lines[0]

        # Remaining lines are description
        description = " ".join(lines[1:]) if len(lines) > 1 else ""

        # Extract price from the full package text
        full_text   = raw_value
        price_raw   = ""
        price_value = None

        price_match = re.search(
            r'[=#]\s*[#₦Nn]?\s*([\d,]+(?:\.\d{2})?)',
            full_text
        )
        if price_match:
            price_raw = price_match.group(0).strip()
            try:
                price_value = float(price_match.group(1).replace(",", ""))
            except ValueError:
                pass
        else:
            # Try finding any number that looks like a price
            all_prices = self.PRICE_PATTERN.findall(full_text)
            if all_prices:
                # Take the last number found (likely the total price)
                try:
                    price_str   = all_prices[-1].replace(",", "")
                    price_value = float(price_str)
                    price_raw   = all_prices[-1]
                except ValueError:
                    pass

        return PackageInfo(
            name       =name,
            description=description,
            price_raw  =price_raw,
            price_value=price_value,
        )

    def _find_section(self, sections: dict) -> Optional[str]:
        '''Find the package section in the parsed sections dict.'''
        for label in self.PACKAGE_LABELS:
            if label in sections:
                return sections[label]
        return None
