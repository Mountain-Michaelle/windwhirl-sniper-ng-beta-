from __future__ import annotations

from typing import Optional

from apps.oms.application.resolved_assignment import (
    ResolvedAssignment, AppliedRule, ResolutionStatus
)
# from apps.oms.application
from apps.oms.application.assignment_history import AssignmentHistory
from apps.oms.application.rules.explicit_assignment_rule import ExplicitAssignmentRule
from apps.oms.application.rules.sequential_assignment_rule import SequentialAssignmentRule
from apps.oms.application.rules.forward_context_rule import ForwardContextRule
from apps.oms.application.rules.batch_fallback_rule import BatchFallbackRule
from apps.oms.application.rules.reassignment_rule import ReassignmentRule
from apps.oms.domain.entities import Staff
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

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