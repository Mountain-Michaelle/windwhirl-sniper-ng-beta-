from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from apps.oms.domain.entities import Order


class PendingOrderStatus(str, Enum):
    '''
    The lifecycle state of an order in the pending buffer.

    WAITING:    Order arrived, no worker context yet.
    CANDIDATE:  Order arrived with a worker context recorded as hint.
    RESOLVED:   Day 6 has determined ownership. Removed from buffer.
    '''
    WAITING   = "WAITING"
    CANDIDATE = "CANDIDATE"
    RESOLVED  = "RESOLVED"


@dataclass
class PendingOrder:
    '''
    An order in the pending assignment buffer.

    Attributes:
        buffer_id:        Unique ID for this buffer entry.
        order:            The Order entity detected by the parser.
        raw_message_id:   Fingerprint of the source RawMessage.
        window_id:        Which assignment window this belongs to.
        status:           Current status within the buffer.
        created_at:       When this order entered the buffer.
        updated_at:       Last mutation timestamp.
        candidate_worker: Phone number of a candidate worker if one
                          was active when this order arrived.
                          NOT ownership — only a hint for Day 6.
        notes:            Any contextual notes for debugging.
    '''
    order:          Order
    raw_message_id: str
    window_id:      str
    buffer_id:      str                = field(
                        default_factory=lambda: str(uuid.uuid4())[:8]
                    )
    status:         PendingOrderStatus = PendingOrderStatus.WAITING
    created_at:     datetime           = field(default_factory=datetime.now)
    updated_at:     datetime           = field(default_factory=datetime.now)
    candidate_worker: str              = ""
    notes:          str                = ""

    def set_candidate(self, worker_number: str) -> None:
        '''
        Record a candidate worker hint on this order.
        Does NOT assign ownership. Day 6 makes that decision.

        Args:
            worker_number: Phone number of the candidate worker.
        '''
        self.candidate_worker = worker_number
        self.status           = PendingOrderStatus.CANDIDATE
        self.updated_at       = datetime.now()

    def mark_resolved(self) -> None:
        '''
        Mark this buffer entry as resolved.
        Called by Day 6 after ownership is determined.
        '''
        self.status     = PendingOrderStatus.RESOLVED
        self.updated_at = datetime.now()

    @property
    def has_candidate(self) -> bool:
        return bool(self.candidate_worker)

    @property
    def is_waiting(self) -> bool:
        return self.status == PendingOrderStatus.WAITING

    @property
    def is_resolved(self) -> bool:
        return self.status == PendingOrderStatus.RESOLVED

    def __repr__(self):
        cw = f", candidate=+{self.candidate_worker}" if self.candidate_worker else ""
        return (
            f"PendingOrder("
            f"id={self.buffer_id!r}, "
            f"order={self.order.order_id!r}, "
            f"status={self.status.value}"
            f"{cw})"
        )