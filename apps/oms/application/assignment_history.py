from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from apps.oms.application.resolved_assignment import AppliedRule, ResolutionStatus
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


@dataclass
class AssignmentHistoryEntry:
    '''
    One immutable record in the assignment history ledger.
    Created for every resolution — including reassignments.
    Never modified after creation.

    history_id:      Unique identifier for this entry.
    order_id:        The order that was resolved.
    worker_number:   The worker assigned.
    worker_name:     Display name of the worker.
    rule:            Which rule produced this assignment.
    status:          RESOLVED, PENDING, or REASSIGNED.
    window_id:       Assignment window this happened in.
    resolved_at:     When the decision was made.
    previous_worker: Previous worker number (for reassignments).
    notes:           Context notes for debugging.
    '''
    order_id:        str
    worker_number:   str
    worker_name:     str
    rule:            AppliedRule
    status:          ResolutionStatus
    window_id:       str
    history_id:      str      = field(default_factory=lambda: str(uuid.uuid4())[:8])
    resolved_at:     datetime = field(default_factory=datetime.now)
    previous_worker: str      = ""
    notes:           str      = ""

    def __repr__(self):
        prev = f", was=+{self.previous_worker}" if self.previous_worker else ""
        return (
            f"AssignmentHistoryEntry("
            f"id={self.history_id!r}, "
            f"order={self.order_id!r}, "
            f"worker=+{self.worker_number}, "
            f"rule={self.rule.value}"
            f"{prev})"
        )


class AssignmentHistory:
    '''
    Append-only ledger of every assignment decision.
    Never deletes. Never modifies. Only appends.

    Reassignments create a NEW entry alongside the old one.
    The full chronological history is always preserved.

    Usage:
        history = AssignmentHistory()
        entry   = history.record(resolved_assignment)
        all     = history.for_order(order_id)
        latest  = history.latest_for_order(order_id)
        exists  = history.is_resolved(order_id)
    '''

    def __init__(self):
        self._entries: list[AssignmentHistoryEntry] = []

    def record(self, resolution: "ResolvedAssignment") -> AssignmentHistoryEntry:
        '''
        Record a resolution decision.
        Determines previous_worker automatically from existing history.

        Args:
            resolution: The ResolvedAssignment produced by a rule.

        Returns:
            The new AssignmentHistoryEntry appended to the ledger.
        '''
        previous = self.latest_for_order(resolution.order_id)

        entry = AssignmentHistoryEntry(
            order_id       =resolution.order_id,
            worker_number  =resolution.worker_number,
            worker_name    =resolution.worker_name,
            rule           =resolution.rule,
            status         =resolution.status,
            window_id      =resolution.window_id,
            history_id     =resolution.history_id,
            previous_worker=previous.worker_number if previous else "",
            notes          =resolution.notes,
        )
        self._entries.append(entry)

        log.info(f"AssignmentHistory: recorded {entry}")
        return entry

    def for_order(self, order_id: str) -> list[AssignmentHistoryEntry]:
        '''All history entries for an order, oldest first.'''
        return [e for e in self._entries if e.order_id == order_id]

    def latest_for_order(self, order_id: str) -> Optional[AssignmentHistoryEntry]:
        '''Most recent history entry for an order, or None.'''
        entries = self.for_order(order_id)
        return entries[-1] if entries else None

    def is_resolved(self, order_id: str) -> bool:
        '''
        True if this order has a resolved assignment in history.
        Note: an order may be "resolved" and then "reassigned".
        Both show as True here — use latest_for_order() for current state.
        '''
        return bool(self.for_order(order_id))

    def current_worker_for(self, order_id: str) -> str:
        '''
        The current (most recent) worker number for an order.
        Returns empty string if order has no history.
        '''
        latest = self.latest_for_order(order_id)
        return latest.worker_number if latest else ""

    def all_entries(self) -> list[AssignmentHistoryEntry]:
        return list(self._entries)

    def summary(self) -> dict:
        total       = len(self._entries)
        by_rule     = {}
        by_worker   = {}
        reassignments = 0

        for e in self._entries:
            by_rule[e.rule.value]   = by_rule.get(e.rule.value, 0) + 1
            by_worker[e.worker_number] = by_worker.get(e.worker_number, 0) + 1
            if e.status == ResolutionStatus.REASSIGNED:
                reassignments += 1

        return {
            "total":         total,
            "by_rule":       by_rule,
            "by_worker":     by_worker,
            "reassignments": reassignments,
        }

    @property
    def total_entries(self) -> int:
        return len(self._entries)