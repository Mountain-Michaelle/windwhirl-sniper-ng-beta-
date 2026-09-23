from __future__ import annotations

import re
from typing import Optional


class CampaignExtractor:
    '''
    Extracts product campaign name from the message.

    Campaign format: *Campaign Name* at the start of the message.
    Example: *Tiktok Sadoer*

    Also handles plain first-line campaigns without asterisks
    if they match known campaign keywords.
    '''

    # Known campaign keywords for fallback detection
    KNOWN_CAMPAIGNS = [
        "tiktok sadoer",
        "facebook sadoer",
        "instagram sadoer",
        "body lotion",
        "sadoer",
        "collagen",
    ]

    # Asterisk pattern: *some text*
    ASTERISK_PATTERN = re.compile(r'\*([^*]+)\*')

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''
        Extract campaign name from message.

        Strategy:
          1. Look for *Campaign* asterisk pattern at start of message
          2. Fall back to known campaign keywords in first few lines
        '''
        # Strategy 1: asterisk pattern anywhere in first 3 lines
        first_lines = "\n".join(text.strip().splitlines()[:3])
        match = self.ASTERISK_PATTERN.search(first_lines)
        if match:
            campaign = match.group(1).strip()
            if campaign:
                return campaign

        # Strategy 2: known campaign keywords in first line
        first_line = text.strip().splitlines()[0].strip().lower() if text.strip() else ""
        for known in self.KNOWN_CAMPAIGNS:
            if known in first_line:
                return text.strip().splitlines()[0].strip()

        return None
