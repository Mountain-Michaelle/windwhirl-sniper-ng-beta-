from __future__ import annotations

from typing import TYPE_CHECKING

from apps.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
from apps.oms.domain.entities import Staff
from apps.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from apps.oms.application.assignment_state_engine import AssignmentState
    from apps.oms.application.assignment_history import AssignmentHistory

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