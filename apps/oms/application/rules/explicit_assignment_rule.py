from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from apps.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from apps.oms.domain.entities import Staff
from apps.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from apps.oms.application.assignment_state_engine import AssignmentState
    from apps.oms.application.assignment_history import AssignmentHistory
    from apps.oms.application.pending_order import PendingOrder

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