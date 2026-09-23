from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from apps.oms.application.pending_order import PendingOrder, PendingOrderStatus
from apps.oms.application.worker_context import (
    CurrentWorkerContext,
    WorkerContextEntry,
)
from apps.oms.application.assignment_window import AssignmentWindow, WindowStatus
from apps.oms.application.order_timeline import OrderTimeline
from apps.oms.application.worker_timeline import WorkerTimeline
from apps.oms.application.assignment_timeline import (
    AssignmentTimeline,
    AssignmentEvent,
)
from apps.oms.domain.entities import Order, Staff
from apps.oms.infrastructure.browser.raw_message import RawMessage
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


@dataclass
class AssignmentState:
    '''
    Complete snapshot of the assignment engine state.
    Returned by observe_order() and observe_assignment() calls.
    Day 6 reads this to make ownership decisions.
    '''
    state_version:    int
    window:           Optional[AssignmentWindow]
    current_worker:   str                        # phone number or ""
    pending_count:    int
    pending_orders:   list[PendingOrder]
    order_timeline:   OrderTimeline
    worker_timeline:  WorkerTimeline
    assignment_timeline: AssignmentTimeline
    snapshot_at:      datetime = field(default_factory=datetime.now)


class AssignmentStateEngine:
    '''
    The memory of the OMS. Observes events and records state.

    This engine maintains a complete, chronological, accurate
    picture of what is happening in the WhatsApp assignment workflow.
    It never decides who owns an order — it only records what it sees.

    Day 6 (Assignment Resolution Engine) will consume this state
    and apply business rules to determine ownership.

    Usage:
        staff_directory = {"michael": Staff(...), "francisca": Staff(...)}
        engine = AssignmentStateEngine(staff_directory)

        # Called by pipeline for ORDER messages:
        state = await engine.observe_order(order, raw_message)

        # Called by pipeline for ASSIGNMENT messages:
        state = await engine.observe_assignment("@Francisca", raw_message)

        # Inspect current state at any time:
        state = engine.current_state
    '''

    # Pattern to detect @mentions in assignment messages
    MENTION_PATTERN = re.compile(
        r"@(\w[\w\s]*\w|\w+)",
        re.UNICODE
    )

    def __init__(self, staff_directory: dict[str, Staff]):
        '''
        Args:
            staff_directory: Maps display name (lowercase) → Staff.
                             Used to resolve @mentions to phone numbers.
                             Example: {"michael": Staff(...)}
        '''
        self._staff_dir      = staff_directory
        self._state_version  = 0

        # Core state objects
        self._window:          Optional[AssignmentWindow]  = None
        self._worker_context:  CurrentWorkerContext        = CurrentWorkerContext()
        self._pending_orders:  list[PendingOrder]          = []

        # Three timelines — append-only chronological records
        self._order_timeline:      OrderTimeline      = OrderTimeline()
        self._worker_timeline:     WorkerTimeline     = WorkerTimeline()
        self._assignment_timeline: AssignmentTimeline = AssignmentTimeline()

    # ──────────────────────────────────────────────────────────
    # PUBLIC OBSERVATION API
    # ──────────────────────────────────────────────────────────

    async def observe_order(
        self,
        order:   Order,
        message: RawMessage,
    ) -> AssignmentState:
        '''
        Observe an ORDER message. Update state accordingly.

        Actions taken (in order):
          1. Open window if none is active
          2. Create PendingOrder entry
          3. If worker context is active → set candidate on the order
          4. Add to window's pending list
          5. Record to OrderTimeline
          6. Record to AssignmentTimeline
          7. Increment state_version
          8. Emit events
          9. Return state snapshot

        RULE: Candidate worker is NOT ownership.
        It is only recorded as a hint for Day 6 to consider.

        Args:
            order:   The Order entity detected by Day 4 parser.
            message: The source RawMessage.

        Returns:
            Current AssignmentState snapshot after this observation.
        '''
        log.info(
            f"StateEngine: observing ORDER {order.order_id!r} "
            f"from {order.customer_name!r}"
        )

        # ── Step 1: Ensure window is open ────────────────────────
        window_just_opened = False
        if not self._window or not self._window.is_open:
            await self._open_window()
            window_just_opened = True

        window_id = self._window.window_id

        # ── Step 2: Create PendingOrder entry ────────────────────
        pending = PendingOrder(
            order          =order,
            raw_message_id =message.fingerprint,
            window_id      =window_id,
        )

        # ── Step 3: Apply candidate worker if context is active ──
        # Worker Context is NOT ownership. Recording it here is only
        # preserving a contextual hint for Day 6 to reason about.
        if self._worker_context.is_active:
            candidate_number = self._worker_context.worker_number
            pending.set_candidate(candidate_number)

            log.info(
                f"StateEngine: candidate worker +{candidate_number} "
                f"recorded on order {order.order_id!r} (NOT ownership)"
            )

            self._assignment_timeline.record(
                event    =AssignmentEvent.CANDIDATE_WORKER_SET,
                window_id=window_id,
                entity_id=order.order_id,
                details  ={
                    "candidate_worker": candidate_number,
                    "note": "hint only — not ownership"
                }
            )
            await dispatcher.emit(
                "assignment.candidate_worker.set",
                order_id        =order.order_id,
                candidate_worker=candidate_number,
                window_id       =window_id,
                note            ="hint only — Day 6 decides ownership",
            )

        # ── Step 4: Add to tracking structures ───────────────────
        self._pending_orders.append(pending)
        self._window.add_pending_order(pending)

        # ── Step 5: Record to OrderTimeline ──────────────────────
        ot_entry = self._order_timeline.record(
            order          =order,
            window_id      =window_id,
            raw_message_id =message.fingerprint,
        )

        # ── Step 6: Record to AssignmentTimeline ─────────────────
        self._assignment_timeline.record(
            event    =AssignmentEvent.PENDING_ORDER_ADDED,
            window_id=window_id,
            entity_id=order.order_id,
            details  ={
                "customer":       order.customer_name,
                "has_candidate":  pending.has_candidate,
                "sequence_num":   ot_entry.sequence_num,
            }
        )

        # ── Step 7: Increment state version ──────────────────────
        self._state_version += 1

        # ── Step 8: Emit events ───────────────────────────────────
        await dispatcher.emit(
            "assignment.pending_order.added",
            order_id       =order.order_id,
            customer_name  =order.customer_name,
            window_id      =window_id,
            candidate_worker=pending.candidate_worker,
            pending_count  =len(self._pending_orders),
            state_version  =self._state_version,
        )

        await self._emit_state_updated("observe_order", order.order_id)

        log.info(
            f"StateEngine: pending buffer now has "
            f"{len(self._pending_orders)} order(s)"
        )

        return self.current_state

    async def observe_assignment(
        self,
        raw_text: str,
        message:  RawMessage,
    ) -> AssignmentState:
        '''
        Observe an ASSIGNMENT message. Update worker context.

        Actions taken (in order):
          1. Extract @mentions from message text
          2. Resolve mentions to Staff phone numbers
          3. Open window if none is active (Rule: assignment before order)
          4. Update CurrentWorkerContext
          5. Update candidate on existing pending orders that have no candidate
          6. Record to WorkerTimeline
          7. Record to AssignmentTimeline
          8. Update window's current_worker field
          9. Increment state_version
          10. Emit events
          11. Return state snapshot

        RULE: Updating worker context is NOT assigning orders.
        Existing pending orders are NOT resolved by this action.
        Only candidate hints are updated where missing.
        Day 6 decides what to do with this context.

        Args:
            raw_text: The raw text of the ASSIGNMENT message.
            message:  The source RawMessage.

        Returns:
            Current AssignmentState snapshot.
        '''
        mentions = self._extract_mentions(raw_text)
        if not mentions:
            log.debug(
                f"StateEngine: ASSIGNMENT message but no @mentions: "
                f"{raw_text[:60]!r}"
            )
            return self.current_state

        for mention in mentions:
            worker = self._resolve_worker(mention)
            if not worker:
                log.warning(
                    f"StateEngine: @{mention!r} could not be resolved "
                    f"to a known staff member — skipping"
                )
                continue

            await self._apply_worker_context(
                worker  =worker,
                mention =mention,
                message =message,
            )

        return self.current_state

    # ──────────────────────────────────────────────────────────
    # WINDOW MANAGEMENT
    # ──────────────────────────────────────────────────────────

    async def open_window(self) -> AssignmentWindow:
        '''
        Public method to open a new assignment window.
        Can be called by external code (Day 6, tests).
        '''
        return await self._open_window()

    async def close_current_window(self) -> None:
        '''
        Public method to close the current window.
        Called by Day 6 when all pending orders are resolved,
        or by business rules requesting closure.
        '''
        if not self._window or not self._window.is_open:
            log.debug("StateEngine: no open window to close")
            return

        self._window.close()
        self._worker_context.clear()

        self._assignment_timeline.record(
            event    =AssignmentEvent.WINDOW_CLOSED,
            window_id=self._window.window_id,
            entity_id=self._window.window_id,
            details  =self._window.snapshot(),
        )

        self._state_version += 1

        log.info(
            f"StateEngine: window {self._window.window_id!r} closed — "
            f"{self._window.order_count} orders, "
            f"{self._window.pending_count} still pending"
        )

        await dispatcher.emit(
            "assignment.window.closed",
            window_id    =self._window.window_id,
            order_count  =self._window.order_count,
            pending_count=self._window.pending_count,
            state_version=self._state_version,
        )

    # ──────────────────────────────────────────────────────────
    # STATE ACCESS
    # ──────────────────────────────────────────────────────────

    @property
    def current_state(self) -> AssignmentState:
        '''
        Current immutable snapshot of the engine state.
        Day 6 reads this to make ownership decisions.
        '''
        return AssignmentState(
            state_version      =self._state_version,
            window             =self._window,
            current_worker     =self._worker_context.worker_number,
            pending_count      =len([p for p in self._pending_orders if not p.is_resolved]),
            pending_orders     =list(self._pending_orders),
            order_timeline     =self._order_timeline,
            worker_timeline    =self._worker_timeline,
            assignment_timeline=self._assignment_timeline,
        )

    @property
    def pending_orders(self) -> list[PendingOrder]:
        '''All pending orders not yet resolved.'''
        return [p for p in self._pending_orders if not p.is_resolved]

    @property
    def state_version(self) -> int:
        return self._state_version

    def stats(self) -> dict:
        return {
            "state_version":    self._state_version,
            "window_open":      bool(self._window and self._window.is_open),
            "window_id":        self._window.window_id if self._window else None,
            "current_worker":   self._worker_context.worker_number or None,
            "pending_count":    len(self.pending_orders),
            "order_timeline":   self._order_timeline.total_count,
            "worker_timeline":  self._worker_timeline.total_count,
            "assignment_events":self._assignment_timeline.total_count,
        }

    # ──────────────────────────────────────────────────────────
    # PRIVATE HELPERS
    # ──────────────────────────────────────────────────────────

    async def _open_window(self) -> AssignmentWindow:
        '''Open a new assignment window. Close current if open.'''
        if self._window and self._window.is_open:
            log.debug(
                f"StateEngine: closing existing window "
                f"{self._window.window_id!r} before opening new one"
            )
            await self.close_current_window()

        self._window = AssignmentWindow()
        self._state_version += 1

        self._assignment_timeline.record(
            event    =AssignmentEvent.WINDOW_OPENED,
            window_id=self._window.window_id,
            entity_id=self._window.window_id,
            details  ={"opened_at": self._window.opened_at.isoformat()},
        )

        log.info(
            f"StateEngine: window opened {self._window.window_id!r}"
        )

        await dispatcher.emit(
            "assignment.window.opened",
            window_id    =self._window.window_id,
            opened_at    =self._window.opened_at.isoformat(),
            state_version=self._state_version,
        )

        return self._window

    async def _apply_worker_context(
        self,
        worker:  Staff,
        mention: str,
        message: RawMessage,
    ) -> None:
        '''
        Update worker context and apply candidate hints to pending orders.
        Does NOT assign ownership to any order.
        '''
        # Ensure window is open (assignment may arrive before any order)
        if not self._window or not self._window.is_open:
            await self._open_window()

        window_id = self._window.window_id

        # Record to WorkerTimeline
        wt_entry = self._worker_timeline.record(
            worker_number  =worker.number,
            display_name   =mention,
            window_id      =window_id,
            raw_message_id =message.fingerprint,
        )

        # Update CurrentWorkerContext
        context_entry = WorkerContextEntry(
            worker_number  =worker.number,
            display_name   =mention,
            raw_message_id =message.fingerprint,
            window_id      =window_id,
        )
        self._worker_context.update(context_entry)
        self._window.update_worker_context(worker.number)

        # Record to AssignmentTimeline
        self._assignment_timeline.record(
            event    =AssignmentEvent.WORKER_CONTEXT_CHANGED,
            window_id=window_id,
            entity_id=worker.number,
            details  ={
                "display_name":  mention,
                "sequence_num":  wt_entry.sequence_num,
                "note":          "context only — not ownership",
            }
        )

        self._state_version += 1

        log.info(
            f"StateEngine: worker context → +{worker.number} "
            f"({mention}) [NOT ownership]"
        )

        # Update candidate hints on pending orders that have no candidate yet
        # RULE: Only update orders with no candidate. Never overwrite existing.
        updated_count = 0
        for pending in self.pending_orders:
            if not pending.has_candidate:
                pending.set_candidate(worker.number)
                updated_count += 1

                self._assignment_timeline.record(
                    event    =AssignmentEvent.CANDIDATE_WORKER_SET,
                    window_id=window_id,
                    entity_id=pending.order.order_id,
                    details  ={
                        "candidate": worker.number,
                        "note":      "set after context change — not ownership"
                    }
                )

        if updated_count:
            log.info(
                f"StateEngine: candidate hint set on "
                f"{updated_count} pending order(s) — NOT ownership"
            )

        # Emit events
        await dispatcher.emit(
            "assignment.worker_context.changed",
            worker_number    =worker.number,
            display_name     =mention,
            window_id        =window_id,
            pending_updated  =updated_count,
            state_version    =self._state_version,
            note             ="context only — ownership decided by Day 6",
        )

        await self._emit_state_updated("worker_context_changed", worker.number)

    async def _emit_state_updated(
        self,
        trigger:   str,
        entity_id: str,
    ) -> None:
        '''Emit the generic state-updated event with current snapshot.'''
        await dispatcher.emit(
            "assignment.state.updated",
            trigger          =trigger,
            entity_id        =entity_id,
            state_version    =self._state_version,
            window_id        =self._window.window_id if self._window else None,
            current_worker   =self._worker_context.worker_number,
            pending_count    =len(self.pending_orders),
        )

    def _extract_mentions(self, text: str) -> list[str]:
        '''Extract @mention names from message text.'''
        matches = self.MENTION_PATTERN.findall(text)
        return [m.strip() for m in matches if m.strip()]

    def _resolve_worker(self, mention_name: str) -> Optional[Staff]:
        '''
        Resolve a @mention to a Staff object using the directory.
        Tries exact match then partial match.
        '''
        name_lower = mention_name.lower().strip()

        if name_lower in self._staff_dir:
            return self._staff_dir[name_lower]

        for key, staff in self._staff_dir.items():
            if name_lower in key or key in name_lower:
                return staff

        # Phone number mention
        digits = re.sub(r"[^\d]", "", mention_name)
        if len(digits) >= 10:
            from apps.oms.domain.value_objects import PhoneNumber
            phone = PhoneNumber.from_raw(digits)
            if phone.is_valid:
                for staff in self._staff_dir.values():
                    if staff.number == phone.normalized:
                        return staff

        return None