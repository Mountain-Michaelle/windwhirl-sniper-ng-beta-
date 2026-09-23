from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional, Tuple


class DateExtractor:
    '''
    Extracts order date from the message and normalizes if possible.

    Label variants:
        "order date"
        "date"
        "ordered on"

    Date format handling:
        "3rd July"       → date(current_year, 7, 3)
        "03/07/2024"     → date(2024, 7, 3)
        "July 3"         → date(current_year, 7, 3)
        "3 July 2024"    → date(2024, 7, 3)

    If normalization fails: preserves raw string, date=None.
    '''

    DATE_LABELS = [
        "order date",
        "ordered on",
        "date",
    ]

    MONTH_MAP = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    def extract(self, text: str, sections: dict) -> Tuple[Optional[str], Optional[date]]:
        '''
        Extract order date.

        Returns:
            Tuple of (raw_string, date_object).
            raw_string is always the original text if found.
            date_object is None if normalization failed.
        '''
        raw = self._find_section(sections)
        if not raw:
            return None, None

        raw_value = raw.strip().splitlines()[0].strip()
        if not raw_value:
            return None, None

        # Try to parse the date
        parsed = self._try_parse(raw_value)
        return raw_value, parsed

    def _try_parse(self, raw: str) -> Optional[date]:
        '''Attempt to parse a date string. Returns None on failure.'''
        clean = raw.strip().lower()
        # Remove ordinal suffixes: 3rd → 3, 1st → 1, etc.
        clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', clean)

        # Try common formats
        formats = [
            "%d %B %Y",   # "3 July 2024"
            "%d %b %Y",   # "3 Jul 2024"
            "%B %d %Y",   # "July 3 2024"
            "%b %d %Y",   # "Jul 3 2024"
            "%d/%m/%Y",   # "03/07/2024"
            "%d-%m-%Y",   # "03-07-2024"
            "%Y-%m-%d",   # "2024-07-03"
            "%d %B",      # "3 July" (no year — use current)
            "%d %b",      # "3 Jul"
            "%B %d",      # "July 3"
            "%b %d",      # "Jul 3"
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(clean, fmt)
                if dt.year == 1900:
                    # No year in format — use current year
                    dt = dt.replace(year=datetime.now().year)
                return dt.date()
            except ValueError:
                continue

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.DATE_LABELS:
            if label in sections:
                return sections[label]
        return None
