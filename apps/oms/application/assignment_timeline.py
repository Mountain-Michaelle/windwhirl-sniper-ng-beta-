from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class AssignmentEvent(str, Enum):
    '''All possible state-change events in the assignment engine.'''
    WINDOW_OPENED         = "WINDOW_OPENED"
    WINDOW_UPDATED        = "WINDOW_UPDATED"
    WINDOW_CLOSED         = "WINDOW_CLOSED"
    PENDING_ORDER_ADDED   = "PENDING_ORDER_ADDED"
    PENDING_ORDER_UPDATED = "PENDING_ORDER_UPDATED"
    WORKER_CONTEXT_CHANGED = "WORKER_CONTEXT_CHANGED"
    CANDIDATE_WORKER_SET  = "CANDIDATE_WORKER_SET"
    STATE_UPDATED         = "STATE_UPDATED"


@dataclass(frozen=True)
class AssignmentTimelineEntry:
    '''
    One immutable audit record.

    event:       What happened.
    occurred_at: When it happened.
    window_id:   Active window at the time (may be empty for pre-window events).
    entity_id:   The order_id, worker_number, or window_id this event concerns.
    details:     Arbitrary context dict for the event.
    sequence_num: Position in the timeline (1-based).
    '''
    event:        AssignmentEvent
    occurred_at:  datetime
    window_id:    str
    entity_id:    str
    details:      dict
    sequence_num: int

    def __repr__(self):
        return (
            f"AssignmentTimelineEntry("
            f"#{self.sequence_num}, "
            f"{self.event.value}, "
            f"entity={self.entity_id!r})"
        )


class AssignmentTimeline:
    '''
    Complete audit trail of all assignment state changes.
    Append-only. Never modified. Never reordered.

    Usage:
        timeline = AssignmentTimeline()
        timeline.record(event, window_id, entity_id, details)
        all_events = timeline.all_entries()
    '''

    def __init__(self):
        self._entries: list[AssignmentTimelineEntry] = []

    def record(
        self,
        event:     AssignmentEvent,
        window_id: str,
        entity_id: str,
        details:   dict = None,
    ) -> AssignmentTimelineEntry:
        '''
        Record a state-change event.

        Args:
            event:     The type of event that occurred.
            window_id: Active window at the time of the event.
            entity_id: The primary entity affected (order_id, worker, etc.)
            details:   Additional context for this event.

        Returns:
            The immutable AssignmentTimelineEntry created.
        '''
        entry = AssignmentTimelineEntry(
            event       =event,
            occurred_at =datetime.now(),
            window_id   =window_id,
            entity_id   =entity_id,
            details     =details or {},
            sequence_num=len(self._entries) + 1,
        )
        self._entries.append(entry)
        return entry

    def all_entries(self) -> list[AssignmentTimelineEntry]:
        return list(self._entries)

    def for_window(self, window_id: str) -> list[AssignmentTimelineEntry]:
        return [e for e in self._entries if e.window_id == window_id]

    def for_event(self, event: AssignmentEvent) -> list[AssignmentTimelineEntry]:
        return [e for e in self._entries if e.event == event]

    def latest(self) -> Optional[AssignmentTimelineEntry]:
        return self._entries[-1] if self._entries else None

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def __repr__(self):
        return f"AssignmentTimeline(total={self.total_count})"