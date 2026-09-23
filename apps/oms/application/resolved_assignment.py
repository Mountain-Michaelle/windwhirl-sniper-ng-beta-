from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ResolutionStatus(str, Enum):
    '''
    Whether a resolution attempt produced an assignment or not.

    RESOLVED:     A rule successfully determined ownership.
    PENDING:      No rule could determine ownership — left pending.
    REASSIGNED:   A previously resolved order was explicitly reassigned.
    '''
    RESOLVED   = "RESOLVED"
    PENDING    = "PENDING"
    REASSIGNED = "REASSIGNED"


class AppliedRule(str, Enum):
    '''
    Which resolution rule produced this assignment.
    Every ResolvedAssignment must reference exactly one rule.
    This makes every decision traceable and auditable.
    '''
    EXPLICIT    = "EXPLICIT"      # Rule 1
    SEQUENTIAL  = "SEQUENTIAL"    # Rule 2
    FORWARD     = "FORWARD"       # Rule 3
    BATCH       = "BATCH"         # Rule 4
    REASSIGNMENT = "REASSIGNMENT" # Rule 5
    PENDING     = "PENDING"       # Rule 6 — no rule fired


@dataclass
class ResolvedAssignment:
    '''
    The output of a successful assignment resolution.

    order_id:          The order that was resolved.
    order_customer:    Customer name for quick reference in logs.
    worker_number:     Phone number of the assigned worker.
    worker_name:       Display name of the assigned worker.
    rule:              Which rule determined this assignment.
    status:            RESOLVED, PENDING, or REASSIGNED.
    window_id:         The assignment window this happened in.
    history_id:        The AssignmentHistory entry ID for audit.
    resolved_at:       When the resolution decision was made.
    previous_worker:   Previous worker (only for REASSIGNED status).
    candidate_worker:  The candidate that was on the order (if any).
    notes:             Debugging notes about why this rule fired.
    '''
    order_id:        str
    order_customer:  str
    worker_number:   str
    worker_name:     str
    rule:            AppliedRule
    status:          ResolutionStatus
    window_id:       str
    history_id:      str      = field(default_factory=lambda: str(uuid.uuid4())[:8])
    resolved_at:     datetime = field(default_factory=datetime.now)
    previous_worker: str      = ""
    candidate_worker: str     = ""
    notes:           str      = ""

    @property
    def is_resolved(self) -> bool:
        return self.status in (
            ResolutionStatus.RESOLVED,
            ResolutionStatus.REASSIGNED,
        )

    def __repr__(self):
        prev = f" (was +{self.previous_worker})" if self.previous_worker else ""
        return (
            f"ResolvedAssignment("
            f"order={self.order_id!r}, "
            f"worker=+{self.worker_number}, "
            f"rule={self.rule.value}"
            f"{prev})"
        )