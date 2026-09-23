from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from apps.oms.domain.entities import Order


@dataclass(frozen=True)
class OrderTimelineEntry:
    '''
    One immutable record in the order timeline.

    order_id:       The order identifier.
    customer_name:  Customer name for quick reference.
    arrived_at:     When this order was detected.
    window_id:      Which assignment window was active.
    raw_message_id: Source RawMessage fingerprint.
    sequence_num:   Position in the overall order sequence (1-based).
    '''
    order_id:       str
    customer_name:  str
    arrived_at:     datetime
    window_id:      str
    raw_message_id: str
    sequence_num:   int

    def __repr__(self):
        return (
            f"OrderTimelineEntry("
            f"#{self.sequence_num}, "
            f"order={self.order_id!r}, "
            f"customer={self.customer_name!r}, "
            f"window={self.window_id!r})"
        )


class OrderTimeline:
    '''
    Append-only chronological record of all detected orders.
    Maintains the exact sequence in which orders arrived.
    Day 6 uses this sequence when applying assignment rules.

    Usage:
        timeline = OrderTimeline()
        timeline.record(order, window_id, message_id)
        entries = timeline.all_entries()
        entries = timeline.for_window(window_id)
    '''

    def __init__(self):
        self._entries: list[OrderTimelineEntry] = []

    def record(
        self,
        order:          Order,
        window_id:      str,
        raw_message_id: str,
    ) -> OrderTimelineEntry:
        '''
        Record an order arrival in the timeline.

        Args:
            order:          The detected Order entity.
            window_id:      ID of the currently active window.
            raw_message_id: Fingerprint of the source message.

        Returns:
            The immutable OrderTimelineEntry that was created.
        '''
        entry = OrderTimelineEntry(
            order_id       =order.order_id,
            customer_name  =order.customer_name,
            arrived_at     =datetime.now(),
            window_id      =window_id,
            raw_message_id =raw_message_id,
            sequence_num   =len(self._entries) + 1,
        )
        self._entries.append(entry)
        return entry

    def all_entries(self) -> list[OrderTimelineEntry]:
        '''All timeline entries, oldest first.'''
        return list(self._entries)

    def for_window(self, window_id: str) -> list[OrderTimelineEntry]:
        '''All entries for a given window ID, in arrival order.'''
        return [e for e in self._entries if e.window_id == window_id]

    def latest(self) -> Optional[OrderTimelineEntry]:
        '''Most recent order entry, or None.'''
        return self._entries[-1] if self._entries else None

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def __repr__(self):
        return f"OrderTimeline(total={self.total_count})"