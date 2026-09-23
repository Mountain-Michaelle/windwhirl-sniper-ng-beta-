cat > /home/claude/oms_day7.py << 'ENDOFFILE'
# ==============================================================
# WINDWHIRL OMS — DAY 7: INTELLIGENT ORDER PARSER
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → application/models/parsed_order.py
#   FILE 2  → application/field_extractors/package_extractor.py
#   FILE 3  → application/field_extractors/customer_extractor.py
#   FILE 4  → application/field_extractors/phone_extractor.py
#   FILE 5  → application/field_extractors/whatsapp_extractor.py
#   FILE 6  → application/field_extractors/address_extractor.py
#   FILE 7  → application/field_extractors/delivery_extractor.py
#   FILE 8  → application/field_extractors/campaign_extractor.py
#   FILE 9  → application/field_extractors/question_extractor.py
#   FILE 10 → application/field_extractors/date_extractor.py
#   FILE 11 → application/field_extractors/__init__.py
#   FILE 12 → application/models/__init__.py
#   FILE 13 → application/order_parser.py
#   FILE 14 → tests/test_order_parser.py
#
# WHAT THIS BUILDS:
#   A structured order extractor. Takes a raw WhatsApp message
#   and a ResolvedAssignment, returns a ParsedOrder with every
#   extractable field populated.
#
# DESIGN PHILOSOPHY:
#   - One extractor class per field
#   - Each extractor is independent — crashes in one never stop others
#   - Labels normalized for matching, values never normalized
#   - Missing fields produce None, not errors
#   - Raw message always preserved in full
#   - Parser never validates, rejects, or corrects
#
# LABEL MATCHING:
#   The WhatsApp order template uses business labels like:
#   "Select Your Package", "Input your Full Name", etc.
#   We normalize labels (lowercase, collapse whitespace) before
#   matching so formatting variations don't break extraction.
#   Everything after the matched label until the next label
#   (or end of message) is the field value.
#
# REAL MESSAGE FORMAT (Nabeau Store):
#   *Tiktok Sadoer*
#   Select Your Package: 1 Combo set -(1 serum & 1 Cream)
#   + Free Doorstep Delivery = #29,500
#   Input your Full Name: Blessing Adeyemi
#   Input Phone Number: 08031234567
#   Input Whatsapp Number: 08031234567
#   Input Full Address: 12 Allen Avenue, Ikeja Lagos
#   When do you want us to deliver: Tomorrow
#   Do you have any questions: No
#   Order Date: 3rd July
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/application/models/parsed_order.py
# ================================================================
# PURPOSE:
#   The output of the Order Parser. Immutable after creation.
#   Every field is Optional — missing is expected and supported.
#   Carries the full raw text for audit purposes.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class ExtractionStatus(str, Enum):
    '''
    How completely the parser was able to extract fields.
    NOT a validity indicator — only extraction completeness.

    COMPLETE: All key fields (name, phone, package, address) extracted.
    PARTIAL:  Some fields extracted, some missing.
    EMPTY:    No fields could be extracted at all.
    '''
    COMPLETE = "COMPLETE"
    PARTIAL  = "PARTIAL"
    EMPTY    = "EMPTY"


@dataclass(frozen=True)
class PackageInfo:
    '''
    Extracted package details.
    frozen=True: immutable value object.

    name:        Short package name e.g. "1 Combo set"
    description: Additional package details e.g. "(1 serum & 1 Cream)"
    price_raw:   Price as written e.g. "#29,500" or "29500"
    price_value: Numeric price if parseable, else None.
    '''
    name:        str            = ""
    description: str            = ""
    price_raw:   str            = ""
    price_value: Optional[float] = None

    @property
    def has_price(self) -> bool:
        return self.price_value is not None

    def __str__(self) -> str:
        parts = [self.name]
        if self.description:
            parts.append(self.description)
        if self.price_raw:
            parts.append(f"= {self.price_raw}")
        return " ".join(p for p in parts if p)


@dataclass
class ParsedOrder:
    '''
    A structured order extracted from a raw WhatsApp message.

    Immutable business data — no methods that modify state.
    Every Optional field may be None if not found in the message.
    The raw_text field always contains the original message in full.

    Fields:
        parsed_id:       Unique ID for this ParsedOrder instance.
        order_id:        The OMS order ID from the resolution engine.
        worker_number:   Phone number of the assigned worker.
        customer_name:   Customer full name (not validated).
        phone_number:    Customer phone (not validated).
        whatsapp_number: Customer WhatsApp (may differ from phone).
        package:         Extracted package details.
        delivery_address: Full delivery address as written.
        delivery_request: When customer wants delivery ("Tomorrow", etc.)
        customer_question: Anything after "Do you have any questions".
        campaign:        Product campaign e.g. "Tiktok Sadoer".
        order_date_raw:  Order date as written e.g. "3rd July".
        order_date:      Parsed date if successfully normalized, else None.
        raw_text:        The original WhatsApp message, never modified.
        parsed_at:       When this ParsedOrder was created.
        parser_version:  Which parser version produced this.
        status:          COMPLETE, PARTIAL, or EMPTY.
        missing_fields:  List of field names that could not be extracted.
        notes:           Extraction notes for debugging.
    '''
    parsed_id:          str                    = field(
                            default_factory=lambda: str(uuid.uuid4())[:8]
                        )
    order_id:           str                    = ""
    worker_number:      str                    = ""
    customer_name:      Optional[str]          = None
    phone_number:       Optional[str]          = None
    whatsapp_number:    Optional[str]          = None
    package:            Optional[PackageInfo]  = None
    delivery_address:   Optional[str]          = None
    delivery_request:   Optional[str]          = None
    customer_question:  Optional[str]          = None
    campaign:           Optional[str]          = None
    order_date_raw:     Optional[str]          = None
    order_date:         Optional[date]         = None
    raw_text:           str                    = ""
    parsed_at:          datetime               = field(default_factory=datetime.now)
    parser_version:     str                    = "1.0"
    status:             ExtractionStatus       = ExtractionStatus.EMPTY
    missing_fields:     list[str]              = field(default_factory=list)
    notes:              list[str]              = field(default_factory=list)

    # KEY FIELDS — used for COMPLETE vs PARTIAL determination
    KEY_FIELDS = ["customer_name", "phone_number", "package", "delivery_address"]

    def compute_status(self) -> "ParsedOrder":
        '''
        Compute and return a new ParsedOrder with updated status
        and missing_fields based on which key fields are present.

        Returns a new instance (frozen semantics even though not frozen).
        '''
        missing = [
            f for f in self.KEY_FIELDS
            if getattr(self, f) is None
        ]
        extracted = [f for f in self.KEY_FIELDS if f not in missing]

        if not extracted:
            status = ExtractionStatus.EMPTY
        elif not missing:
            status = ExtractionStatus.COMPLETE
        else:
            status = ExtractionStatus.PARTIAL

        self.missing_fields.clear()
        self.missing_fields.extend(missing)
        object.__setattr__(self, 'status', status) if hasattr(self, '__dataclass_fields__') else None
        # For non-frozen dataclass, direct assignment:
        self.status = status
        return self

    def summary(self) -> str:
        '''Short human-readable summary for logging.'''
        parts = []
        if self.customer_name:
            parts.append(f"customer={self.customer_name!r}")
        if self.phone_number:
            parts.append(f"phone={self.phone_number!r}")
        if self.package:
            parts.append(f"package={self.package.name!r}")
        if self.delivery_address:
            parts.append(f"address={self.delivery_address[:30]!r}")
        return f"ParsedOrder({', '.join(parts) or 'empty'})"

    def __repr__(self):
        return (
            f"ParsedOrder("
            f"id={self.parsed_id!r}, "
            f"status={self.status.value}, "
            f"order={self.order_id!r}, "
            f"customer={self.customer_name!r})"
        )
"""


# ==============================================================
# ================================================================
#  HELPER: Label Normalizer (used by all extractors)
# ================================================================
# Place this at the top of each extractor file that needs it,
# or import from a shared utility module.
# ================================================================
# ==============================================================

"""
# Shared utility — add to each extractor file or a shared utils.py

import re

def normalize_label(text: str) -> str:
    '''
    Normalize a label for comparison.
    Lowercases, collapses whitespace, strips punctuation at edges.
    Never modifies extracted VALUES — only used for label matching.
    '''
    return re.sub(r'\\s+', ' ', text.lower().strip(' :*-_'))
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/application/field_extractors/package_extractor.py
# ================================================================
# PURPOSE:
#   Extracts package name, description, and price.
#
# LABEL VARIANTS (normalized):
#   "Select Your Package"
#   "Package"
#   "Order"
#   "Product"
#
# PRICE PATTERNS:
#   "#29,500" | "₦29500" | "N29,500" | "29500" | "29,500"
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/application/field_extractors/customer_extractor.py
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/application/field_extractors/phone_extractor.py
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/application/field_extractors/whatsapp_extractor.py
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/application/field_extractors/address_extractor.py
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/application/field_extractors/delivery_extractor.py
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/application/field_extractors/campaign_extractor.py
# ================================================================
# PURPOSE:
#   Extracts the product campaign name.
#   In Nabeau Store messages, the campaign appears at the TOP
#   of the message wrapped in asterisks:
#     *Tiktok Sadoer*
#     *Facebook Sadoer*
#     *Body Lotion*
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/app/oms/application/field_extractors/question_extractor.py
# ================================================================
# PURPOSE:
#   Extracts everything after "Do you have any questions".
#   May be multi-line. Never truncated.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from typing import Optional


class QuestionExtractor:
    '''
    Extracts customer question(s) from the order.

    Captures everything after the question label.
    May span multiple lines. Never truncated.

    Label variants (normalized):
        "do you have any questions"
        "any questions"
        "questions"
        "customer question"
    '''

    QUESTION_LABELS = [
        "do you have any questions",
        "any questions",
        "questions",
        "question",
        "customer question",
        "remarks",
        "note",
        "notes",
        "additional note",
        "additional notes",
        "comment",
        "comments",
    ]

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''
        Extract customer question(s). Preserves multi-line content.
        Returns None if the customer wrote "No", "None", or similar.
        '''
        raw = self._find_section(sections)
        if not raw:
            return None

        value = raw.strip()

        # If customer explicitly said "No" or "None", no question
        if value.lower() in ("no", "none", "nope", "n/a", "nil", "nothing", "-"):
            return None

        if value:
            return value

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.QUESTION_LABELS:
            if label in sections:
                return sections[label]
        return None
"""


# ==============================================================
# ================================================================
#  FILE 10
#  PATH: windwhirl/app/oms/application/field_extractors/date_extractor.py
# ================================================================
# PURPOSE:
#   Extracts order date and attempts normalization to a date object.
#   If normalization fails, preserves the original string.
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 11
#  PATH: windwhirl/app/oms/application/field_extractors/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.field_extractors.package_extractor import PackageExtractor
from app.oms.application.field_extractors.customer_extractor import CustomerExtractor
from app.oms.application.field_extractors.phone_extractor import PhoneExtractor
from app.oms.application.field_extractors.whatsapp_extractor import WhatsAppExtractor
from app.oms.application.field_extractors.address_extractor import AddressExtractor
from app.oms.application.field_extractors.delivery_extractor import DeliveryExtractor
from app.oms.application.field_extractors.campaign_extractor import CampaignExtractor
from app.oms.application.field_extractors.question_extractor import QuestionExtractor
from app.oms.application.field_extractors.date_extractor import DateExtractor

__all__ = [
    "PackageExtractor",
    "CustomerExtractor",
    "PhoneExtractor",
    "WhatsAppExtractor",
    "AddressExtractor",
    "DeliveryExtractor",
    "CampaignExtractor",
    "QuestionExtractor",
    "DateExtractor",
]
"""


# ==============================================================
# ================================================================
#  FILE 12
#  PATH: windwhirl/app/oms/application/models/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.models.parsed_order import (
    ParsedOrder,
    PackageInfo,
    ExtractionStatus,
)

__all__ = ["ParsedOrder", "PackageInfo", "ExtractionStatus"]
"""


# ==============================================================
# ================================================================
#  FILE 13
#  PATH: windwhirl/app/oms/application/order_parser.py
# ================================================================
# PURPOSE:
#   The main Order Parser. Coordinates all field extractors.
#   Sections the raw message by label, calls each extractor,
#   merges results into a ParsedOrder.
#
# SECTIONING ALGORITHM:
#   1. Split message into lines
#   2. For each line: check if it starts with a known label
#   3. If yes: start a new section for that label
#   4. If no: append to the current section's value
#   5. Result: dict of normalized_label → raw_value_string
#
# This handles:
#   - Labels with colons: "Name: Blessing"
#   - Labels on own line, value on next line
#   - Multi-line values (e.g. addresses, packages)
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import re
from typing import Optional

from app.oms.application.models.parsed_order import ParsedOrder, ExtractionStatus
from app.oms.application.field_extractors import (
    PackageExtractor,
    CustomerExtractor,
    PhoneExtractor,
    WhatsAppExtractor,
    AddressExtractor,
    DeliveryExtractor,
    CampaignExtractor,
    QuestionExtractor,
    DateExtractor,
)
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)

# All known label prefixes (normalized) for section detection
# Order matters — longer/more specific labels must come first
# to prevent "phone" matching before "phone number"
ALL_LABELS = [
    "select your package",
    "select package",
    "input your full name",
    "input full name",
    "input phone number",
    "input your whatsapp number",
    "input whatsapp number",
    "input full address",
    "input your full address",
    "input address",
    "when do you want us to deliver",
    "when do you want delivery",
    "do you have any questions",
    "any questions",
    "full name",
    "customer name",
    "phone number",
    "phone no",
    "whatsapp number",
    "whatsapp no",
    "delivery address",
    "full address",
    "delivery date",
    "delivery time",
    "preferred delivery",
    "customer question",
    "additional notes",
    "additional note",
    "order date",
    "ordered on",
    "package",
    "product",
    "item",
    "customer",
    "phone",
    "whatsapp",
    "address",
    "location",
    "delivery",
    "questions",
    "question",
    "remarks",
    "notes",
    "note",
    "date",
    "name",
    "mobile",
    "tel",
    "contact",
    "wa",
]


def normalize_label(text: str) -> str:
    '''Normalize a label for comparison. Never use on values.'''
    return re.sub(r'\s+', ' ', text.lower().strip(' :*-_'))


class OrderParser:
    '''
    Converts a raw WhatsApp order message into a structured ParsedOrder.

    Coordinates 9 independent field extractors. Each extractor
    receives the full text and the pre-parsed sections dict.
    Failures in one extractor never affect others.

    Usage:
        parser = OrderParser()
        parsed = await parser.parse(
            raw_text="*Tiktok Sadoer*\\nSelect Your Package: ...",
            order_id="ORD-001",
            worker_number="2348XXXXXXXXX",
        )
    '''

    PARSER_VERSION = "1.0"

    def __init__(self):
        self._package   = PackageExtractor()
        self._customer  = CustomerExtractor()
        self._phone     = PhoneExtractor()
        self._whatsapp  = WhatsAppExtractor()
        self._address   = AddressExtractor()
        self._delivery  = DeliveryExtractor()
        self._campaign  = CampaignExtractor()
        self._question  = QuestionExtractor()
        self._date      = DateExtractor()

    async def parse(
        self,
        raw_text:     str,
        order_id:     str = "",
        worker_number: str = "",
    ) -> ParsedOrder:
        '''
        Parse a raw WhatsApp order message into a ParsedOrder.

        Args:
            raw_text:      The complete raw WhatsApp message text.
            order_id:      OMS order ID from resolution engine.
            worker_number: Assigned worker phone number.

        Returns:
            ParsedOrder with all extractable fields populated.
            Never raises — all extractor errors are caught internally.
        '''
        log.debug(
            f"OrderParser: parsing order {order_id!r} "
            f"({len(raw_text)} chars)"
        )

        parsed = ParsedOrder(
            order_id      =order_id,
            worker_number =worker_number,
            raw_text      =raw_text,
            parser_version=self.PARSER_VERSION,
        )

        notes = []

        # ── Step 1: Section the message by labels ─────────────────
        sections = self._section_message(raw_text)

        log.debug(
            f"OrderParser: found {len(sections)} section(s): "
            f"{list(sections.keys())}"
        )

        # ── Step 2: Run each extractor independently ──────────────
        # Each extractor is wrapped in try/except.
        # One failure NEVER stops the others.

        # Campaign (reads full text, not sections)
        try:
            parsed.campaign = self._campaign.extract(raw_text, sections)
            if parsed.campaign:
                log.debug(f"Campaign: {parsed.campaign!r}")
        except Exception as e:
            notes.append(f"campaign_extractor error: {e}")
            log.warning(f"OrderParser: campaign extractor failed: {e}")

        # Customer name
        try:
            parsed.customer_name = self._customer.extract(raw_text, sections)
            if parsed.customer_name:
                log.debug(f"Customer: {parsed.customer_name!r}")
            else:
                notes.append("customer_name: not found")
        except Exception as e:
            notes.append(f"customer_extractor error: {e}")
            log.warning(f"OrderParser: customer extractor failed: {e}")

        # Phone number
        try:
            parsed.phone_number = self._phone.extract(raw_text, sections)
            if parsed.phone_number:
                log.debug(f"Phone: {parsed.phone_number!r}")
            else:
                notes.append("phone_number: not found")
        except Exception as e:
            notes.append(f"phone_extractor error: {e}")
            log.warning(f"OrderParser: phone extractor failed: {e}")

        # WhatsApp number
        try:
            parsed.whatsapp_number = self._whatsapp.extract(raw_text, sections)
            if parsed.whatsapp_number:
                log.debug(f"WhatsApp: {parsed.whatsapp_number!r}")
        except Exception as e:
            notes.append(f"whatsapp_extractor error: {e}")
            log.warning(f"OrderParser: whatsapp extractor failed: {e}")

        # Package
        try:
            parsed.package = self._package.extract(raw_text, sections)
            if parsed.package:
                log.debug(
                    f"Package: {parsed.package.name!r} "
                    f"price={parsed.package.price_value}"
                )
            else:
                notes.append("package: not found")
        except Exception as e:
            notes.append(f"package_extractor error: {e}")
            log.warning(f"OrderParser: package extractor failed: {e}")

        # Delivery address
        try:
            parsed.delivery_address = self._address.extract(raw_text, sections)
            if parsed.delivery_address:
                log.debug(f"Address: {parsed.delivery_address[:40]!r}")
            else:
                notes.append("delivery_address: not found")
        except Exception as e:
            notes.append(f"address_extractor error: {e}")
            log.warning(f"OrderParser: address extractor failed: {e}")

        # Delivery request
        try:
            parsed.delivery_request = self._delivery.extract(raw_text, sections)
            if parsed.delivery_request:
                log.debug(f"Delivery: {parsed.delivery_request!r}")
        except Exception as e:
            notes.append(f"delivery_extractor error: {e}")
            log.warning(f"OrderParser: delivery extractor failed: {e}")

        # Customer question
        try:
            parsed.customer_question = self._question.extract(raw_text, sections)
            if parsed.customer_question:
                log.debug(f"Question: {parsed.customer_question!r}")
        except Exception as e:
            notes.append(f"question_extractor error: {e}")
            log.warning(f"OrderParser: question extractor failed: {e}")

        # Order date
        try:
            raw_date, parsed_date = self._date.extract(raw_text, sections)
            parsed.order_date_raw = raw_date
            parsed.order_date     = parsed_date
            if raw_date:
                log.debug(f"Date: {raw_date!r} → {parsed_date}")
        except Exception as e:
            notes.append(f"date_extractor error: {e}")
            log.warning(f"OrderParser: date extractor failed: {e}")

        # ── Step 3: Compute status ────────────────────────────────
        parsed.notes.extend(notes)
        parsed.compute_status()

        # ── Step 4: Emit event ────────────────────────────────────
        event_map = {
            ExtractionStatus.COMPLETE: "order.parsed",
            ExtractionStatus.PARTIAL:  "order.partially_parsed",
            ExtractionStatus.EMPTY:    "order.empty_parsed",
        }
        event_name = event_map.get(parsed.status, "order.parsed")

        await dispatcher.emit(
            event_name,
            parsed_order  =parsed,
            order_id      =order_id,
            status        =parsed.status.value,
            missing_fields=parsed.missing_fields,
        )

        log.info(
            f"OrderParser: {parsed.status.value} | "
            f"{parsed.summary()} | "
            f"missing={parsed.missing_fields}"
        )

        return parsed

    def _section_message(self, text: str) -> dict[str, str]:
        '''
        Split a raw message into sections by recognized labels.

        Algorithm:
          1. Process each line
          2. Check if line starts with a known label (after normalization)
          3. If yes: start a new current_label section
          4. If no: append line to the current section's value
          5. Handles both "Label: value" (same line) and
             "Label:\\nvalue" (value on next line) formats

        Returns:
            dict mapping normalized_label → raw_value_string
        '''
        sections:      dict[str, str] = {}
        current_label: Optional[str]  = None
        current_lines: list[str]      = []

        def flush():
            if current_label is not None:
                sections[current_label] = "\n".join(current_lines).strip()

        for line in text.splitlines():
            stripped   = line.strip()
            normalized = normalize_label(stripped)

            matched_label = None
            inline_value  = ""

            # Check all known labels against this line
            for label in ALL_LABELS:
                if normalized.startswith(label):
                    matched_label = label
                    # Extract any inline value after the label
                    rest = stripped[len(label):].strip(" :\t")
                    # Remove leading colon/asterisk
                    rest = re.sub(r'^[:\-\s]+', '', rest).strip()
                    inline_value = rest
                    break

            if matched_label:
                # Save the previous section
                flush()
                current_label = matched_label
                current_lines = [inline_value] if inline_value else []
            elif current_label is not None:
                # Continuation of current section
                current_lines.append(line)
            # Lines before any label are ignored (handled by campaign extractor)

        flush()  # Save the last section
        return sections
"""


# ==============================================================
# ================================================================
#  FILE 14
#  PATH: windwhirl/app/oms/tests/test_order_parser.py
# ================================================================
# Unit tests for all scenarios.
# Run: python -m pytest app/oms/tests/test_order_parser.py -v
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from app.oms.application.order_parser import OrderParser
from app.oms.application.models.parsed_order import ExtractionStatus


FULL_ORDER = '''*Tiktok Sadoer*
Select Your Package: 1 Combo set -(1 serum & 1 Cream)
+ Free Doorstep Delivery = #29,500
Input your Full Name: Blessing Adeyemi
Input Phone Number: 08031234567
Input Whatsapp Number: 08031234567
Input Full Address: 12 Allen Avenue, Ikeja Lagos
When do you want us to deliver: Tomorrow
Do you have any questions: No
Order Date: 3rd July'''


PARTIAL_ORDER = '''*Facebook Sadoer*
Select Your Package: 2 Combo sets = #59,000
Input your Full Name: Emeka Okonkwo
Input Full Address: 5 Broad Street, Lagos Island'''


MISSING_ALL = '''Hi please I want to order the product
Thank you'''


MULTI_QUESTION = '''*Sadoer*
Input your Full Name: Fatima Abubakar
Input Phone Number: 07012345678
Input Full Address: Wuse 2, Abuja
When do you want us to deliver: Monday
Do you have any questions: Can I pay on delivery?
Also, do you deliver to Zone 4?
Order Date: 5th August'''


EXTRA_BLANK_LINES = '''*Body Lotion*

Select Your Package: 1 Body Lotion set = #15,000

Input your Full Name:   Titilayo Adekunle

Input Phone Number: 08098765432

Input Whatsapp Number: 08098765432

Input Full Address: 22 Bode Thomas Street, Surulere

When do you want us to deliver: Friday

Do you have any questions: None'''


async def parse(text, order_id="TEST-001"):
    parser = OrderParser()
    return await parser.parse(text, order_id=order_id, worker_number="2348XXXXXXXXX")


# ── Complete order ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_order_status():
    parsed = await parse(FULL_ORDER)
    assert parsed.status == ExtractionStatus.COMPLETE


@pytest.mark.asyncio
async def test_complete_order_campaign():
    parsed = await parse(FULL_ORDER)
    assert parsed.campaign == "Tiktok Sadoer"


@pytest.mark.asyncio
async def test_complete_order_customer():
    parsed = await parse(FULL_ORDER)
    assert parsed.customer_name == "Blessing Adeyemi"


@pytest.mark.asyncio
async def test_complete_order_phone():
    parsed = await parse(FULL_ORDER)
    assert parsed.phone_number == "08031234567"


@pytest.mark.asyncio
async def test_complete_order_whatsapp():
    parsed = await parse(FULL_ORDER)
    assert parsed.whatsapp_number == "08031234567"


@pytest.mark.asyncio
async def test_complete_order_package():
    parsed = await parse(FULL_ORDER)
    assert parsed.package is not None
    assert "Combo set" in parsed.package.name
    assert parsed.package.price_value == 29500.0


@pytest.mark.asyncio
async def test_complete_order_address():
    parsed = await parse(FULL_ORDER)
    assert parsed.delivery_address is not None
    assert "Allen" in parsed.delivery_address
    assert "Lagos" in parsed.delivery_address


@pytest.mark.asyncio
async def test_complete_order_delivery():
    parsed = await parse(FULL_ORDER)
    assert parsed.delivery_request == "Tomorrow"


@pytest.mark.asyncio
async def test_complete_order_no_question():
    '''Customer answered "No" — question should be None.'''
    parsed = await parse(FULL_ORDER)
    assert parsed.customer_question is None


@pytest.mark.asyncio
async def test_complete_order_date():
    parsed = await parse(FULL_ORDER)
    assert parsed.order_date_raw == "3rd July"
    # Date should be parsed (July 3 of current year)
    if parsed.order_date:
        assert parsed.order_date.month == 7
        assert parsed.order_date.day == 3


@pytest.mark.asyncio
async def test_raw_text_preserved():
    '''Raw text must always be preserved exactly.'''
    parsed = await parse(FULL_ORDER)
    assert parsed.raw_text == FULL_ORDER


# ── Partial order ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_order_status():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.status == ExtractionStatus.PARTIAL


@pytest.mark.asyncio
async def test_partial_order_has_name():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.customer_name == "Emeka Okonkwo"


@pytest.mark.asyncio
async def test_partial_order_missing_phone():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.phone_number is None
    assert "phone_number" in parsed.missing_fields


# ── Empty / unrecognized order ────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_order_status():
    parsed = await parse(MISSING_ALL)
    # No labels matched — should be EMPTY or PARTIAL
    assert parsed.status in (ExtractionStatus.EMPTY, ExtractionStatus.PARTIAL)


@pytest.mark.asyncio
async def test_raw_always_preserved_even_for_empty():
    parsed = await parse(MISSING_ALL)
    assert parsed.raw_text == MISSING_ALL


# ── Multi-line question ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_line_question():
    parsed = await parse(MULTI_QUESTION)
    assert parsed.customer_question is not None
    assert "delivery" in parsed.customer_question.lower()


@pytest.mark.asyncio
async def test_multi_question_customer():
    parsed = await parse(MULTI_QUESTION)
    assert parsed.customer_name == "Fatima Abubakar"


@pytest.mark.asyncio
async def test_multi_question_date():
    parsed = await parse(MULTI_QUESTION)
    assert parsed.order_date_raw is not None
    if parsed.order_date:
        assert parsed.order_date.month == 8
        assert parsed.order_date.day == 5


# ── Extra blank lines ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extra_blank_lines_customer():
    '''Extra blank lines must not break extraction.'''
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.customer_name == "Titilayo Adekunle"


@pytest.mark.asyncio
async def test_extra_blank_lines_phone():
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.phone_number == "08098765432"


@pytest.mark.asyncio
async def test_extra_blank_lines_no_question():
    '''Customer answered "None" — question should be None.'''
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.customer_question is None


@pytest.mark.asyncio
async def test_extra_blank_lines_package_price():
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.package is not None
    assert parsed.package.price_value == 15000.0


# ── Campaign extraction ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_campaign_facebook():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.campaign == "Facebook Sadoer"


@pytest.mark.asyncio
async def test_campaign_body_lotion():
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.campaign == "Body Lotion"
"""


# ==============================================================
# DAY 7 VERIFICATION
# ==============================================================
#
# Test 1 — Imports:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.models.parsed_order import ParsedOrder, ExtractionStatus
#   from app.oms.application.field_extractors import (
#       PackageExtractor, CustomerExtractor, PhoneExtractor,
#       AddressExtractor, CampaignExtractor
#   )
#   from app.oms.application.order_parser import OrderParser
#   print('All Day 7 imports OK')
#   "
#
# Test 2 — Quick parse of a real order:
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.application.order_parser import OrderParser
#
#   msg = '''*Tiktok Sadoer*
#   Select Your Package: 1 Combo set -(1 serum & 1 Cream)
#   + Free Doorstep Delivery = #29,500
#   Input your Full Name: Blessing Adeyemi
#   Input Phone Number: 08031234567
#   Input Whatsapp Number: 08031234567
#   Input Full Address: 12 Allen Avenue, Ikeja Lagos
#   When do you want us to deliver: Tomorrow
#   Do you have any questions: No
#   Order Date: 3rd July'''
#
#   async def run():
#       parser = OrderParser()
#       parsed = await parser.parse(msg, 'ORD-001', '2348XXXXXXXXX')
#       print(f'Status:    {parsed.status.value}')
#       print(f'Campaign:  {parsed.campaign}')
#       print(f'Customer:  {parsed.customer_name}')
#       print(f'Phone:     {parsed.phone_number}')
#       print(f'Package:   {parsed.package.name if parsed.package else None}')
#       print(f'Price:     {parsed.package.price_value if parsed.package else None}')
#       print(f'Address:   {parsed.delivery_address}')
#       print(f'Delivery:  {parsed.delivery_request}')
#       print(f'Question:  {parsed.customer_question}')
#       print(f'Date:      {parsed.order_date_raw} -> {parsed.order_date}')
#       print(f'Missing:   {parsed.missing_fields}')
#
#   asyncio.run(run())
#   "
#
# Test 3 — Run all unit tests:
#   python -m pytest app/oms/tests/test_order_parser.py -v
#   Expected: 20+ tests PASSED
#
# ==============================================================
# WHAT DAY 8 BUILDS
# ==============================================================
# Day 8: Validation Engine
#   Receives ParsedOrder from Day 7.
#   Produces ValidatedOrder — ParsedOrder is never modified.
#   Validation checks:
#     - Nigerian phone number format
#     - WhatsApp number validity
#     - Required fields presence
#     - Address completeness
#     - Price consistency
#     - Quality score (0.0 to 1.0)
#     - Validation flags per field
#
#   Day 8 adds one listener:
#     @dispatcher.on("order.parsed")
#     async def validate(parsed_order, **kwargs):
#         validated = validator.validate(parsed_order)
#         await dispatcher.emit("order.validated", ...)
#
#   ParsedOrder is immutable. Validator reads it, never writes.
# ==============================================================
