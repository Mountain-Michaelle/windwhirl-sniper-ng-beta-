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