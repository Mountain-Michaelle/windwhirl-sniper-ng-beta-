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