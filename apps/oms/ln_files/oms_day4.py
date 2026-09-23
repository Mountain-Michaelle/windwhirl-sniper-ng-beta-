# ==============================================================
# WINDWHIRL OMS — DAY 4: CLASSIFICATION, PARSING, VALIDATION
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → domain/value_objects.py          (new)
#   FILE 2  → domain/entities.py               (update Order)
#   FILE 3  → application/classifier.py        (new)
#   FILE 4  → application/parser.py            (new)
#   FILE 5  → application/validator.py         (new)
#   FILE 6  → application/assignment_engine.py (new)
#   FILE 7  → application/pipeline.py          (new — wires 1-4 together)
#   FILE 8  → application/__init__.py          (update)
#   FILE 9  → oms_runner.py                    (update — register pipeline)
#
# WHAT THIS DAY BUILDS:
#   The message processing pipeline. From RawMessage → Order.
#
#   Stage 1 — CLASSIFIER
#     Input:  RawMessage (text, sender, direction)
#     Output: MessageClass enum (ORDER | ASSIGNMENT | SYSTEM | UNKNOWN)
#     Method: keyword scoring + pattern matching
#     No ML. No external APIs. Pure Python regex.
#
#   Stage 2 — PARSER
#     Input:  RawMessage classified as ORDER
#     Output: Order domain entity (customer, items, phone, address)
#     Method: flexible regex tuned for Nigerian informal order language
#     Example inputs it handles:
#       "Customer: Blessing. Item: Sadoer combo x2. Address: Lagos Island"
#       "blessing adeyemi 08031234567 - 2 sadoer sets - ikeja"
#       "New order\nName: Mrs Fatima\nQty: 1 Sadoer\nPhone: 07012345678"
#       "pls add order for Emeka, wants collagen set, no 5 broad street"
#
#   Stage 3 — VALIDATOR
#     Input:  Parsed Order
#     Output: list[ValidationError] — empty means valid
#     Rules:  customer name present, at least one item, phone valid if given
#
#   Stage 4 — ASSIGNMENT ENGINE
#     Input:  Valid Order, available Staff list
#     Output: Staff (the one to assign to)
#     Today:  Always assigns to the single configured staff member
#     Future: Round-robin, load-balancing, skill-based
#
#   Stage 5 — PIPELINE
#     Wires stages 1-4 together.
#     Handles event emission at each stage.
#     Called by oms_runner.py on every "message.received" event.
#
# EVENT FLOW (extending Day 3):
#   "message.received"    → Pipeline.process()
#   "message.classified"  → emitted after Stage 1
#   "order.parsed"        → emitted after Stage 2 success
#   "order.parse_failed"  → emitted after Stage 2 failure
#   "order.validated"     → emitted after Stage 3 success
#   "order.invalid"       → emitted after Stage 3 failure
#   "order.assigned"      → emitted after Stage 4
#   "order.detected"      → emitted after full pipeline success
#
# ARCHITECTURAL RULE MAINTAINED:
#   Classifier, Parser, Validator, AssignmentEngine live in application/.
#   They implement domain interfaces (IParser, IValidator, etc.)
#   They have ZERO infrastructure imports.
#   No Playwright. No database. No browser. Pure Python logic.
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/domain/value_objects.py
# ================================================================
# PURPOSE:
#   Immutable value objects used by the Order entity.
#   Value objects have no identity — two objects with the same
#   values are considered equal. They represent concepts, not things.
#
#   CustomerInfo  — the customer's contact details
#   OrderItem     — a single product line in an order
#   PhoneNumber   — a normalized Nigerian phone number
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PhoneNumber:
    '''
    A normalized Nigerian phone number.
    frozen=True makes it immutable (value object behaviour).

    Stores in E.164 format: 2348XXXXXXXXX (13 digits, no +)
    Accepts any Nigerian format on creation and normalizes it.

    Usage:
        phone = PhoneNumber.from_raw("08031234567")
        phone.normalized   → "2348031234567"
        phone.display      → "+234 803 123 4567"
        phone.is_valid     → True

        invalid = PhoneNumber.from_raw("123")
        invalid.is_valid   → False
    '''
    normalized: str      # E.164 without + e.g. "2348031234567"
    is_valid:   bool

    @staticmethod
    def from_raw(raw: str) -> "PhoneNumber":
        '''
        Parse and normalize a raw phone number string.
        Handles all Nigerian formats:
          08031234567    → 2348031234567
          +2348031234567 → 2348031234567
          +08031234567   → 2348031234567
          2348031234567  → 2348031234567
          234-803-123-4567 → 2348031234567
        '''
        if not raw:
            return PhoneNumber(normalized="", is_valid=False)

        # Strip everything except digits
        digits = re.sub(r"[^\d]", "", raw.strip())

        # Apply normalization rules
        if digits.startswith("0") and not digits.startswith("234"):
            digits = "234" + digits[1:]
        elif not digits.startswith("234"):
            if len(digits) == 10:
                digits = "234" + digits

        # Validate: 13 digits starting with 234
        if len(digits) == 13 and digits.startswith("234"):
            return PhoneNumber(normalized=digits, is_valid=True)

        return PhoneNumber(normalized=digits, is_valid=False)

    @property
    def display(self) -> str:
        '''Human-readable format: +234 803 123 4567'''
        if not self.is_valid or len(self.normalized) != 13:
            return self.normalized
        n = self.normalized
        return f"+{n[:3]} {n[3:6]} {n[6:9]} {n[9:]}"

    def __str__(self) -> str:
        return self.display if self.is_valid else self.normalized

    def __bool__(self) -> bool:
        return self.is_valid


@dataclass(frozen=True)
class OrderItem:
    '''
    A single product line in an order.
    frozen=True: immutable value object.

    raw_text:    The original item text as parsed from the message.
                 e.g. "2 Sadoer Combo Set"
    product:     Normalized product name if recognized, else raw_text.
                 e.g. "Sadoer Collagen Combo Set"
    quantity:    How many units ordered. Default 1 if not specified.
    unit_price:  Price per unit if mentioned. 0 if not.
    '''
    raw_text:   str
    product:    str
    quantity:   int   = 1
    unit_price: float = 0.0

    @property
    def total_price(self) -> float:
        return self.quantity * self.unit_price

    def __str__(self) -> str:
        qty_str = f"{self.quantity}x " if self.quantity != 1 else ""
        return f"{qty_str}{self.product}"


@dataclass(frozen=True)
class CustomerInfo:
    '''
    A customer's contact and delivery information.
    frozen=True: immutable value object.

    All fields are optional — the parser extracts whatever
    is present in the message. Validator checks for required fields.
    '''
    name:    str            = ""
    phone:   PhoneNumber    = None
    address: str            = ""

    def __post_init__(self):
        # Ensure phone is always a PhoneNumber instance
        if self.phone is None:
            object.__setattr__(self, "phone", PhoneNumber("", False))
        elif isinstance(self.phone, str):
            object.__setattr__(self, "phone", PhoneNumber.from_raw(self.phone))

    @property
    def has_phone(self) -> bool:
        return self.phone is not None and self.phone.is_valid

    @property
    def has_address(self) -> bool:
        return bool(self.address.strip())

    def __str__(self) -> str:
        parts = [self.name]
        if self.has_phone:
            parts.append(str(self.phone))
        if self.has_address:
            parts.append(self.address)
        return " | ".join(p for p in parts if p)
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/domain/entities.py
# ================================================================
# UPDATE: Add customer and items fields to the Order entity.
# Find the existing Order dataclass and add these fields.
# The rest of entities.py stays exactly as it was in Day 1.
# ================================================================
# ADD these imports at the top of domain/entities.py:
# ==============================================================

"""
# Add to existing imports in domain/entities.py:
from app.oms.domain.value_objects import CustomerInfo, OrderItem, PhoneNumber
"""

# ADD these fields to the Order dataclass (after raw_text field):
"""
    customer:       CustomerInfo       = field(default_factory=CustomerInfo)
    items:          list[OrderItem]    = field(default_factory=list)
    total_price:    float              = 0.0
    delivery_address: str             = ""
    payment_method:   str             = ""
"""

# ADD this method to the Order class:
"""
    def item_summary(self) -> str:
        '''Human-readable summary of ordered items for logging.'''
        if not self.items:
            return "(no items parsed)"
        return ", ".join(str(item) for item in self.items)

    @property
    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/application/classifier.py
# ================================================================
# PURPOSE:
#   Stage 1 of the pipeline. Reads a RawMessage and decides what
#   TYPE of message it is before any expensive parsing happens.
#
# CLASSIFICATION TYPES:
#   ORDER       → A customer wants to buy something
#   ASSIGNMENT  → A coordinator is assigning a task to a staff member
#   SYSTEM      → WhatsApp system messages (join/leave/etc)
#   UNKNOWN     → Cannot determine — log and skip
#
# METHOD: Keyword scoring
#   Each MessageClass has a set of trigger keywords and patterns.
#   The message is scored against each class.
#   The highest-scoring class wins.
#   Minimum score threshold prevents false positives.
#
# WHY NOT ML:
#   Your order volume (dozens per day) is too small to train a
#   meaningful model. Keyword scoring with your actual vocabulary
#   ("sadoer", "collagen", "customer", "order") is more reliable,
#   faster, and fully explainable for debugging.
# ================================================================
# ==============================================================

"""
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.oms.domain.interfaces import IParser
from app.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class MessageClass(str, Enum):
    '''
    The type of message detected in the WhatsApp group.

    ORDER:      A customer wants to buy products.
                Parser will extract customer, items, phone, address.

    ASSIGNMENT: A coordinator is assigning an order/task to staff.
                e.g. "Michael please handle this customer"
                Parser will extract task details and assignee.

    STATUS:     A staff member reporting order status.
                e.g. "Order delivered to Blessing"
                Will update order status in future milestone.

    SYSTEM:     WhatsApp system messages.
                e.g. "Michael added to the group"
                Always ignored — no processing needed.

    UNKNOWN:    Could not determine message type.
                Logged and skipped.
    '''
    ORDER      = "ORDER"
    ASSIGNMENT = "ASSIGNMENT"
    STATUS     = "STATUS"
    SYSTEM     = "SYSTEM"
    UNKNOWN    = "UNKNOWN"


@dataclass
class ClassificationResult:
    '''
    The output of the Classifier for one message.

    message_class: The determined type of the message.
    confidence:    Score from 0.0 to 1.0 indicating certainty.
                   Below 0.3 = low confidence (treat as UNKNOWN).
    reasoning:     List of matched signals for debugging.
                   Helps diagnose missed or false classifications.
    '''
    message_class: MessageClass
    confidence:    float
    reasoning:     list[str] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        '''True if confidence is above the minimum threshold.'''
        return self.confidence >= 0.25

    def __repr__(self):
        return (
            f"ClassificationResult("
            f"class={self.message_class.value}, "
            f"confidence={self.confidence:.2f}, "
            f"signals={len(self.reasoning)})"
        )


class MessageClassifier:
    '''
    Classifies WhatsApp messages into ORDER, ASSIGNMENT, STATUS,
    SYSTEM, or UNKNOWN using keyword scoring.

    All classification is case-insensitive.
    Each keyword match adds weight to a class score.
    The class with the highest weight wins.

    Usage:
        classifier = MessageClassifier()
        result = classifier.classify(raw_message)
        if result.message_class == MessageClass.ORDER and result.is_confident:
            # send to parser
    '''

    # ── ORDER signals ───────────────────────────────────────────
    # High-weight: strong indicator this is an order
    # Low-weight:  supporting evidence, not conclusive alone
    ORDER_SIGNALS = [
        # Product names (high weight)
        (r"sadoer",                   0.4),
        (r"collagen\s*(set|combo|pack)?", 0.4),
        (r"face\s*(serum|cream)",     0.3),
        (r"nabeau",                   0.3),

        # Order intent keywords (medium weight)
        (r"\border\b",                0.3),
        (r"\bwants?\b",               0.2),
        (r"\bbuy\b",                  0.2),
        (r"\bpurchase",               0.2),
        (r"\bnew\s+customer\b",       0.3),
        (r"\bcustomer\b",             0.2),
        (r"\bplace\s+order\b",        0.4),
        (r"\badd\s+order\b",          0.4),

        # Quantity patterns (medium weight)
        (r"\b\d+\s*(piece|unit|set|pack|bottle)s?\b", 0.25),
        (r"\bx\s*\d+\b",              0.2),
        (r"\bqty\b",                  0.25),
        (r"\bquantity\b",             0.2),

        # Delivery fields (supporting evidence)
        (r"\baddress\b",              0.15),
        (r"\bdelivery\b",             0.15),
        (r"\blocation\b",             0.10),
        (r"\blagos|abuja|ph|port\s*harcourt|ibadan|kano|enugu\b", 0.10),

        # Contact fields (supporting evidence)
        (r"\bphone\b",                0.10),
        (r"\bnumber\b",               0.10),
        (r"0[789][01]\d{8}\b",        0.20),  # Nigerian phone pattern
    ]

    # ── ASSIGNMENT signals ──────────────────────────────────────
    ASSIGNMENT_SIGNALS = [
        (r"\bassign\b",               0.5),
        (r"\bhandle\s+this\b",        0.4),
        (r"\bplease\s+(take|handle|attend)", 0.4),
        (r"\byour\s+customer\b",      0.3),
        (r"\bfollow\s+up\b",          0.3),
        (r"\bresponsible\s+for\b",    0.3),
        (r"\btake\s+(care|over)\b",   0.3),
        (r"\bthis\s+(is\s+)?for\s+you\b", 0.3),
    ]

    # ── STATUS REPORT signals ───────────────────────────────────
    STATUS_SIGNALS = [
        (r"\bdelivered\b",            0.4),
        (r"\bpicked\s+up\b",          0.4),
        (r"\bsent\s+out\b",           0.35),
        (r"\bdispatched\b",           0.4),
        (r"\bdone\b",                 0.2),
        (r"\bcompleted\b",            0.3),
        (r"\bnot\s+(home|available)\b", 0.3),
        (r"\bno\s+response\b",        0.3),
        (r"\bcustomer\s+(said|confirmed|paid)", 0.35),
        (r"\bpayment\s+(received|confirmed|done)", 0.35),
    ]

    # ── SYSTEM MESSAGE signals ──────────────────────────────────
    SYSTEM_SIGNALS = [
        (r"added\s+\w+\s+to\s+the\s+group", 0.9),
        (r"removed\s+\w+\s+from",       0.9),
        (r"changed\s+the\s+subject",     0.9),
        (r"changed\s+the\s+(group\s+)?icon", 0.9),
        (r"left\s+the\s+group",          0.9),
        (r"joined\s+using\s+this\s+group", 0.9),
        (r"messages\s+and\s+calls\s+are\s+end.to.end\s+encrypted", 0.99),
        (r"you\s+created\s+this\s+group", 0.99),
    ]

    def classify(self, message: RawMessage) -> ClassificationResult:
        '''
        Classify a raw message into ORDER, ASSIGNMENT, STATUS,
        SYSTEM, or UNKNOWN.

        Args:
            message: The raw WhatsApp message to classify.

        Returns:
            ClassificationResult with class, confidence, and reasoning.
        '''
        text    = message.raw_text.lower().strip()
        reasons = []

        # ── System messages: check first, exit early ─────────────
        system_score = self._score(text, self.SYSTEM_SIGNALS, reasons, "SYSTEM")
        if system_score >= 0.8:
            return ClassificationResult(
                message_class=MessageClass.SYSTEM,
                confidence=min(1.0, system_score),
                reasoning=reasons
            )

        # ── Score all remaining classes ───────────────────────────
        order_score      = self._score(text, self.ORDER_SIGNALS,      [], "ORDER")
        assignment_score = self._score(text, self.ASSIGNMENT_SIGNALS, [], "ASSIGNMENT")
        status_score     = self._score(text, self.STATUS_SIGNALS,     [], "STATUS")

        # ── Pick the highest scoring class ───────────────────────
        scores = {
            MessageClass.ORDER:      order_score,
            MessageClass.ASSIGNMENT: assignment_score,
            MessageClass.STATUS:     status_score,
            MessageClass.UNKNOWN:    0.0,
        }

        winner, top_score = max(scores.items(), key=lambda x: x[1])

        # Collect reasoning for the winning class only
        if winner == MessageClass.ORDER:
            self._score(text, self.ORDER_SIGNALS, reasons, "ORDER")
        elif winner == MessageClass.ASSIGNMENT:
            self._score(text, self.ASSIGNMENT_SIGNALS, reasons, "ASSIGNMENT")
        elif winner == MessageClass.STATUS:
            self._score(text, self.STATUS_SIGNALS, reasons, "STATUS")

        # Minimum threshold — below this, call it UNKNOWN
        if top_score < 0.20:
            winner    = MessageClass.UNKNOWN
            top_score = 0.0
            reasons   = [f"No signals matched (top raw score: {top_score:.2f})"]

        result = ClassificationResult(
            message_class=winner,
            confidence=min(1.0, top_score),
            reasoning=reasons
        )

        log.debug(
            f"Classified: {result.message_class.value} "
            f"({result.confidence:.2f}) — {message.preview(50)!r}"
        )

        return result

    def _score(
        self,
        text:    str,
        signals: list[tuple],
        reasons: list[str],
        label:   str
    ) -> float:
        '''
        Score a text against a list of (pattern, weight) signals.
        Appends matched signal descriptions to reasons list.
        Returns total accumulated score.
        '''
        total = 0.0
        for pattern, weight in signals:
            if re.search(pattern, text, re.IGNORECASE):
                total += weight
                reasons.append(f"[{label}+{weight:.2f}] matched: {pattern!r}")
        return total
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/application/parser.py
# ================================================================
# PURPOSE:
#   Stage 2. Extracts structured order data from an ORDER message.
#   Returns an Order domain entity.
#
# INPUT REALITY:
#   Nigerian WhatsApp business messages are informal.
#   No fixed format. Abbreviations. Mixed English/Yoruba/Igbo/Pidgin.
#   The parser handles all common patterns seen in Nabeau Store orders.
#
# EXTRACTION TARGETS:
#   customer name, phone number, delivery address, items + quantities
#
# STRATEGY:
#   1. Try structured format (labelled fields: "Name:", "Phone:", etc.)
#   2. Fall back to positional extraction (first line = name, etc.)
#   3. Use product vocabulary to find items anywhere in the text
#   4. Scan all digit sequences for phone numbers
#   5. Treat remaining unmatched text as address candidates
#
# DESIGN RULE:
#   Parser never raises. It extracts what it can and returns
#   an Order with whatever was found. The Validator then checks
#   if what was found is sufficient for the order to proceed.
# ================================================================
# ==============================================================

"""
import re
import uuid
from datetime import datetime
from typing import Optional

from app.oms.domain.entities import Order, OrderStatus
from app.oms.domain.value_objects import CustomerInfo, OrderItem, PhoneNumber
from app.oms.domain.interfaces import IParser
from app.oms.infrastructure.browser.raw_message import RawMessage
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class OrderParser(IParser):
    '''
    Extracts structured order information from informal WhatsApp messages.
    Implements the IParser domain interface.

    Handles real Nabeau Store order message formats:
      Structured:   "Customer: Blessing Okafor\\nPhone: 08031234567\\n..."
      Semi-struct:  "blessing adeyemi - 2 sadoer sets - ikeja"
      Informal:     "pls add order for Emeka, wants collagen set"
      Mixed:        "New order\\nName: Mrs Fatima\\nQty: 1 Sadoer\\n..."

    Usage:
        parser = OrderParser(staff_number="2348XXXXXXXXX")
        order  = parser.parse(message, staff_number)
        if order is None:
            # Message does not contain enough info to make an order
    '''

    # ── Product vocabulary ──────────────────────────────────────
    # Maps any recognized variant to the canonical product name.
    # Add new products here as inventory grows.
    PRODUCT_MAP = {
        # Sadoer Collagen Combo Set variants
        r"sadoer\s*(collagen\s*)?(combo\s*)?(set|pack|bundle)?": "Sadoer Collagen Combo Set",
        r"collagen\s*(combo\s*)?(set|pack|bundle)?":             "Sadoer Collagen Combo Set",
        r"face\s*serum\s*(and|&|with)?\s*face\s*cream":         "Sadoer Collagen Combo Set",
        r"sadoer\s*(serum|cream)":                               "Sadoer Collagen Combo Set",
        r"collagen\s*(face\s*)?(serum|cream)":                   "Sadoer Collagen Combo Set",
        # Add more products here in future:
        # r"vitamin\s*c\s*serum": "Aesthtany Vitamin C Serum",
    }

    # ── Field label patterns ────────────────────────────────────
    # These match labelled fields like "Name: Blessing" or "Phone: 080..."
    LABEL_PATTERNS = {
        "name": [
            r"(?:customer|name|client|buyer)\s*[:\-]\s*(.+?)(?:\n|$|,|\|)",
            r"(?:for|to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b",
        ],
        "phone": [
            r"(?:phone|number|tel|call|contact|whatsapp)\s*[:\-]?\s*([+\d\s\-]{10,15})",
            r"\b((?:0|\+?234)[789][01]\d{8})\b",     # Nigerian phone anywhere
        ],
        "address": [
            r"(?:address|location|delivery|deliver\s+to|area)\s*[:\-]\s*(.+?)(?:\n|$)",
            r"(?:no\.?\s*\d+[,\s]+\w+)",              # Street number pattern
        ],
        "quantity": [
            r"(?:qty|quantity|pieces?|units?|sets?|packs?)\s*[:\-]?\s*(\d+)",
            r"(\d+)\s*(?:piece|unit|set|pack|bottle)s?",
            r"\bx\s*(\d+)\b",
            r"\b(\d+)\s*x\b",
        ],
    ]

    # ── Honorifics to strip from customer names ─────────────────
    HONORIFICS = re.compile(
        r"^(mr\.?|mrs\.?|miss\.?|ms\.?|dr\.?|prof\.?|engr\.?|"
        r"alhaji\.?|alhaja\.?|chief\.?|barr\.?|pastor\.?)\s+",
        re.IGNORECASE
    )

    def __init__(self, staff_number: str = ""):
        self._staff_number = staff_number

    def looks_like_order(self, message: RawMessage) -> bool:
        '''
        Quick pre-check: does this message likely contain an order?
        Returns True if at least one product keyword is found.
        This prevents the parser from trying to parse every message.
        '''
        text = message.raw_text.lower()
        for pattern in self.PRODUCT_MAP:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        # Also check for strong order intent without product name
        order_intent = re.search(
            r"\b(order|customer|delivery|purchase|buy)\b",
            text,
            re.IGNORECASE
        )
        return bool(order_intent)

    def parse(
        self,
        message:      RawMessage,
        staff_number: str
    ) -> Optional[Order]:
        '''
        Parse a RawMessage into an Order domain entity.

        Returns Order if parsing extracted enough to make an order.
        Returns None if the message does not contain order content.

        Does NOT raise — returns None on any failure.
        '''
        text = message.raw_text.strip()

        if not text:
            return None

        try:
            customer_name = self._extract_name(text)
            phone_raw     = self._extract_phone(text)
            address       = self._extract_address(text)
            items         = self._extract_items(text)

            # Minimum viability: need at least a name OR a phone
            if not customer_name and not phone_raw:
                log.debug(
                    f"Parser: no customer name or phone found in: "
                    f"{text[:60]!r}"
                )
                return None

            phone = PhoneNumber.from_raw(phone_raw) if phone_raw else PhoneNumber("", False)

            customer = CustomerInfo(
                name    =customer_name,
                phone   =phone,
                address =address,
            )

            # Generate a deterministic order ID from message fingerprint
            order_id = f"ORD-{message.fingerprint[:8].upper()}"

            order = Order(
                order_id       =order_id,
                staff_number   =staff_number,
                customer_name  =customer_name or str(phone),
                customer       =customer,
                items          =items,
                raw_text       =message.raw_text,
                source_message =None,   # Will be set by pipeline
                status         =OrderStatus.DETECTED,
                detected_at    =datetime.now(),
            )

            log.debug(
                f"Parser: extracted order {order_id}\n"
                f"  Customer: {customer}\n"
                f"  Items: {order.item_summary()}"
            )

            return order

        except Exception as e:
            log.warning(
                f"Parser error for message {message.fingerprint[:8]!r}: {e}",
                exc_info=True
            )
            return None

    # ── Private extraction methods ──────────────────────────────

    def _extract_name(self, text: str) -> str:
        '''
        Extract customer name from message text.
        Tries labelled patterns first, then positional heuristics.
        '''
        # Try labelled field patterns first
        for pattern in self.LABEL_PATTERNS["name"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip(" .,;:")
                name = self.HONORIFICS.sub("", name).strip()
                if 2 <= len(name) <= 50:
                    return name.title()

        # Fallback: first line that looks like a name (2+ words, no numbers)
        for line in text.split("\n"):
            line = line.strip()
            if (
                2 <= len(line.split()) <= 4
                and not re.search(r"\d", line)
                and not re.search(r"order|customer|item|phone|address", line, re.I)
            ):
                cleaned = self.HONORIFICS.sub("", line).strip()
                if cleaned:
                    return cleaned.title()

        return ""

    def _extract_phone(self, text: str) -> str:
        '''
        Extract the first recognizable Nigerian phone number.
        Tries labelled patterns first, then scans for raw phone number.
        '''
        # Labelled field first
        for pattern in self.LABEL_PATTERNS["phone"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                raw = re.sub(r"[^\d+]", "", match.group(1))
                if len(raw) >= 10:
                    return raw

        # Scan for Nigerian phone pattern anywhere in text
        phone_match = re.search(
            r"\b((?:\+?234|0)[789][01]\d{8})\b",
            text
        )
        if phone_match:
            return phone_match.group(1)

        return ""

    def _extract_address(self, text: str) -> str:
        '''
        Extract delivery address from message text.
        Tries labelled patterns first, then location keywords.
        '''
        # Labelled field
        for pattern in self.LABEL_PATTERNS["address"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                addr = match.group(0) if match.lastindex is None else match.group(1)
                addr = addr.strip(" .,;:")
                if len(addr) >= 4:
                    return addr.title()

        # Known Nigerian city/area names as fallback
        city_pattern = (
            r"\b(lagos(\s+island|\s+mainland|\s+state)?|"
            r"abuja|port\s*harcourt|ph|ibadan|kano|"
            r"enugu|benin(\s+city)?|aba|onitsha|"
            r"ikeja|lekki|victoria\s+island|vi|"
            r"surulere|yaba|ikorodu|festac|"
            r"ajah|sangotedo|ibeju)\b"
        )
        city_match = re.search(city_pattern, text, re.IGNORECASE)
        if city_match:
            return city_match.group(0).title()

        return ""

    def _extract_items(self, text: str) -> list[OrderItem]:
        '''
        Find all product mentions in the text and extract quantities.
        Returns a list of OrderItem value objects.
        '''
        items   = []
        text_l  = text.lower()

        for pattern, canonical_name in self.PRODUCT_MAP.items():
            match = re.search(pattern, text_l, re.IGNORECASE)
            if not match:
                continue

            # Look for quantity near the product mention
            # Search in a window around the match position
            start    = max(0, match.start() - 30)
            end      = min(len(text_l), match.end() + 30)
            window   = text_l[start:end]
            quantity = 1

            for qty_pattern in self.LABEL_PATTERNS["quantity"]:
                qty_match = re.search(qty_pattern, window, re.IGNORECASE)
                if qty_match:
                    try:
                        quantity = int(qty_match.group(1))
                        break
                    except (ValueError, IndexError):
                        pass

            items.append(OrderItem(
                raw_text =match.group(0),
                product  =canonical_name,
                quantity =quantity,
            ))

        # Deduplicate by canonical product name
        seen     = set()
        unique   = []
        for item in items:
            if item.product not in seen:
                seen.add(item.product)
                unique.append(item)

        return unique
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/application/validator.py
# ================================================================
# PURPOSE:
#   Stage 3. Checks a parsed Order against business rules.
#   Returns a list of error messages — empty means valid.
#
# RULES:
#   - Customer name must be present (minimum 2 characters)
#   - At least one product item must be identified
#   - Phone number must be valid Nigerian format IF present
#     (phone is not required — some orders come through WhatsApp
#     and the customer number is already known from the sender)
#   - Quantity must be positive
#
# DESIGN:
#   Returns errors, never raises.
#   Caller decides what to do with invalid orders
#   (log, notify staff, skip, etc.)
# ================================================================
# ==============================================================

"""
from app.oms.domain.entities import Order
from app.oms.domain.interfaces import IValidator
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class OrderValidator(IValidator):
    '''
    Validates a parsed Order against Nabeau Store business rules.
    Implements the IValidator domain interface.

    Usage:
        validator = OrderValidator()
        errors    = validator.validate(order)
        if not errors:
            # Order is valid — proceed to assignment
        else:
            log.warning(f"Invalid order: {errors}")
    '''

    def validate(self, order: Order) -> list[str]:
        '''
        Validate an order. Returns list of error strings.
        Empty list means the order is valid and ready to process.

        Args:
            order: The Order entity to validate.

        Returns:
            List of human-readable error messages.
            Empty list means valid.
        '''
        errors = []

        # ── Rule 1: Customer name must be present ────────────────
        if not order.customer_name or len(order.customer_name.strip()) < 2:
            errors.append(
                "Customer name is missing or too short. "
                "Cannot process order without customer identification."
            )

        # ── Rule 2: At least one product item must be identified ─
        if not order.items:
            errors.append(
                "No products identified in the order message. "
                "Parser could not find a recognizable product name."
            )

        # ── Rule 3: All item quantities must be positive ─────────
        for item in order.items:
            if item.quantity <= 0:
                errors.append(
                    f"Invalid quantity for {item.product!r}: {item.quantity}. "
                    f"Quantity must be at least 1."
                )

        # ── Rule 4: Phone number validity IF provided ─────────────
        if order.customer and order.customer.phone:
            phone = order.customer.phone
            if phone.normalized and not phone.is_valid:
                errors.append(
                    f"Phone number appears invalid: {phone.normalized!r}. "
                    f"Expected Nigerian format (13 digits starting with 234)."
                )

        # ── Rule 5: Reasonable customer name (no obviously bad data) ─
        if order.customer_name:
            name = order.customer_name.strip()
            # Reject names that are clearly noise
            noise_patterns = [
                r"^\d+$",          # All digits
                r"^[^a-zA-Z]+$",   # No letters at all
            ]
            import re
            for pattern in noise_patterns:
                if re.match(pattern, name):
                    errors.append(
                        f"Customer name looks like noise: {name!r}. "
                        "Please verify the order manually."
                    )
                    break

        if errors:
            log.debug(
                f"Validation failed for order {order.order_id!r}: "
                f"{len(errors)} error(s)"
            )
        else:
            log.debug(f"Validation passed for order {order.order_id!r}")

        return errors
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/application/assignment_engine.py
# ================================================================
# PURPOSE:
#   Stage 4. Assigns an order to a staff member.
#
# TODAY'S IMPLEMENTATION:
#   Single-staff assignment — always assigns to the one configured
#   staff member. This is correct for a single-staff OMS instance.
#
# FUTURE IMPLEMENTATIONS (when needed):
#   RoundRobinAssignmentEngine  — rotate through staff list
#   LoadBalancedAssignmentEngine — assign to least-busy staff
#   SkillBasedAssignmentEngine  — match order type to staff skill
#
# Each future engine implements IAssignmentEngine — the pipeline
# code never changes, only the engine that is injected.
# ================================================================
# ==============================================================

"""
from app.oms.domain.entities import Order, Staff
from app.oms.domain.interfaces import IAssignmentEngine
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class SingleStaffAssignmentEngine(IAssignmentEngine):
    '''
    Assignment engine for single-staff OMS instances.
    Always assigns every order to the one configured staff member.

    This is the correct implementation for Nabeau Store's current
    setup where one OMS instance monitors one staff member's orders.

    Usage:
        engine = SingleStaffAssignmentEngine(staff)
        assigned = engine.assign(order, available_staff=[staff])
    '''

    def __init__(self, staff: Staff):
        '''
        Args:
            staff: The Staff member to assign all orders to.
        '''
        self._staff = staff

    def assign(self, order: Order, available_staff: list[Staff]) -> Staff:
        '''
        Always returns the configured staff member.
        available_staff list is accepted but not used in this
        implementation (kept for interface compatibility).

        Args:
            order:           The order to assign (used for logging).
            available_staff: Ignored in single-staff mode.

        Returns:
            The configured Staff member.
        '''
        log.debug(
            f"Assigning order {order.order_id!r} to "
            f"staff +{self._staff.number}"
        )
        return self._staff
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/application/pipeline.py
# ================================================================
# PURPOSE:
#   Wires all four stages together into one cohesive processing unit.
#   This is the class that oms_runner.py calls for each message.
#
# PIPELINE FLOW:
#   RawMessage
#     → Classifier  (ORDER? ASSIGNMENT? SYSTEM? UNKNOWN?)
#         ↓ if ORDER and confident
#     → Parser      (customer, items, phone, address)
#         ↓ if parsed successfully
#     → Validator   (business rules check)
#         ↓ if valid
#     → Assignment  (which staff member)
#         ↓
#     → OrderMonitorService.save()
#         ↓
#     Events emitted at each stage
#
# DESIGN:
#   Pipeline is a single-responsibility coordinator.
#   It contains no business logic itself — only calls stage
#   classes and emits events. Logic belongs in each stage.
# ================================================================
# ==============================================================

"""
from typing import Optional

from app.oms.application.classifier import MessageClassifier, MessageClass
from app.oms.application.parser import OrderParser
from app.oms.application.validator import OrderValidator
from app.oms.application.assignment_engine import SingleStaffAssignmentEngine
from app.oms.domain.entities import Order, Staff
from app.oms.infrastructure.browser.raw_message import RawMessage
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class MessagePipeline:
    '''
    Processes a RawMessage through all four pipeline stages.
    Called for every "message.received" event from the DOM observer.

    Responsibilities:
        - Coordinate stages 1-4
        - Emit events at each stage
        - Log outcomes at every decision point
        - Never raise — all errors are caught and logged

    Usage:
        pipeline = MessagePipeline(
            classifier=MessageClassifier(),
            parser=OrderParser(),
            validator=OrderValidator(),
            assignment_engine=SingleStaffAssignmentEngine(staff),
            staff=staff,
        )

        @dispatcher.on("message.received")
        async def handle_message(message: RawMessage, **kwargs):
            await pipeline.process(message)
    '''

    def __init__(
        self,
        classifier:        MessageClassifier,
        parser:            OrderParser,
        validator:         OrderValidator,
        assignment_engine: SingleStaffAssignmentEngine,
        staff:             Staff,
    ):
        self._classifier = classifier
        self._parser     = parser
        self._validator  = validator
        self._assigner   = assignment_engine
        self._staff      = staff
        self._stats = {
            "total":       0,
            "classified":  0,
            "parsed":      0,
            "validated":   0,
            "assigned":    0,
            "skipped":     0,
            "errors":      0,
        }

    async def process(self, message: RawMessage) -> Optional[Order]:
        '''
        Process one message through the full pipeline.

        Returns the final Order if all stages succeed.
        Returns None if the message is not an order or any stage fails.
        Never raises — all exceptions are caught and logged.

        Args:
            message: The RawMessage from the DOM observer.

        Returns:
            Completed Order entity, or None.
        '''
        self._stats["total"] += 1

        try:
            return await self._run(message)
        except Exception as e:
            self._stats["errors"] += 1
            log.error(
                f"Pipeline error for message {message.fingerprint[:8]!r}: {e}",
                exc_info=True
            )
            return None

    async def _run(self, message: RawMessage) -> Optional[Order]:
        '''Internal pipeline runner. May raise — caller catches.'''

        # ── Stage 1: Classify ────────────────────────────────────
        result = self._classifier.classify(message)

        self._stats["classified"] += 1
        await dispatcher.emit(
            "message.classified",
            message   =message,
            cls       =result.message_class.value,
            confidence=result.confidence,
            reasoning =result.reasoning,
        )

        if result.message_class == MessageClass.SYSTEM:
            log.debug(f"Skipping system message: {message.preview(40)!r}")
            self._stats["skipped"] += 1
            return None

        if result.message_class == MessageClass.UNKNOWN:
            log.debug(
                f"Unknown message type (confidence={result.confidence:.2f}): "
                f"{message.preview(40)!r}"
            )
            self._stats["skipped"] += 1
            return None

        if result.message_class != MessageClass.ORDER:
            # ASSIGNMENT and STATUS will be handled in future milestones
            log.debug(
                f"Message classified as {result.message_class.value} "
                f"— not yet handled (future milestone)"
            )
            self._stats["skipped"] += 1
            return None

        if not result.is_confident:
            log.info(
                f"Low confidence ORDER classification "
                f"({result.confidence:.2f}) — skipping: "
                f"{message.preview(40)!r}"
            )
            self._stats["skipped"] += 1
            return None

        log.info(
            f"ORDER detected (confidence={result.confidence:.2f}): "
            f"{message.preview(50)!r}"
        )

        # ── Stage 2: Parse ───────────────────────────────────────
        order = self._parser.parse(message, self._staff.number)

        if order is None:
            log.info(
                f"Parser returned None — message looks like order "
                f"but could not extract fields: {message.preview(50)!r}"
            )
            await dispatcher.emit(
                "order.parse_failed",
                message=message,
                reason ="Parser returned None"
            )
            self._stats["skipped"] += 1
            return None

        # Attach the source message reference
        order.source_message = message
        self._stats["parsed"] += 1

        await dispatcher.emit(
            "order.parsed",
            order   =order,
            message =message,
        )

        log.info(
            f"Order parsed: {order.order_id!r}\n"
            f"  Customer: {order.customer}\n"
            f"  Items:    {order.item_summary()}\n"
            f"  Address:  {order.customer.address or '(none)'}"
        )

        # ── Stage 3: Validate ────────────────────────────────────
        errors = self._validator.validate(order)

        if errors:
            log.warning(
                f"Order {order.order_id!r} failed validation:\n"
                + "\n".join(f"  • {e}" for e in errors)
            )
            await dispatcher.emit(
                "order.invalid",
                order  =order,
                errors =errors,
            )
            self._stats["skipped"] += 1
            return None

        self._stats["validated"] += 1
        await dispatcher.emit("order.validated", order=order)

        # ── Stage 4: Assign ──────────────────────────────────────
        assigned_staff = self._assigner.assign(order, [self._staff])
        order.staff_number = assigned_staff.number

        self._stats["assigned"] += 1
        await dispatcher.emit(
            "order.assigned",
            order =order,
            staff =assigned_staff,
        )

        # ── Final: order.detected ────────────────────────────────
        # This is the terminal event — downstream (Day 5 storage,
        # Day 6 notifications) listens to this event.
        await dispatcher.emit(
            "order.detected",
            order  =order,
            source ="pipeline",
        )

        log.info(
            f"✅ ORDER COMPLETE: {order.order_id!r}\n"
            f"   {order.customer_name} → {order.item_summary()}\n"
            f"   Assigned to: +{assigned_staff.number}"
        )

        self._stats["total_processed"] = self._stats.get("total_processed", 0) + 1
        return order

    def stats(self) -> dict:
        '''Return pipeline processing statistics.'''
        return dict(self._stats)
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/application/__init__.py
# ================================================================
# Update to expose Day 4 components.
# ================================================================
# ==============================================================

"""
from app.oms.application.services import OrderMonitorService
from app.oms.application.classifier import (
    MessageClassifier,
    MessageClass,
    ClassificationResult,
)
from app.oms.application.parser import OrderParser
from app.oms.application.validator import OrderValidator
from app.oms.application.assignment_engine import SingleStaffAssignmentEngine
from app.oms.application.pipeline import MessagePipeline

__all__ = [
    "OrderMonitorService",
    "MessageClassifier",
    "MessageClass",
    "ClassificationResult",
    "OrderParser",
    "OrderValidator",
    "SingleStaffAssignmentEngine",
    "MessagePipeline",
]
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/oms_runner.py
# ================================================================
# FULL REPLACE — wires Day 2 + Day 3 + Day 4 together.
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.oms.config.settings import get_settings
from app.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from app.oms.infrastructure.browser.raw_message import RawMessage
from app.oms.infrastructure.browser.message_cache import MessageCache
from app.oms.infrastructure.browser.checkpoint_store import CheckpointStore
from app.oms.infrastructure.browser.recovery_manager import RecoveryManager
from app.oms.infrastructure.browser.dom_observer import DOMObserver
from app.oms.application.classifier import MessageClassifier, MessageClass
from app.oms.application.parser import OrderParser
from app.oms.application.validator import OrderValidator
from app.oms.application.assignment_engine import SingleStaffAssignmentEngine
from app.oms.application.pipeline import MessagePipeline
from app.oms.domain.entities import Order, Staff
from app.oms.shared.logger import get_logger
from app.oms.events import dispatcher

log = get_logger("oms.runner")


# ── Build pipeline ───────────────────────────────────────────────
# Assembled once at startup — injected with all dependencies

def build_pipeline(settings) -> MessagePipeline:
    staff = Staff(
        number      =settings.whatsapp.staff_number,
        group_name  =settings.whatsapp.group_name,
        display_name=getattr(settings.whatsapp, "staff_display_name", ""),
    )
    return MessagePipeline(
        classifier       =MessageClassifier(),
        parser           =OrderParser(staff_number=staff.number),
        validator        =OrderValidator(),
        assignment_engine=SingleStaffAssignmentEngine(staff),
        staff            =staff,
    )


# ── Event listeners ──────────────────────────────────────────────

@dispatcher.on("browser.connected")
async def on_browser_connected(**kwargs):
    log.info(f"Browser connected — {kwargs.get('state')}")


@dispatcher.on("recovery.completed")
async def on_recovery_completed(**kwargs):
    log.info(
        f"Recovery complete — "
        f"{kwargs.get('recovered_count', 0)} message(s) replayed"
    )


@dispatcher.on("observer.started")
async def on_observer_started(**kwargs):
    log.info(
        f"Observer active — watching: {kwargs.get('group')!r}\n"
        f"Waiting for new messages..."
    )


@dispatcher.on("message.classified")
async def on_classified(**kwargs):
    log.debug(
        f"Classified: {kwargs.get('cls')} "
        f"({kwargs.get('confidence', 0):.2f})"
    )


@dispatcher.on("order.parsed")
async def on_order_parsed(order: Order, **kwargs):
    log.info(
        f"Order parsed: {order.order_id!r} — "
        f"{order.customer_name} — {order.item_summary()}"
    )


@dispatcher.on("order.invalid")
async def on_order_invalid(order: Order, errors: list, **kwargs):
    log.warning(
        f"Order {order.order_id!r} invalid:\n"
        + "\n".join(f"  • {e}" for e in errors)
    )


@dispatcher.on("order.detected")
async def on_order_detected(order: Order, **kwargs):
    log.info(
        f"\n{'=' * 50}\n"
        f"  NEW ORDER DETECTED\n"
        f"  ID:       {order.order_id}\n"
        f"  Customer: {order.customer_name}\n"
        f"  Phone:    {order.customer.phone}\n"
        f"  Items:    {order.item_summary()}\n"
        f"  Address:  {order.customer.address or '(not provided)'}\n"
        f"  Status:   {order.status.value}\n"
        f"{'=' * 50}"
    )
    # Day 5 will save this to the database here


async def main():
    log.info("Windwhirl OMS starting — Day 4...")

    settings = get_settings()

    # Set these if not using environment variables:
    # settings.whatsapp.group_name   = "Your Group Name Here"
    # settings.whatsapp.staff_number = "2348XXXXXXXXX"

    pipeline      = build_pipeline(settings)
    bootstrap     = BrowserBootstrap(settings)
    observer_task = None

    # ── Register message pipeline on "message.received" event ────
    @dispatcher.on("message.received")
    async def handle_message(message: RawMessage, **kwargs):
        await pipeline.process(message)

    # ── Same for recovered messages ───────────────────────────────
    @dispatcher.on("message.recovered")
    async def handle_recovered(message: RawMessage, **kwargs):
        log.info(f"Processing recovered message: {message.preview()!r}")
        await pipeline.process(message)

    try:
        await bootstrap.start()

        if settings.whatsapp.group_name:
            opened = await bootstrap.session_manager.open_target_group(
                settings.whatsapp.group_name
            )
            if not opened:
                log.warning(
                    f"Could not open group: {settings.whatsapp.group_name!r}"
                )
        else:
            log.warning("whatsapp.group_name not configured — set in settings.py")
            await bootstrap.run_forever()
            return

        page = bootstrap.page

        checkpoint_store = CheckpointStore(
            group_name  =settings.whatsapp.group_name,
            data_dir    ="data",
            max_history =settings.observer.checkpoint_history_size,
        )
        cache = MessageCache(max_size=settings.observer.message_cache_size)

        # Recovery
        recovery = RecoveryManager(
            page             =page,
            checkpoint_store =checkpoint_store,
            cache            =cache,
            cfg              =settings,
        )
        await recovery.run()

        # Live observer
        observer = DOMObserver(
            page             =page,
            cache            =cache,
            checkpoint_store =checkpoint_store,
            cfg              =settings,
        )
        observer_task = asyncio.create_task(
            observer.run(),
            name="oms_dom_observer"
        )

        await bootstrap.run_forever()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down.")
    except Exception as e:
        log.error(f"OMS runner error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if observer_task and not observer_task.done():
            observer_task.cancel()
            try:
                await observer_task
            except asyncio.CancelledError:
                pass
        # Log final pipeline stats before exiting
        log.info(f"Pipeline stats: {pipeline.stats()}")
        await bootstrap.stop()
        log.info("Windwhirl OMS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
"""


# ==============================================================
# DAY 4 VERIFICATION
# ==============================================================
#
# Test 1 — Classifier on typical Nabeau order:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.classifier import MessageClassifier, MessageClass
#   from app.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection
#   from datetime import datetime
#
#   clf = MessageClassifier()
#
#   def make_msg(text):
#       return RawMessage(
#           internal_id=1, fingerprint='test',
#           sender='Coordinator', raw_text=text,
#           timestamp='10:00', direction=MessageDirection.INCOMING,
#           group_name='Nabeau Orders'
#       )
#
#   cases = [
#       ('Customer Blessing wants 2 sadoer sets, Lagos island, 08031234567', MessageClass.ORDER),
#       ('New order: Emeka, 1 collagen combo, 07012345678, Ikeja', MessageClass.ORDER),
#       ('Order delivered to Fatima', MessageClass.STATUS),
#       ('Michael please handle this customer', MessageClass.ASSIGNMENT),
#       ('Messages and calls are end-to-end encrypted', MessageClass.SYSTEM),
#       ('Good morning everyone', MessageClass.UNKNOWN),
#   ]
#
#   for text, expected in cases:
#       result = clf.classify(make_msg(text))
#       ok = '✅' if result.message_class == expected else '❌'
#       print(f'{ok} {expected.value}: {text[:50]!r}')
#       if result.message_class != expected:
#           print(f'   Got: {result.message_class.value} ({result.confidence:.2f})')
#           print(f'   Signals: {result.reasoning}')
#   "
#
# Test 2 — Parser extracts fields correctly:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.parser import OrderParser
#   from app.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection
#   from datetime import datetime
#
#   parser = OrderParser(staff_number='2348XXXXXXXXX')
#
#   def make_msg(text, fp='test123'):
#       return RawMessage(
#           internal_id=1, fingerprint=fp,
#           sender='Coordinator', raw_text=text,
#           timestamp='10:00', direction=MessageDirection.INCOMING,
#           group_name='Nabeau Orders'
#       )
#
#   messages = [
#       'Customer: Blessing Adeyemi\nPhone: 08031234567\nItem: 2 Sadoer Combo Set\nAddress: Lagos Island',
#       'blessing - 2 sadoer sets - ikeja - 07012345678',
#       'pls add order for Emeka, wants 1 collagen set, Abuja',
#   ]
#
#   for text in messages:
#       order = parser.parse(make_msg(text), '2348XXXXXXXXX')
#       if order:
#           print(f'✅ Parsed: {order.customer_name} | {order.item_summary()} | {order.customer.phone}')
#       else:
#           print(f'❌ Could not parse: {text[:50]!r}')
#   "
#
# Test 3 — Full pipeline:
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.application.classifier import MessageClassifier
#   from app.oms.application.parser import OrderParser
#   from app.oms.application.validator import OrderValidator
#   from app.oms.application.assignment_engine import SingleStaffAssignmentEngine
#   from app.oms.application.pipeline import MessagePipeline
#   from app.oms.domain.entities import Staff
#   from app.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection
#
#   staff    = Staff(number='2348037882259', group_name='Nabeau Orders')
#   pipeline = MessagePipeline(
#       classifier        =MessageClassifier(),
#       parser            =OrderParser(staff_number=staff.number),
#       validator         =OrderValidator(),
#       assignment_engine =SingleStaffAssignmentEngine(staff),
#       staff             =staff,
#   )
#
#   msg = RawMessage(
#       internal_id=1, fingerprint='abc123test',
#       sender='Coordinator',
#       raw_text='Customer: Titilayo Adeyemi. Wants 2 Sadoer Collagen Sets. Phone: 08031234567. Lagos Island.',
#       timestamp='10:30', direction=MessageDirection.INCOMING,
#       group_name='Nabeau Orders'
#   )
#
#   order = asyncio.run(pipeline.process(msg))
#   if order:
#       print('Pipeline OK:', order)
#       print('Customer:', order.customer)
#       print('Items:', order.item_summary())
#   else:
#       print('Pipeline returned None')
#   "
#
# ==============================================================
# WHAT DAY 5 BUILDS
# ==============================================================
# Day 5: Persistence Layer
#   - SQLiteOrderRepository (implements IOrderRepository from Day 1)
#   - DatabaseDuplicateDetector (implements IDuplicateDetector)
#   - Order table schema (SQLAlchemy ORM)
#   - Saves every order.detected event to the database
#   - Query methods: by_status, by_staff, by_date
#   - Export to Excel/CSV
#
# Day 5 adds one listener in oms_runner.py:
#   @dispatcher.on("order.detected")
#   async def save_order(order: Order, **kwargs):
#       await repository.save(order)
#
# No changes to Day 4 pipeline code. Clean event-driven handoff.
# ==============================================================