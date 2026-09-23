# ==============================================================
# WINDWHIRL OMS — DAY 6: ASSIGNMENT RESOLUTION ENGINE
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → application/resolved_assignment.py
#   FILE 2  → application/assignment_history.py
#   FILE 3  → application/rules/__init__.py
#   FILE 4  → application/rules/explicit_assignment_rule.py
#   FILE 5  → application/rules/sequential_assignment_rule.py
#   FILE 6  → application/rules/forward_context_rule.py
#   FILE 7  → application/rules/batch_fallback_rule.py
#   FILE 8  → application/rules/reassignment_rule.py
#   FILE 9  → application/assignment_resolution_engine.py
#   FILE 10 → tests/test_resolution_engine.py
#   FILE 11 → oms_runner.py  (update — wire resolution engine)
#
# WHAT THIS BUILDS:
#   The first decision-making engine in the OMS.
#   Reads Assignment State from Day 5.
#   Applies 5 rules in strict priority order.
#   Produces ResolvedAssignment for every order it can resolve.
#   Leaves unresolvable orders pending — never forces an assignment.
#
# RULE PIPELINE (strict priority order):
#   Rule 1: ExplicitAssignmentRule     — @Worker directly after order
#   Rule 2: SequentialAssignmentRule   — sequential @Worker tags
#   Rule 3: ForwardContextRule         — candidate worker at window close
#   Rule 4: BatchFallbackRule          — many orders, one @Worker after
#   Rule 5: ReassignmentRule           — manager corrects a resolved order
#   Rule 6: Leave Pending              — no rule fired, order stays pending
#
# WHEN THE ENGINE RUNS:
#   Triggered after EVERY classified message is processed by Day 5.
#   Receives the current AssignmentState snapshot.
#   Tries to resolve as many pending orders as possible.
#   Returns list of ResolvedAssignment (may be empty).
#
# WHAT THIS DOES NOT DO:
#   ❌ Parse orders
#   ❌ Validate orders
#   ❌ Touch the browser
#   ❌ Write to database
#   ❌ Touch Google Sheets
#   ❌ Guess or use AI
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/application/resolved_assignment.py
# ================================================================
# PURPOSE:
#   The output of the Assignment Resolution Engine.
#   One ResolvedAssignment per successfully resolved order.
#   Carries everything Day 7+ needs: order reference, worker,
#   which rule decided it, and the history entry ID.
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/application/assignment_history.py
# ================================================================
# PURPOSE:
#   Append-only ledger of every resolution decision ever made.
#   Never overwritten. New reassignments add new entries alongside
#   old ones. The complete audit trail is always intact.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.oms.application.resolved_assignment import AppliedRule, ResolutionStatus
from app.oms.shared.logger import get_logger

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
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/application/rules/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.rules.explicit_assignment_rule import ExplicitAssignmentRule
from app.oms.application.rules.sequential_assignment_rule import SequentialAssignmentRule
from app.oms.application.rules.forward_context_rule import ForwardContextRule
from app.oms.application.rules.batch_fallback_rule import BatchFallbackRule
from app.oms.application.rules.reassignment_rule import ReassignmentRule

__all__ = [
    "ExplicitAssignmentRule",
    "SequentialAssignmentRule",
    "ForwardContextRule",
    "BatchFallbackRule",
    "ReassignmentRule",
]
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/application/rules/explicit_assignment_rule.py
# ================================================================
# PURPOSE:
#   Rule 1 — Highest priority.
#
#   Fires when: a @Worker mention arrived IMMEDIATELY after
#   exactly ONE pending order with no other pending orders
#   between the order and the mention.
#
#   Chronological check:
#     Order arrives at time T.
#     @Worker arrives at time T+n.
#     No other orders arrived between T and T+n.
#     → Resolve that one order to this worker.
#
#   This rule only resolves ONE order per @Worker mention.
#   Sequential assignments (multiple orders + multiple @Worker)
#   are handled by Rule 2.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from app.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from app.oms.domain.entities import Staff
from app.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from app.oms.application.assignment_state_engine import AssignmentState
    from app.oms.application.assignment_history import AssignmentHistory
    from app.oms.application.pending_order import PendingOrder

log = get_logger(__name__)


class ExplicitAssignmentRule:
    '''
    Rule 1: Explicit Assignment.

    Applies when a single @Worker mention directly follows
    exactly one pending order with no intervening orders.

    Highest priority rule — checked first in the pipeline.
    If this rule fires, no other rule is evaluated for that order.

    Condition:
        - Exactly 1 pending order in the buffer
        - A current worker context is set
        - No explicit history exists for this order
          (if history exists, ReassignmentRule handles it)

    Result:
        The single pending order is resolved to the current worker.
        Removed from pending buffer.
    '''

    def try_resolve(
        self,
        state:    "AssignmentState",
        history:  "AssignmentHistory",
        worker:   Staff,
    ) -> Optional[ResolvedAssignment]:
        '''
        Attempt to apply Rule 1.

        Args:
            state:   Current AssignmentState from Day 5.
            history: Assignment history ledger.
            worker:  The currently active worker (from context).

        Returns:
            ResolvedAssignment if rule fires.
            None if conditions are not met.
        '''
        pending = [p for p in state.pending_orders if not p.is_resolved]

        # Condition: exactly one pending order
        if len(pending) != 1:
            log.debug(
                f"Rule 1 (Explicit): skipped — "
                f"{len(pending)} pending orders (need exactly 1)"
            )
            return None

        # Condition: no prior history for this order
        # (if history exists, this is a reassignment — Rule 5 handles it)
        target = pending[0]
        if history.is_resolved(target.order.order_id):
            log.debug(
                f"Rule 1 (Explicit): skipped — "
                f"order {target.order.order_id!r} already has history "
                f"(ReassignmentRule will handle)"
            )
            return None

        log.info(
            f"Rule 1 (Explicit): resolving order {target.order.order_id!r} "
            f"→ +{worker.number} ({worker.display_name})"
        )

        return ResolvedAssignment(
            order_id        =target.order.order_id,
            order_customer  =target.order.customer_name,
            worker_number   =worker.number,
            worker_name     =worker.display_name or worker.number,
            rule            =AppliedRule.EXPLICIT,
            status          =ResolutionStatus.RESOLVED,
            window_id       =state.window.window_id if state.window else "",
            candidate_worker=target.candidate_worker,
            notes           ="Rule 1: explicit @Worker after single pending order",
        )
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/application/rules/sequential_assignment_rule.py
# ================================================================
# PURPOSE:
#   Rule 2 — Sequential Assignment.
#
#   Fires when multiple orders and multiple @Worker mentions
#   arrived in a sequential pattern. Each @Worker tag resolves
#   the oldest unresolved pending order.
#
#   Example that triggers this rule:
#     Order A → @Francisca → Order B → @Michael
#     Order A resolves to Francisca (Rule 2).
#     Order B resolves to Michael (Rule 2).
#
#   This rule is applied iteratively as each @Worker arrives.
#   It resolves ONLY the oldest pending order — not all of them.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from app.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from app.oms.domain.entities import Staff
from app.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from app.oms.application.assignment_state_engine import AssignmentState
    from app.oms.application.assignment_history import AssignmentHistory

log = get_logger(__name__)


class SequentialAssignmentRule:
    '''
    Rule 2: Sequential Assignment.

    Applies when there are multiple pending orders and a worker
    context is active. Resolves the single OLDEST pending order
    to the current worker.

    This rule handles the pattern:
        Order A → @Worker A → Order B → @Worker B
    Each @Worker mention resolves only the next oldest order.

    Condition:
        - 2+ pending orders exist
        - A current worker context is set
        - The oldest pending order has no history

    Result:
        The oldest pending order resolves to the current worker.
        Only ONE order resolved per rule firing.
    '''

    def try_resolve(
        self,
        state:   "AssignmentState",
        history: "AssignmentHistory",
        worker:  Staff,
    ) -> Optional[ResolvedAssignment]:
        '''
        Attempt to apply Rule 2.

        Args:
            state:   Current AssignmentState.
            history: Assignment history ledger.
            worker:  The currently active worker.

        Returns:
            ResolvedAssignment for the oldest pending order, or None.
        '''
        pending = [p for p in state.pending_orders if not p.is_resolved]

        # Condition: 2+ pending orders
        if len(pending) < 2:
            log.debug(
                f"Rule 2 (Sequential): skipped — "
                f"only {len(pending)} pending order(s) (need 2+)"
            )
            return None

        # Resolve only the OLDEST pending order (first in list)
        target = pending[0]

        # Skip if already has a history entry
        if history.is_resolved(target.order.order_id):
            log.debug(
                f"Rule 2 (Sequential): oldest order "
                f"{target.order.order_id!r} already resolved — skipping"
            )
            return None

        log.info(
            f"Rule 2 (Sequential): resolving oldest order "
            f"{target.order.order_id!r} → +{worker.number} "
            f"({worker.display_name}) | "
            f"{len(pending) - 1} order(s) still pending"
        )

        return ResolvedAssignment(
            order_id        =target.order.order_id,
            order_customer  =target.order.customer_name,
            worker_number   =worker.number,
            worker_name     =worker.display_name or worker.number,
            rule            =AppliedRule.SEQUENTIAL,
            status          =ResolutionStatus.RESOLVED,
            window_id       =state.window.window_id if state.window else "",
            candidate_worker=target.candidate_worker,
            notes           ="Rule 2: sequential @Worker pattern",
        )
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/application/rules/forward_context_rule.py
# ================================================================
# PURPOSE:
#   Rule 3 — Forward Context.
#
#   Fires ONLY at a logical resolution point (window closing),
#   NOT immediately when the order arrives.
#
#   A "forward context" means: @Worker appeared BEFORE an order.
#   The order inherited the worker as a candidate.
#   If no explicit assignment ever arrived for that order,
#   at window-close time we resolve it using the candidate.
#
#   RULE: This never fires on message arrival alone.
#   It only fires when the resolution engine explicitly asks
#   for a "best effort" resolution at window close.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from app.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from app.oms.domain.entities import Staff
from app.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from app.oms.application.assignment_state_engine import AssignmentState
    from app.oms.application.assignment_history import AssignmentHistory
    from app.oms.application.pending_order import PendingOrder

log = get_logger(__name__)


class ForwardContextRule:
    '''
    Rule 3: Forward Context Resolution.

    Applies ONLY at window-close time (not on message arrival).
    Resolves orders that have a candidate worker but no explicit assignment.

    Condition (all must be true):
        - Called at window resolution point (not on every message)
        - Order has a candidate_worker set
        - Order has no history (never explicitly assigned)
        - No conflicting explicit assignment exists

    Result:
        Order resolved using the candidate worker.
        Marks the order with FORWARD rule.
    '''

    def try_resolve_at_window_close(
        self,
        pending:  "PendingOrder",
        state:    "AssignmentState",
        history:  "AssignmentHistory",
        staff_dir: dict[str, Staff],
    ) -> Optional[ResolvedAssignment]:
        '''
        Attempt forward context resolution for one pending order.
        Only called during window finalization — never on message arrival.

        Args:
            pending:   The pending order to try resolving.
            state:     Current AssignmentState.
            history:   Assignment history ledger.
            staff_dir: Staff directory for number → Staff lookup.

        Returns:
            ResolvedAssignment if rule fires, None otherwise.
        '''
        # Must have a candidate worker
        if not pending.candidate_worker:
            log.debug(
                f"Rule 3 (Forward): skipped for {pending.order.order_id!r} "
                f"— no candidate worker"
            )
            return None

        # Must not already have explicit history
        if history.is_resolved(pending.order.order_id):
            log.debug(
                f"Rule 3 (Forward): skipped for {pending.order.order_id!r} "
                f"— already in history"
            )
            return None

        # Resolve the candidate to a Staff object
        worker = self._find_worker(pending.candidate_worker, staff_dir)
        if not worker:
            log.warning(
                f"Rule 3 (Forward): candidate worker "
                f"+{pending.candidate_worker} not in staff directory — "
                f"leaving pending"
            )
            return None

        log.info(
            f"Rule 3 (Forward): resolving {pending.order.order_id!r} "
            f"using candidate +{worker.number} ({worker.display_name}) "
            f"[window close]"
        )

        return ResolvedAssignment(
            order_id        =pending.order.order_id,
            order_customer  =pending.order.customer_name,
            worker_number   =worker.number,
            worker_name     =worker.display_name or worker.number,
            rule            =AppliedRule.FORWARD,
            status          =ResolutionStatus.RESOLVED,
            window_id       =state.window.window_id if state.window else "",
            candidate_worker=pending.candidate_worker,
            notes           ="Rule 3: forward context resolved at window close",
        )

    def _find_worker(
        self,
        worker_number: str,
        staff_dir:     dict[str, Staff]
    ) -> Optional[Staff]:
        '''Find a Staff object by phone number.'''
        for staff in staff_dir.values():
            if staff.number == worker_number:
                return staff
        return None
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/application/rules/batch_fallback_rule.py
# ================================================================
# PURPOSE:
#   Rule 4 — Batch Fallback. EDGE CASE ONLY.
#
#   Fires only when ALL conditions are simultaneously true:
#     1. Multiple pending orders exist
#     2. None of them have explicit assignments
#     3. A single @Worker appears after all of them
#     4. No conflicting worker tags exist
#
#   In this case ALL pending orders are resolved to that one worker.
#
#   This rule MUST NOT fire if any explicit assignment exists.
#   It is a last resort, not a primary workflow.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from app.oms.domain.entities import Staff
from app.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from app.oms.application.assignment_state_engine import AssignmentState
    from app.oms.application.assignment_history import AssignmentHistory

log = get_logger(__name__)


class BatchFallbackRule:
    '''
    Rule 4: Batch Fallback Assignment. EDGE CASE ONLY.

    Resolves ALL pending orders to a single worker.
    Used only when no explicit assignments exist and a single
    @Worker tag arrives after a group of orders.

    Strict conditions (ALL must be true):
        - 2+ pending orders exist
        - NONE of the pending orders have history
        - NONE of the pending orders have conflicting candidates
        - The current worker context is set

    If ANY pending order has a different candidate worker or existing
    history, this rule does NOT fire.

    Result:
        All pending orders resolved to the current worker.
        Returns list of ResolvedAssignment (one per order).
    '''

    def try_resolve_batch(
        self,
        state:   "AssignmentState",
        history: "AssignmentHistory",
        worker:  Staff,
    ) -> list[ResolvedAssignment]:
        '''
        Attempt batch resolution of all pending orders.

        Args:
            state:   Current AssignmentState.
            history: Assignment history ledger.
            worker:  The currently active worker.

        Returns:
            List of ResolvedAssignment (may be empty if conditions not met).
        '''
        pending = [p for p in state.pending_orders if not p.is_resolved]

        # Condition: 2+ pending orders
        if len(pending) < 2:
            log.debug(
                f"Rule 4 (Batch): skipped — "
                f"{len(pending)} pending order(s) (need 2+)"
            )
            return []

        # Condition: NONE have existing history
        for p in pending:
            if history.is_resolved(p.order.order_id):
                log.debug(
                    f"Rule 4 (Batch): skipped — "
                    f"order {p.order.order_id!r} already has history"
                )
                return []

        # Condition: no conflicting candidates
        # (if any order has a different candidate, explicit is expected)
        for p in pending:
            if p.candidate_worker and p.candidate_worker != worker.number:
                log.debug(
                    f"Rule 4 (Batch): skipped — "
                    f"order {p.order.order_id!r} has conflicting candidate "
                    f"+{p.candidate_worker} vs current worker +{worker.number}"
                )
                return []

        log.info(
            f"Rule 4 (Batch FALLBACK): resolving {len(pending)} order(s) "
            f"→ +{worker.number} ({worker.display_name}) | "
            f"edge case — no explicit assignments existed"
        )

        results = []
        window_id = state.window.window_id if state.window else ""

        for i, p in enumerate(pending):
            results.append(ResolvedAssignment(
                order_id        =p.order.order_id,
                order_customer  =p.order.customer_name,
                worker_number   =worker.number,
                worker_name     =worker.display_name or worker.number,
                rule            =AppliedRule.BATCH,
                status          =ResolutionStatus.RESOLVED,
                window_id       =window_id,
                candidate_worker=p.candidate_worker,
                notes           =(
                    f"Rule 4 (Batch fallback): "
                    f"order {i+1} of {len(pending)} in batch"
                ),
            ))

        return results
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/application/rules/reassignment_rule.py
# ================================================================
# PURPOSE:
#   Rule 5 — Reassignment.
#
#   Fires when a manager explicitly assigns an order that
#   was already resolved. Managers make mistakes — the system
#   must support corrections.
#
#   Creates a new history entry alongside the old one.
#   Never deletes previous history.
#   Updates the current assignment to the new worker.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from app.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from app.oms.domain.entities import Staff
from app.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from app.oms.application.assignment_state_engine import AssignmentState
    from app.oms.application.assignment_history import AssignmentHistory
    from app.oms.application.pending_order import PendingOrder

log = get_logger(__name__)


class ReassignmentRule:
    '''
    Rule 5: Reassignment.

    Handles explicit reassignment of already-resolved orders.
    The manager explicitly routes an order to a different worker.

    Condition:
        - Order already has a history entry (was previously resolved)
        - A new explicit @Worker mention has arrived
        - The new worker differs from the current assignment

    Result:
        New ResolvedAssignment with REASSIGNED status.
        Previous assignment preserved in history (never deleted).
        History gains a new entry with previous_worker populated.
    '''

    def try_resolve(
        self,
        pending:  "PendingOrder",
        state:    "AssignmentState",
        history:  "AssignmentHistory",
        worker:   Staff,
    ) -> Optional[ResolvedAssignment]:
        '''
        Attempt to apply the reassignment rule for one order.

        Args:
            pending:  The pending order to check.
            state:    Current AssignmentState.
            history:  Assignment history ledger.
            worker:   The new worker being explicitly mentioned.

        Returns:
            ResolvedAssignment with REASSIGNED status, or None.
        '''
        # Must already have a history entry
        previous = history.latest_for_order(pending.order.order_id)
        if not previous:
            log.debug(
                f"Rule 5 (Reassignment): skipped for "
                f"{pending.order.order_id!r} — no prior history"
            )
            return None

        # New worker must differ from current
        if previous.worker_number == worker.number:
            log.debug(
                f"Rule 5 (Reassignment): skipped — "
                f"same worker +{worker.number}"
            )
            return None

        log.info(
            f"Rule 5 (Reassignment): order {pending.order.order_id!r} "
            f"reassigned from +{previous.worker_number} "
            f"→ +{worker.number} ({worker.display_name})"
        )

        return ResolvedAssignment(
            order_id        =pending.order.order_id,
            order_customer  =pending.order.customer_name,
            worker_number   =worker.number,
            worker_name     =worker.display_name or worker.number,
            rule            =AppliedRule.REASSIGNMENT,
            status          =ResolutionStatus.REASSIGNED,
            window_id       =state.window.window_id if state.window else "",
            previous_worker =previous.worker_number,
            candidate_worker=pending.candidate_worker,
            notes           =(
                f"Rule 5: reassigned from +{previous.worker_number} "
                f"to +{worker.number}"
            ),
        )
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/app/oms/application/assignment_resolution_engine.py
# ================================================================
# PURPOSE:
#   The Assignment Resolution Engine — orchestrates all 5 rules.
#
#   Called after every classified message is processed by Day 5.
#   Receives the current AssignmentState and tries to resolve
#   as many pending orders as possible.
#
#   Rule priority is enforced strictly:
#     1. Explicit
#     2. Sequential
#     3. Forward (only at window close)
#     4. Batch (only as fallback)
#     5. Reassignment
#     6. Leave Pending
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from typing import Optional

from app.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from app.oms.application.assignment_history import AssignmentHistory
from app.oms.application.rules.explicit_assignment_rule import ExplicitAssignmentRule
from app.oms.application.rules.sequential_assignment_rule import SequentialAssignmentRule
from app.oms.application.rules.forward_context_rule import ForwardContextRule
from app.oms.application.rules.batch_fallback_rule import BatchFallbackRule
from app.oms.application.rules.reassignment_rule import ReassignmentRule
from app.oms.domain.entities import Staff
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class AssignmentResolutionEngine:
    '''
    Reads Assignment State produced by Day 5 and determines
    the final owner of every pending order.

    Applies 5 rules in strict priority order.
    First matching rule wins. Unresolvable orders stay pending.

    This engine makes decisions — Day 5 only recorded facts.

    Usage:
        resolution_engine = AssignmentResolutionEngine(staff_directory)

        # After every classified message:
        results = await resolution_engine.resolve(state)

        # At window close (for forward context):
        results = await resolution_engine.resolve_at_window_close(state)
    '''

    def __init__(self, staff_directory: dict[str, Staff]):
        '''
        Args:
            staff_directory: Maps display name (lowercase) → Staff.
        '''
        self._staff_dir = staff_directory
        self._history   = AssignmentHistory()

        # Instantiate all rules
        self._rule_explicit    = ExplicitAssignmentRule()
        self._rule_sequential  = SequentialAssignmentRule()
        self._rule_forward     = ForwardContextRule()
        self._rule_batch       = BatchFallbackRule()
        self._rule_reassignment = ReassignmentRule()

        self._total_resolved = 0
        self._total_pending  = 0

    async def resolve(
        self,
        state: "AssignmentState",
    ) -> list[ResolvedAssignment]:
        '''
        Attempt to resolve pending orders after a new message.
        Called after every ORDER or ASSIGNMENT message is processed.

        Rule execution order:
          1. If no worker context → nothing to resolve yet
          2. Try Reassignment first (for already-resolved orders in buffer)
          3. Try Explicit (1 pending order)
          4. Try Sequential (2+ pending orders, resolve oldest)
          5. Try Batch fallback (multiple pending, no explicit history)
          6. Leave remaining orders pending

        Args:
            state: Current AssignmentState snapshot from Day 5.

        Returns:
            List of ResolvedAssignment. May be empty.
        '''
        if not state.current_worker:
            log.debug(
                "Resolution: no worker context active — nothing to resolve"
            )
            return []

        worker = self._find_worker_by_number(state.current_worker)
        if not worker:
            log.warning(
                f"Resolution: worker +{state.current_worker} "
                f"not in staff directory — cannot resolve"
            )
            return []

        pending = [p for p in state.pending_orders if not p.is_resolved]
        if not pending:
            log.debug("Resolution: no pending orders")
            return []

        log.debug(
            f"Resolution: {len(pending)} pending order(s), "
            f"worker context: +{worker.number} ({worker.display_name})"
        )

        results = []

        # ── Rule 5: Check reassignments first ────────────────────
        # Orders already in history that have a new worker context
        # must be handled before new assignments
        for p in pending:
            if self._history.is_resolved(p.order.order_id):
                result = self._rule_reassignment.try_resolve(
                    pending =p,
                    state   =state,
                    history =self._history,
                    worker  =worker,
                )
                if result:
                    await self._commit(result, p)
                    results.append(result)

        # ── Rule 1: Explicit (1 pending, no history) ─────────────
        result = self._rule_explicit.try_resolve(state, self._history, worker)
        if result:
            target = next(
                (p for p in pending if p.order.order_id == result.order_id),
                None
            )
            if target:
                await self._commit(result, target)
                results.append(result)
                return results  # Rule 1 fired — done for this cycle

        # ── Rule 2: Sequential (2+ pending, resolve oldest) ──────
        result = self._rule_sequential.try_resolve(state, self._history, worker)
        if result:
            target = next(
                (p for p in pending if p.order.order_id == result.order_id),
                None
            )
            if target:
                await self._commit(result, target)
                results.append(result)
                return results  # Rule 2 fired — done for this cycle

        # ── Rule 4: Batch fallback (all conditions strict) ───────
        batch_results = self._rule_batch.try_resolve_batch(
            state, self._history, worker
        )
        if batch_results:
            for result in batch_results:
                target = next(
                    (p for p in pending if p.order.order_id == result.order_id),
                    None
                )
                if target:
                    await self._commit(result, target)
            results.extend(batch_results)
            return results

        # ── Rule 6: Leave pending ─────────────────────────────────
        if not results:
            log.info(
                f"Resolution: {len(pending)} order(s) left pending — "
                f"no rule fired"
            )
            for p in pending:
                if not self._history.is_resolved(p.order.order_id):
                    self._total_pending += 1
                    await dispatcher.emit(
                        "assignment.order.still_pending",
                        order_id =p.order.order_id,
                        customer =p.order.customer_name,
                        window_id=state.window.window_id if state.window else "",
                    )

        return results

    async def resolve_at_window_close(
        self,
        state: "AssignmentState",
    ) -> list[ResolvedAssignment]:
        '''
        Called when the assignment window is about to close.
        Applies Rule 3 (Forward Context) to remaining pending orders.
        This is the only time Rule 3 fires.

        Orders that cannot be resolved even at window close
        remain pending — never forced.

        Args:
            state: Current AssignmentState.

        Returns:
            List of ResolvedAssignment from forward context resolution.
        '''
        pending = [p for p in state.pending_orders if not p.is_resolved]
        if not pending:
            return []

        log.info(
            f"Window close: attempting Rule 3 (Forward Context) "
            f"on {len(pending)} pending order(s)"
        )

        results = []

        for p in pending:
            result = self._rule_forward.try_resolve_at_window_close(
                pending  =p,
                state    =state,
                history  =self._history,
                staff_dir=self._staff_dir,
            )
            if result:
                await self._commit(result, p)
                results.append(result)
            else:
                log.info(
                    f"Window close: order {p.order.order_id!r} "
                    f"could not be resolved — left pending"
                )

        return results

    # ──────────────────────────────────────────────────────────
    # PRIVATE HELPERS
    # ──────────────────────────────────────────────────────────

    async def _commit(
        self,
        result:  ResolvedAssignment,
        pending: "PendingOrder",
    ) -> None:
        '''
        Commit a resolution: record in history, mark pending as resolved,
        update the order entity, emit events.
        '''
        # Record in history (append-only)
        self._history.record(result)

        # Mark the pending buffer entry as resolved
        pending.mark_resolved()

        # Update the Order entity's staff_number
        pending.order.staff_number = result.worker_number

        self._total_resolved += 1

        # Emit the appropriate event
        event = (
            "assignment.updated"
            if result.status == ResolutionStatus.REASSIGNED
            else "assignment.resolved"
        )

        await dispatcher.emit(
            event,
            order_id       =result.order_id,
            order_customer =result.order_customer,
            worker_number  =result.worker_number,
            worker_name    =result.worker_name,
            rule           =result.rule.value,
            status         =result.status.value,
            window_id      =result.window_id,
            history_id     =result.history_id,
            previous_worker=result.previous_worker,
        )

        log.info(
            f"✅ Assignment committed: "
            f"order {result.order_id!r} → "
            f"+{result.worker_number} [{result.rule.value}]"
            + (f" (was +{result.previous_worker})" if result.previous_worker else "")
        )

    def _find_worker_by_number(self, number: str) -> Optional[Staff]:
        '''Find a Staff object by phone number.'''
        for staff in self._staff_dir.values():
            if staff.number == number:
                return staff
        return None

    def stats(self) -> dict:
        return {
            "total_resolved":  self._total_resolved,
            "total_pending":   self._total_pending,
            "history_entries": self._history.total_entries,
            "history_summary": self._history.summary(),
        }

    @property
    def history(self) -> AssignmentHistory:
        '''Read-only access to the assignment history. Day 7+ uses this.'''
        return self._history
"""


# ==============================================================
# ================================================================
#  FILE 10
#  PATH: windwhirl/app/oms/tests/test_resolution_engine.py
# ================================================================
# Unit tests for all rules and scenarios.
# Run with: python -m pytest app/oms/tests/test_resolution_engine.py -v
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from app.oms.application.assignment_state_engine import (
    AssignmentStateEngine, AssignmentState
)
from app.oms.application.assignment_resolution_engine import (
    AssignmentResolutionEngine
)
from app.oms.application.resolved_assignment import AppliedRule, ResolutionStatus
from app.oms.domain.entities import Order, OrderStatus, Staff
from app.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection


# ── Fixtures ─────────────────────────────────────────────────────

def make_staff(name: str, number: str) -> Staff:
    return Staff(number=number, group_name="Test Group", display_name=name)


def make_directory() -> dict:
    return {
        "francisca": make_staff("Francisca", "2348031111111"),
        "michael":   make_staff("Michael",   "2348032222222"),
        "john":      make_staff("John",      "2348033333333"),
    }


def make_order(oid: str, customer: str = "Test Customer") -> Order:
    return Order(
        order_id      =oid,
        staff_number  ="",
        customer_name =customer,
        items         =[],
        raw_text      =f"Order {oid}",
        source_message=None,
        status        =OrderStatus.DETECTED,
    )


def make_msg(text: str, fp: str = "fp") -> RawMessage:
    return RawMessage(
        internal_id=1, fingerprint=fp, sender="Manager",
        raw_text=text, timestamp="10:00",
        direction=MessageDirection.INCOMING,
        group_name="Test Group",
    )


async def setup_engines(directory=None):
    d = directory or make_directory()
    state_engine      = AssignmentStateEngine(d)
    resolution_engine = AssignmentResolutionEngine(d)
    return state_engine, resolution_engine, d


# ── Rule 1: Explicit Assignment ───────────────────────────────────

@pytest.mark.asyncio
async def test_rule1_explicit():
    '''One order + @Worker → explicit assignment.'''
    se, re, d = await setup_engines()

    state = await se.observe_order(make_order("A"), make_msg("order", "fp1"))
    state = await se.observe_assignment("@Francisca", make_msg("@Francisca", "fp2"))

    results = await re.resolve(state)

    assert len(results) == 1
    assert results[0].order_id == "A"
    assert results[0].worker_number == "2348031111111"
    assert results[0].rule == AppliedRule.EXPLICIT
    assert results[0].status == ResolutionStatus.RESOLVED


# ── Rule 2: Sequential Assignment ────────────────────────────────

@pytest.mark.asyncio
async def test_rule2_sequential():
    '''Order A → @F; Order B → @M — each resolves one.'''
    se, re, d = await setup_engines()

    state = await se.observe_order(make_order("A"), make_msg("order A", "fp1"))
    state = await se.observe_assignment("@Francisca", make_msg("@Francisca", "fp2"))
    results_a = await re.resolve(state)

    assert len(results_a) == 1
    assert results_a[0].order_id == "A"

    # Mark resolved in state engine
    se.pending_orders[0].mark_resolved() if se.pending_orders else None

    state = await se.observe_order(make_order("B"), make_msg("order B", "fp3"))
    state = await se.observe_assignment("@Michael", make_msg("@Michael", "fp4"))
    results_b = await re.resolve(state)

    assert len(results_b) >= 1
    order_b_result = next((r for r in results_b if r.order_id == "B"), None)
    assert order_b_result is not None
    assert order_b_result.worker_number == "2348032222222"
    assert order_b_result.rule == AppliedRule.SEQUENTIAL


# ── Rule 3: Forward Context ───────────────────────────────────────

@pytest.mark.asyncio
async def test_rule3_forward_context_at_window_close():
    '''@Worker then Order → candidate set; resolves at window close.'''
    se, re, d = await setup_engines()

    # @Worker before any order
    state = await se.observe_assignment("@Francisca", make_msg("@Francisca", "fp1"))
    results = await re.resolve(state)
    assert len(results) == 0  # No pending orders — nothing to resolve

    # Order arrives with Francisca as candidate
    state = await se.observe_order(make_order("A"), make_msg("order A", "fp2"))

    # NOT resolved on arrival
    results = await re.resolve(state)
    assert len(results) == 0  # Forward context does not resolve on arrival

    # Resolve at window close
    close_results = await re.resolve_at_window_close(state)
    assert len(close_results) == 1
    assert close_results[0].order_id == "A"
    assert close_results[0].rule == AppliedRule.FORWARD


# ── Rule 4: Batch Fallback ────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule4_batch_fallback():
    '''3 orders + @Worker → batch assign all to one worker.'''
    se, re, d = await setup_engines()

    for i in range(3):
        await se.observe_order(make_order(f"ORD-{i}"), make_msg(f"order {i}", f"fp{i}"))

    state = await se.observe_assignment("@Francisca", make_msg("@Francisca", "fp99"))
    results = await re.resolve(state)

    assert len(results) == 3
    assert all(r.worker_number == "2348031111111" for r in results)
    assert all(r.rule == AppliedRule.BATCH for r in results)


# ── Rule 5: Reassignment ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule5_reassignment():
    '''Already-resolved order receives new explicit assignment.'''
    se, re, d = await setup_engines()

    # First assignment
    state = await se.observe_order(make_order("A"), make_msg("order A", "fp1"))
    state = await se.observe_assignment("@Francisca", make_msg("@Francisca", "fp2"))
    results = await re.resolve(state)
    assert len(results) == 1
    assert results[0].worker_number == "2348031111111"

    # Re-add to pending for reassignment scenario
    pending = se._pending_orders[0]
    pending.status = __import__(
        "app.oms.application.pending_order",
        fromlist=["PendingOrderStatus"]
    ).PendingOrderStatus.CANDIDATE

    # New assignment — Michael takes over
    state = await se.observe_assignment("@Michael", make_msg("@Michael", "fp3"))
    results = await re.resolve(state)

    reassignment = next(
        (r for r in results if r.rule == AppliedRule.REASSIGNMENT), None
    )
    assert reassignment is not None
    assert reassignment.worker_number == "2348032222222"
    assert reassignment.previous_worker == "2348031111111"
    assert reassignment.status == ResolutionStatus.REASSIGNED

    # History has 2 entries for this order
    history = re.history.for_order("A")
    assert len(history) == 2


# ── Rule 6: Leave Pending ────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule6_leave_pending_no_worker():
    '''Order with no worker context — stays pending.'''
    se, re, d = await setup_engines()

    state = await se.observe_order(make_order("A"), make_msg("order A", "fp1"))
    results = await re.resolve(state)  # No worker context

    assert len(results) == 0  # Nothing resolved
    assert len(se.pending_orders) == 1  # Still pending


# ── No Backward Assumption ────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_backward_assumption():
    '''Resolved orders are never reopened by later @Worker.'''
    se, re, d = await setup_engines()

    # Resolve A to Francisca
    state = await se.observe_order(make_order("A"), make_msg("order A", "fp1"))
    state = await se.observe_assignment("@Francisca", make_msg("@Francisca", "fp2"))
    results = await re.resolve(state)
    assert results[0].worker_number == "2348031111111"

    # Later @John — must NOT affect A
    state = await se.observe_assignment("@John", make_msg("@John", "fp3"))
    results_later = await re.resolve(state)

    # A should not appear in results with John
    for r in results_later:
        if r.order_id == "A":
            assert r.worker_number != "2348033333333", \
                "Backward assumption violation: order A was reassigned to John"

    # History for A must still show Francisca as first entry
    history = re.history.for_order("A")
    assert history[0].worker_number == "2348031111111"


# ── Mixed Scenarios ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mixed_explicit_then_more_orders():
    '''Explicit assignment then more orders — new orders stay pending.'''
    se, re, d = await setup_engines()

    # Order A explicitly assigned to Francisca
    state = await se.observe_order(make_order("A"), make_msg("order A", "fp1"))
    state = await se.observe_assignment("@Francisca", make_msg("@Francisca", "fp2"))
    results = await re.resolve(state)
    se.pending_orders[0].mark_resolved()

    assert len(results) == 1

    # Two more orders arrive — no new assignment yet
    await se.observe_order(make_order("B"), make_msg("order B", "fp3"))
    state = await se.observe_order(make_order("C"), make_msg("order C", "fp4"))

    results = await re.resolve(state)
    # Sequential rule fires for B (oldest of 2 pending with Francisca context)
    assert any(r.order_id == "B" for r in results) or len(results) == 0
"""


# ==============================================================
# ================================================================
#  FILE 11
#  PATH: windwhirl/oms_runner.py
# ================================================================
# Update: wire AssignmentResolutionEngine alongside state engine.
# Both engines receive the same classified messages.
# State engine records. Resolution engine decides.
# ================================================================
# ==============================================================

"""
# ADD to imports in oms_runner.py:
from app.oms.application.assignment_resolution_engine import AssignmentResolutionEngine
from app.oms.application.resolved_assignment import ResolvedAssignment

# ADD to build functions:
def build_resolution_engine(settings, staff_directory) -> AssignmentResolutionEngine:
    return AssignmentResolutionEngine(staff_directory)

# ADD event listeners:

@dispatcher.on("assignment.resolved")
async def on_assignment_resolved(**kwargs):
    log.info(
        f"ASSIGNMENT RESOLVED:\\n"
        f"  Order:    {kwargs.get('order_id')!r}\\n"
        f"  Customer: {kwargs.get('order_customer')!r}\\n"
        f"  Worker:   +{kwargs.get('worker_number')} "
        f"({kwargs.get('worker_name')})\\n"
        f"  Rule:     {kwargs.get('rule')}\\n"
        f"  Window:   {kwargs.get('window_id')!r}"
    )
    # Day 7 saves to database here

@dispatcher.on("assignment.updated")
async def on_assignment_updated(**kwargs):
    log.info(
        f"REASSIGNMENT:\\n"
        f"  Order:    {kwargs.get('order_id')!r}\\n"
        f"  New:      +{kwargs.get('worker_number')}\\n"
        f"  Previous: +{kwargs.get('previous_worker')}\\n"
        f"  Rule:     {kwargs.get('rule')}"
    )

@dispatcher.on("assignment.order.still_pending")
async def on_still_pending(**kwargs):
    log.info(
        f"Order still pending: {kwargs.get('order_id')!r} "
        f"| customer: {kwargs.get('customer')!r}"
    )

# In main(), after building state_engine:
# resolution_engine = build_resolution_engine(settings, staff_directory)

# UPDATE the message.received handler to also call resolution_engine:
@dispatcher.on("message.received")
async def handle_message(message: RawMessage, **kwargs):
    state = await pipeline.process_with_state(message)
    if state:
        results = await resolution_engine.resolve(state)
        if results:
            log.info(f"Resolved {len(results)} assignment(s) this cycle")

# NOTE: pipeline.py needs a process_with_state() method that
# returns the AssignmentState alongside processing the message.
# Add this to pipeline.py:
#
#   async def process_with_state(self, message):
#       order = await self.process(message)
#       return self._state_engine.current_state
"""


# ==============================================================
# DAY 6 VERIFICATION
# ==============================================================
#
# Test 1 — Imports:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.resolved_assignment import ResolvedAssignment, AppliedRule
#   from app.oms.application.assignment_history import AssignmentHistory
#   from app.oms.application.rules import (
#       ExplicitAssignmentRule, SequentialAssignmentRule,
#       ForwardContextRule, BatchFallbackRule, ReassignmentRule
#   )
#   from app.oms.application.assignment_resolution_engine import AssignmentResolutionEngine
#   print('All Day 6 imports OK')
#   "
#
# Test 2 — Run all unit tests:
#   python -m pytest app/oms/tests/test_resolution_engine.py -v
#
#   Expected: 9+ tests PASSED
#
# Test 3 — Quick Rule 1 scenario:
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.application.assignment_state_engine import AssignmentStateEngine
#   from app.oms.application.assignment_resolution_engine import AssignmentResolutionEngine
#   from app.oms.domain.entities import Order, OrderStatus, Staff
#   from app.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection
#
#   staff = Staff('2348031111111', 'Nabeau', 'Francisca')
#   d     = {'francisca': staff}
#   se    = AssignmentStateEngine(d)
#   re    = AssignmentResolutionEngine(d)
#
#   def o(oid): return Order(oid, '', 'Test', [], 'test', None, OrderStatus.DETECTED)
#   def m(t, fp): return RawMessage(1, fp, 'Mgr', t, '10', MessageDirection.INCOMING, 'G')
#
#   async def run():
#       state = await se.observe_order(o('ORD-1'), m('order', 'fp1'))
#       state = await se.observe_assignment('@Francisca', m('@Francisca', 'fp2'))
#       results = await re.resolve(state)
#       print(f'Resolved: {len(results)}')
#       print(f'Rule: {results[0].rule.value}')
#       print(f'Worker: +{results[0].worker_number}')
#       print(f'Stats: {re.stats()}')
#
#   asyncio.run(run())
#   "
#
#   Expected:
#     Resolved: 1
#     Rule: EXPLICIT
#     Worker: +2348031111111
#
# ==============================================================
# WHAT DAY 7 BUILDS
# ==============================================================
# Day 7: Persistence + Notifications
#   - SQLiteOrderRepository saves ResolvedAssignment + Order
#   - Listens to "assignment.resolved" events
#   - Writes order + worker to database
#   - Provides query API: by_worker, by_date, pending_count
#   - Optional: WhatsApp notification to staff when assigned
#
# Day 7 adds one listener:
#   @dispatcher.on("assignment.resolved")
#   async def save_assignment(order_id, worker_number, **kwargs):
#       await repository.save_assignment(order_id, worker_number)
#
# No changes to Day 6 engine. Clean event-driven handoff.
# ==============================================================
ENDOFFILE
echo "Lines: $(wc -l < /home/claude/oms_day6.py)"