from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MessageDirection(str, Enum):
    '''
    Whether the message was sent by someone else (INCOMING)
    or by the monitored account itself (OUTGOING).

    The OMS primarily cares about INCOMING messages — orders
    sent by customers or assignments sent by coordinators.
    OUTGOING messages are captured for completeness and for
    the recovery manager's checkpoint matching.
    '''
    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"
    UNKNOWN  = "UNKNOWN"


@dataclass
class RawMessage:
    '''
    A single WhatsApp message as captured from the DOM.
    Contains no interpretation — only raw captured data.

    Fields:
        internal_id:   OMS-assigned sequential ID for this session.
                       Not persistent — resets each run.
        fingerprint:   Deterministic hash for deduplication.
                       Based on sender + timestamp + normalized text.
                       Persistent across runs — used by checkpoint store.
        sender:        Sender's display name or phone number as shown
                       in WhatsApp Web. May be a name if saved in contacts.
        sender_number: Phone number if extractable from DOM, else "".
        raw_text:      Complete message text including whitespace.
                       Exactly as it appears in WhatsApp Web.
        timestamp:     Message timestamp as parsed from WhatsApp Web UI.
                       May be a time string ("14:32") or date ("Yesterday").
        direction:     INCOMING or OUTGOING (relative to monitored account).
        captured_at:   When the OMS captured this message (system time).
        dom_reference: CSS path or identifier of the DOM node.
                       Used for debugging selector issues.
        group_name:    Which WhatsApp group this message came from.
        is_recovered:  True if this message was found during startup
                       recovery, False if detected live.
    '''
    internal_id:   int
    fingerprint:   str
    sender:        str
    raw_text:      str
    timestamp:     str
    direction:     MessageDirection
    group_name:    str
    captured_at:   datetime           = field(default_factory=datetime.now)
    sender_number: str                = ""
    dom_reference: str                = ""
    is_recovered:  bool               = False

    @staticmethod
    def compute_fingerprint(
        sender:    str,
        timestamp: str,
        raw_text:  str
    ) -> str:
        '''
        Compute a deterministic fingerprint for a message.
        Used for deduplication — two messages with the same
        fingerprint are treated as the same message.

        INPUT NORMALIZATION:
          sender:    stripped and lowercased
          timestamp: stripped
          raw_text:  whitespace collapsed, stripped, lowercased

        OUTPUT:
          First 16 characters of SHA-256 hex digest.
          16 chars = 64-bit collision space — sufficient for a
          WhatsApp group message volume (thousands per day).

        WHY NOT USE WHATSAPP'S INTERNAL ID:
          WhatsApp Web's internal message IDs are not consistently
          accessible via the DOM across WhatsApp Web versions.
          A deterministic fingerprint based on content is more
          reliable than depending on WhatsApp's internal structure.
        '''
        # Normalize inputs before hashing
        normalized_sender = sender.strip().lower()
        normalized_time   = timestamp.strip()
        # Collapse multiple whitespace into single space
        normalized_text   = re.sub(r'\s+', ' ', raw_text).strip().lower()

        payload = f"{normalized_sender}|{normalized_time}|{normalized_text}"
        digest  = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        # Return first 16 hex chars (64 bits) — enough entropy for this use case
        return digest[:16]

    def preview(self, max_chars: int = 60) -> str:
        '''Short preview of message text for logging.'''
        text = self.raw_text.strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    def is_incoming(self) -> bool:
        '''True if this message was sent by someone else.'''
        return self.direction == MessageDirection.INCOMING

    def __repr__(self):
        return (
            f"RawMessage("
            f"id={self.internal_id}, "
            f"fp={self.fingerprint!r}, "
            f"sender={self.sender!r}, "
            f"direction={self.direction.value}, "
            f"recovered={self.is_recovered}, "
            f"text={self.preview(40)!r}"
            f")"
        )