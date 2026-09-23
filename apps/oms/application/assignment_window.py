from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from apps.oms.application.pending_order import PendingOrder


class WindowStatus(str, Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class AssignmentWindow:
    '''
    One continuous assignment session.

    Tracks all activity between when the window opened and closed.
    Contains references to all pending orders in this window.
    Never contains ownership decisions — those belong to Day 6.

    Attributes:
        window_id:       Unique identifier.
        opened_at:       When this window was opened.
        updated_at:      Most recent state change inside this window.
        closed_at:       When window was closed (None if still open).
        status:          OPEN or CLOSED.
        pending_orders:  All PendingOrder entries in this window,
                         in chronological arrival order.
        current_worker:  Phone number of most recent worker context.
                         NOT ownership — just current context.
        order_count:     How many orders entered this window.
        version:         Internal mutation counter for this window.
    '''
    window_id:     str            = field(
                       default_factory=lambda: str(uuid.uuid4())[:8]
                   )
    opened_at:     datetime       = field(default_factory=datetime.now)
    updated_at:    datetime       = field(default_factory=datetime.now)
    closed_at:     Optional[datetime] = None
    status:        WindowStatus   = WindowStatus.OPEN
    pending_orders: list          = field(default_factory=list)
    current_worker: str           = ""
    order_count:   int            = 0
    version:       int            = 0

    @property
    def is_open(self) -> bool:
        return self.status == WindowStatus.OPEN

    @property
    def pending_count(self) -> int:
        '''Number of orders still waiting in this window.'''
        return sum(
            1 for po in self.pending_orders
            if not po.is_resolved
        )

    def add_pending_order(self, pending: PendingOrder) -> None:
        '''Add a pending order to this window. Increments counters.'''
        self.pending_orders.append(pending)
        self.order_count += 1
        self._touch()

    def update_worker_context(self, worker_number: str) -> None:
        '''Record the new current worker context. NOT ownership.'''
        self.current_worker = worker_number
        self._touch()

    def close(self) -> None:
        '''Close this window. Called by Day 6 or business rules.'''
        self.status    = WindowStatus.CLOSED
        self.closed_at = datetime.now()
        self._touch()

    def _touch(self) -> None:
        '''Update the updated_at timestamp and increment version.'''
        self.updated_at = datetime.now()
        self.version   += 1

    def snapshot(self) -> dict:
        '''Serializable snapshot of this window for events and logging.'''
        return {
            "window_id":     self.window_id,
            "status":        self.status.value,
            "opened_at":     self.opened_at.isoformat(),
            "order_count":   self.order_count,
            "pending_count": self.pending_count,
            "current_worker": self.current_worker,
            "version":       self.version,
        }

    def __repr__(self):
        return (
            f"AssignmentWindow("
            f"id={self.window_id!r}, "
            f"status={self.status.value}, "
            f"orders={self.order_count}, "
            f"pending={self.pending_count}, "
            f"worker=+{self.current_worker or 'none'})"
        )