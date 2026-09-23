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