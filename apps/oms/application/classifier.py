import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from apps.oms.domain.interfaces import IParser
from apps.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection
from apps.oms.shared.logger import get_logger

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