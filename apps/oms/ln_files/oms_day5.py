
# ==============================================================
# WINDWHIRL OMS — DAY 5: ASSIGNMENT STATE ENGINE
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → application/pending_order.py
#   FILE 2  → application/worker_context.py
#   FILE 3  → application/assignment_window.py
#   FILE 4  → application/order_timeline.py
#   FILE 5  → application/worker_timeline.py
#   FILE 6  → application/assignment_timeline.py
#   FILE 7  → application/assignment_state_engine.py
#   FILE 8  → application/__init__.py          (update)
#   FILE 9  → oms_runner.py                    (update)
#
# WHAT THIS DAY DOES AND DOES NOT DO:
#
#   ✅ Maintains current assignment window
#   ✅ Buffers pending orders chronologically
#   ✅ Tracks current worker context
#   ✅ Records candidate workers on orders (hint only)
#   ✅ Maintains three timelines: order, worker, assignment
#   ✅ Increments state version on every mutation
#   ✅ Emits state-change events
#
#   ❌ Does NOT decide who owns an order
#   ❌ Does NOT assign workers
#   ❌ Does NOT resolve pending orders
#   ❌ Does NOT parse orders
#   ❌ Does NOT validate data
#   ❌ Does NOT write to database
#   ❌ Does NOT touch Google Sheets
#
# MENTAL MODEL:
#   This engine is a court reporter — it records everything
#   that happens, in order, with timestamps, without opinion.
#   Day 6 (Assignment Resolution Engine) reads this record
#   and makes ownership decisions based on it.
#
# STATE MAINTAINED:
#   AssignmentWindow    → the current active batch session
#   PendingOrderBuffer  → orders waiting (never removed here)
#   WorkerContext       → the currently active worker mention
#   OrderTimeline       → every order that arrived, in order
#   WorkerTimeline      → every worker context change, in order
#   AssignmentTimeline  → every state event, in order
#   state_version       → increments on every mutation
#
# EVENTS EMITTED:
#   "assignment.window.opened"
#   "assignment.window.updated"
#   "assignment.window.closed"
#   "assignment.pending_order.added"
#   "assignment.pending_order.updated"
#   "assignment.worker_context.changed"
#   "assignment.candidate_worker.set"
#   "assignment.state.updated"
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/application/pending_order.py
# ================================================================
# PURPOSE:
#   Represents one order sitting in the pending buffer.
#   Pure data container — no business logic.
#   Holds a reference to the Order entity and metadata
#   about its presence in the buffer.
#
# KEY FIELD — candidate_worker:
#   If a Worker Context was active when this order arrived,
#   that worker is stored here as a CANDIDATE only.
#   This is NOT ownership. Day 6 decides if it becomes ownership.
#   The field is a hint — nothing more.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from app.oms.domain.entities import Order


class PendingOrderStatus(str, Enum):
    '''
    The lifecycle state of an order in the pending buffer.

    WAITING:    Order arrived, no worker context yet.
    CANDIDATE:  Order arrived with a worker context recorded as hint.
    RESOLVED:   Day 6 has determined ownership. Removed from buffer.
    '''
    WAITING   = "WAITING"
    CANDIDATE = "CANDIDATE"
    RESOLVED  = "RESOLVED"


@dataclass
class PendingOrder:
    '''
    An order in the pending assignment buffer.

    Attributes:
        buffer_id:        Unique ID for this buffer entry.
        order:            The Order entity detected by the parser.
        raw_message_id:   Fingerprint of the source RawMessage.
        window_id:        Which assignment window this belongs to.
        status:           Current status within the buffer.
        created_at:       When this order entered the buffer.
        updated_at:       Last mutation timestamp.
        candidate_worker: Phone number of a candidate worker if one
                          was active when this order arrived.
                          NOT ownership — only a hint for Day 6.
        notes:            Any contextual notes for debugging.
    '''
    order:          Order
    raw_message_id: str
    window_id:      str
    buffer_id:      str                = field(
                        default_factory=lambda: str(uuid.uuid4())[:8]
                    )
    status:         PendingOrderStatus = PendingOrderStatus.WAITING
    created_at:     datetime           = field(default_factory=datetime.now)
    updated_at:     datetime           = field(default_factory=datetime.now)
    candidate_worker: str              = ""
    notes:          str                = ""

    def set_candidate(self, worker_number: str) -> None:
        '''
        Record a candidate worker hint on this order.
        Does NOT assign ownership. Day 6 makes that decision.

        Args:
            worker_number: Phone number of the candidate worker.
        '''
        self.candidate_worker = worker_number
        self.status           = PendingOrderStatus.CANDIDATE
        self.updated_at       = datetime.now()

    def mark_resolved(self) -> None:
        '''
        Mark this buffer entry as resolved.
        Called by Day 6 after ownership is determined.
        '''
        self.status     = PendingOrderStatus.RESOLVED
        self.updated_at = datetime.now()

    @property
    def has_candidate(self) -> bool:
        return bool(self.candidate_worker)

    @property
    def is_waiting(self) -> bool:
        return self.status == PendingOrderStatus.WAITING

    @property
    def is_resolved(self) -> bool:
        return self.status == PendingOrderStatus.RESOLVED

    def __repr__(self):
        cw = f", candidate=+{self.candidate_worker}" if self.candidate_worker else ""
        return (
            f"PendingOrder("
            f"id={self.buffer_id!r}, "
            f"order={self.order.order_id!r}, "
            f"status={self.status.value}"
            f"{cw})"
        )
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/application/worker_context.py
# ================================================================
# PURPOSE:
#   Represents the currently active worker context.
#   Updated whenever an ASSIGNMENT-classified message arrives.
#
# CRITICAL RULE:
#   Worker Context is NOT ownership.
#   It is only the currently mentioned worker.
#   Having a Worker Context does NOT mean pending orders
#   belong to that worker. Day 6 makes that call.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class WorkerContextEntry:
    '''
    One worker context state at a point in time.
    Stored in the WorkerTimeline — never mutated after creation.

    worker_number:   Phone number of the mentioned worker.
    display_name:    Name as mentioned in the WhatsApp message.
    mentioned_at:    When this context became active.
    raw_message_id:  Source message fingerprint for traceability.
    window_id:       Which assignment window was active at the time.
    '''
    worker_number:  str
    display_name:   str
    raw_message_id: str
    window_id:      str
    mentioned_at:   datetime = field(default_factory=datetime.now)

    def __repr__(self):
        return (
            f"WorkerContextEntry("
            f"+{self.worker_number}, "
            f"name={self.display_name!r}, "
            f"at={self.mentioned_at.strftime('%H:%M:%S')})"
        )


class CurrentWorkerContext:
    '''
    Tracks the currently active worker context.

    Mutable — changes every time a new worker is mentioned.
    The WorkerTimeline records every historical change.
    This class only holds the CURRENT state.

    Remember: this is NOT ownership.
    It is simply the most recently mentioned worker.
    '''

    def __init__(self):
        self._current: Optional[WorkerContextEntry] = None

    @property
    def active(self) -> Optional[WorkerContextEntry]:
        '''The current worker context entry, or None if none set.'''
        return self._current

    @property
    def worker_number(self) -> str:
        '''Current worker phone number, or empty string.'''
        return self._current.worker_number if self._current else ""

    @property
    def display_name(self) -> str:
        '''Current worker display name, or empty string.'''
        return self._current.display_name if self._current else ""

    @property
    def is_active(self) -> bool:
        '''True if a worker context is currently set.'''
        return self._current is not None

    def update(self, entry: WorkerContextEntry) -> None:
        '''
        Set a new worker context.
        The previous context is NOT stored here —
        it is stored in WorkerTimeline.

        Args:
            entry: The new WorkerContextEntry to set as current.
        '''
        self._current = entry

    def clear(self) -> None:
        '''Clear the current worker context. Used when window closes.'''
        self._current = None

    def __repr__(self):
        if self._current:
            return (
                f"CurrentWorkerContext("
                f"+{self.worker_number}, "
                f"{self.display_name!r})"
            )
        return "CurrentWorkerContext(none)"
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/application/assignment_window.py
# ================================================================
# PURPOSE:
#   Represents one active assignment session window.
#   A window groups related orders and worker mentions together.
#
# WINDOW LIFECYCLE:
#   Opens when: first ORDER or first ASSIGNMENT message arrives
#   Updates when: any state change occurs within the window
#   Closes when: Day 6 signals all pending orders resolved,
#                or business rules explicitly request closure
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from app.oms.application.pending_order import PendingOrder


class WindowStatus(str, Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class AssignmentWindow:
    '''
    One continuous assignment session.

    Tracks all activity between when the window opened and closed.
    Contains references to all pending orders in this window.
    Never contains ownership decisions — those belong to Day 6.

    Attributes:
        window_id:       Unique identifier.
        opened_at:       When this window was opened.
        updated_at:      Most recent state change inside this window.
        closed_at:       When window was closed (None if still open).
        status:          OPEN or CLOSED.
        pending_orders:  All PendingOrder entries in this window,
                         in chronological arrival order.
        current_worker:  Phone number of most recent worker context.
                         NOT ownership — just current context.
        order_count:     How many orders entered this window.
        version:         Internal mutation counter for this window.
    '''
    window_id:     str            = field(
                       default_factory=lambda: str(uuid.uuid4())[:8]
                   )
    opened_at:     datetime       = field(default_factory=datetime.now)
    updated_at:    datetime       = field(default_factory=datetime.now)
    closed_at:     Optional[datetime] = None
    status:        WindowStatus   = WindowStatus.OPEN
    pending_orders: list          = field(default_factory=list)
    current_worker: str           = ""
    order_count:   int            = 0
    version:       int            = 0

    @property
    def is_open(self) -> bool:
        return self.status == WindowStatus.OPEN

    @property
    def pending_count(self) -> int:
        '''Number of orders still waiting in this window.'''
        return sum(
            1 for po in self.pending_orders
            if not po.is_resolved
        )

    def add_pending_order(self, pending: PendingOrder) -> None:
        '''Add a pending order to this window. Increments counters.'''
        self.pending_orders.append(pending)
        self.order_count += 1
        self._touch()

    def update_worker_context(self, worker_number: str) -> None:
        '''Record the new current worker context. NOT ownership.'''
        self.current_worker = worker_number
        self._touch()

    def close(self) -> None:
        '''Close this window. Called by Day 6 or business rules.'''
        self.status    = WindowStatus.CLOSED
        self.closed_at = datetime.now()
        self._touch()

    def _touch(self) -> None:
        '''Update the updated_at timestamp and increment version.'''
        self.updated_at = datetime.now()
        self.version   += 1

    def snapshot(self) -> dict:
        '''Serializable snapshot of this window for events and logging.'''
        return {
            "window_id":     self.window_id,
            "status":        self.status.value,
            "opened_at":     self.opened_at.isoformat(),
            "order_count":   self.order_count,
            "pending_count": self.pending_count,
            "current_worker": self.current_worker,
            "version":       self.version,
        }

    def __repr__(self):
        return (
            f"AssignmentWindow("
            f"id={self.window_id!r}, "
            f"status={self.status.value}, "
            f"orders={self.order_count}, "
            f"pending={self.pending_count}, "
            f"worker=+{self.current_worker or 'none'})"
        )
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/application/order_timeline.py
# ================================================================
# PURPOSE:
#   Append-only chronological record of every order that arrived.
#   Day 6 reads this to understand the sequence of orders.
#   Never reordered. Never deleted.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.oms.domain.entities import Order


@dataclass(frozen=True)
class OrderTimelineEntry:
    '''
    One immutable record in the order timeline.

    order_id:       The order identifier.
    customer_name:  Customer name for quick reference.
    arrived_at:     When this order was detected.
    window_id:      Which assignment window was active.
    raw_message_id: Source RawMessage fingerprint.
    sequence_num:   Position in the overall order sequence (1-based).
    '''
    order_id:       str
    customer_name:  str
    arrived_at:     datetime
    window_id:      str
    raw_message_id: str
    sequence_num:   int

    def __repr__(self):
        return (
            f"OrderTimelineEntry("
            f"#{self.sequence_num}, "
            f"order={self.order_id!r}, "
            f"customer={self.customer_name!r}, "
            f"window={self.window_id!r})"
        )


class OrderTimeline:
    '''
    Append-only chronological record of all detected orders.
    Maintains the exact sequence in which orders arrived.
    Day 6 uses this sequence when applying assignment rules.

    Usage:
        timeline = OrderTimeline()
        timeline.record(order, window_id, message_id)
        entries = timeline.all_entries()
        entries = timeline.for_window(window_id)
    '''

    def __init__(self):
        self._entries: list[OrderTimelineEntry] = []

    def record(
        self,
        order:          Order,
        window_id:      str,
        raw_message_id: str,
    ) -> OrderTimelineEntry:
        '''
        Record an order arrival in the timeline.

        Args:
            order:          The detected Order entity.
            window_id:      ID of the currently active window.
            raw_message_id: Fingerprint of the source message.

        Returns:
            The immutable OrderTimelineEntry that was created.
        '''
        entry = OrderTimelineEntry(
            order_id       =order.order_id,
            customer_name  =order.customer_name,
            arrived_at     =datetime.now(),
            window_id      =window_id,
            raw_message_id =raw_message_id,
            sequence_num   =len(self._entries) + 1,
        )
        self._entries.append(entry)
        return entry

    def all_entries(self) -> list[OrderTimelineEntry]:
        '''All timeline entries, oldest first.'''
        return list(self._entries)

    def for_window(self, window_id: str) -> list[OrderTimelineEntry]:
        '''All entries for a given window ID, in arrival order.'''
        return [e for e in self._entries if e.window_id == window_id]

    def latest(self) -> Optional[OrderTimelineEntry]:
        '''Most recent order entry, or None.'''
        return self._entries[-1] if self._entries else None

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def __repr__(self):
        return f"OrderTimeline(total={self.total_count})"
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/application/worker_timeline.py
# ================================================================
# PURPOSE:
#   Append-only chronological record of every worker context change.
#   Preserves the exact sequence: Worker A at 09:00, Worker B at 09:05.
#   Day 6 reads this sequence when resolving ownership.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.oms.application.worker_context import WorkerContextEntry


@dataclass(frozen=True)
class WorkerTimelineEntry:
    '''
    One immutable record of a worker context change.

    worker_number:  Phone number of the mentioned worker.
    display_name:   Name as it appeared in the message.
    changed_at:     When this context became active.
    window_id:      Active window at the time.
    raw_message_id: Source message fingerprint.
    sequence_num:   Position in the worker timeline (1-based).
    '''
    worker_number:  str
    display_name:   str
    changed_at:     datetime
    window_id:      str
    raw_message_id: str
    sequence_num:   int

    def __repr__(self):
        return (
            f"WorkerTimelineEntry("
            f"#{self.sequence_num}, "
            f"+{self.worker_number}, "
            f"name={self.display_name!r}, "
            f"window={self.window_id!r})"
        )


class WorkerTimeline:
    '''
    Append-only record of every worker context change.
    Never modified after recording. Never reordered.

    Usage:
        timeline = WorkerTimeline()
        timeline.record(worker_number, display_name, window_id, message_id)
        all_changes = timeline.all_entries()
    '''

    def __init__(self):
        self._entries: list[WorkerTimelineEntry] = []

    def record(
        self,
        worker_number:  str,
        display_name:   str,
        window_id:      str,
        raw_message_id: str,
    ) -> WorkerTimelineEntry:
        '''
        Record a worker context change.

        Args:
            worker_number:  Phone number of the new worker context.
            display_name:   How the worker was mentioned (@Michael etc).
            window_id:      ID of the currently active window.
            raw_message_id: Fingerprint of the source message.

        Returns:
            The immutable WorkerTimelineEntry created.
        '''
        entry = WorkerTimelineEntry(
            worker_number  =worker_number,
            display_name   =display_name,
            changed_at     =datetime.now(),
            window_id      =window_id,
            raw_message_id =raw_message_id,
            sequence_num   =len(self._entries) + 1,
        )
        self._entries.append(entry)
        return entry

    def all_entries(self) -> list[WorkerTimelineEntry]:
        return list(self._entries)

    def for_window(self, window_id: str) -> list[WorkerTimelineEntry]:
        return [e for e in self._entries if e.window_id == window_id]

    def latest(self) -> Optional[WorkerTimelineEntry]:
        return self._entries[-1] if self._entries else None

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def __repr__(self):
        return f"WorkerTimeline(total={self.total_count})"
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/application/assignment_timeline.py
# ================================================================
# PURPOSE:
#   Append-only record of every state-change event in the engine.
#   The complete audit trail of what happened and when.
#   Day 6 uses this alongside the other timelines to make decisions.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class AssignmentEvent(str, Enum):
    '''All possible state-change events in the assignment engine.'''
    WINDOW_OPENED         = "WINDOW_OPENED"
    WINDOW_UPDATED        = "WINDOW_UPDATED"
    WINDOW_CLOSED         = "WINDOW_CLOSED"
    PENDING_ORDER_ADDED   = "PENDING_ORDER_ADDED"
    PENDING_ORDER_UPDATED = "PENDING_ORDER_UPDATED"
    WORKER_CONTEXT_CHANGED = "WORKER_CONTEXT_CHANGED"
    CANDIDATE_WORKER_SET  = "CANDIDATE_WORKER_SET"
    STATE_UPDATED         = "STATE_UPDATED"


@dataclass(frozen=True)
class AssignmentTimelineEntry:
    '''
    One immutable audit record.

    event:       What happened.
    occurred_at: When it happened.
    window_id:   Active window at the time (may be empty for pre-window events).
    entity_id:   The order_id, worker_number, or window_id this event concerns.
    details:     Arbitrary context dict for the event.
    sequence_num: Position in the timeline (1-based).
    '''
    event:        AssignmentEvent
    occurred_at:  datetime
    window_id:    str
    entity_id:    str
    details:      dict
    sequence_num: int

    def __repr__(self):
        return (
            f"AssignmentTimelineEntry("
            f"#{self.sequence_num}, "
            f"{self.event.value}, "
            f"entity={self.entity_id!r})"
        )


class AssignmentTimeline:
    '''
    Complete audit trail of all assignment state changes.
    Append-only. Never modified. Never reordered.

    Usage:
        timeline = AssignmentTimeline()
        timeline.record(event, window_id, entity_id, details)
        all_events = timeline.all_entries()
    '''

    def __init__(self):
        self._entries: list[AssignmentTimelineEntry] = []

    def record(
        self,
        event:     AssignmentEvent,
        window_id: str,
        entity_id: str,
        details:   dict = None,
    ) -> AssignmentTimelineEntry:
        '''
        Record a state-change event.

        Args:
            event:     The type of event that occurred.
            window_id: Active window at the time of the event.
            entity_id: The primary entity affected (order_id, worker, etc.)
            details:   Additional context for this event.

        Returns:
            The immutable AssignmentTimelineEntry created.
        '''
        entry = AssignmentTimelineEntry(
            event       =event,
            occurred_at =datetime.now(),
            window_id   =window_id,
            entity_id   =entity_id,
            details     =details or {},
            sequence_num=len(self._entries) + 1,
        )
        self._entries.append(entry)
        return entry

    def all_entries(self) -> list[AssignmentTimelineEntry]:
        return list(self._entries)

    def for_window(self, window_id: str) -> list[AssignmentTimelineEntry]:
        return [e for e in self._entries if e.window_id == window_id]

    def for_event(self, event: AssignmentEvent) -> list[AssignmentTimelineEntry]:
        return [e for e in self._entries if e.event == event]

    def latest(self) -> Optional[AssignmentTimelineEntry]:
        return self._entries[-1] if self._entries else None

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def __repr__(self):
        return f"AssignmentTimeline(total={self.total_count})"
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/application/assignment_state_engine.py
# ================================================================
# PURPOSE:
#   The Assignment State Engine — the memory of the OMS.
#   Observes classified messages and maintains complete state.
#   Makes NO decisions. Records facts only.
#
# TWO PUBLIC METHODS:
#   observe_order(order, message)      → called for ORDER messages
#   observe_assignment(mention, message) → called for ASSIGNMENT messages
#
# Both methods:
#   1. Update relevant state objects
#   2. Record to relevant timelines
#   3. Increment state_version
#   4. Emit state-change events
#   5. Return the updated state snapshot
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.oms.application.pending_order import PendingOrder, PendingOrderStatus
from app.oms.application.worker_context import (
    CurrentWorkerContext,
    WorkerContextEntry,
)
from app.oms.application.assignment_window import AssignmentWindow, WindowStatus
from app.oms.application.order_timeline import OrderTimeline
from app.oms.application.worker_timeline import WorkerTimeline
from app.oms.application.assignment_timeline import (
    AssignmentTimeline,
    AssignmentEvent,
)
from app.oms.domain.entities import Order, Staff
from app.oms.infrastructure.browser.raw_message import RawMessage
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

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
            from app.oms.domain.value_objects import PhoneNumber
            phone = PhoneNumber.from_raw(digits)
            if phone.is_valid:
                for staff in self._staff_dir.values():
                    if staff.number == phone.normalized:
                        return staff

        return None
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/application/__init__.py
# ================================================================
# Update to expose Day 5 components.
# ================================================================
# ==============================================================

"""
from app.oms.application.services import OrderMonitorService
from app.oms.application.classifier import (
    MessageClassifier,
    MessageClass,
    ClassificationResult,
)
from app.oms.application.parser import OrderParser
from app.oms.application.validator import OrderValidator
from app.oms.application.pending_order import PendingOrder, PendingOrderStatus
from app.oms.application.worker_context import CurrentWorkerContext, WorkerContextEntry
from app.oms.application.assignment_window import AssignmentWindow, WindowStatus
from app.oms.application.order_timeline import OrderTimeline, OrderTimelineEntry
from app.oms.application.worker_timeline import WorkerTimeline, WorkerTimelineEntry
from app.oms.application.assignment_timeline import (
    AssignmentTimeline,
    AssignmentTimelineEntry,
    AssignmentEvent,
)
from app.oms.application.assignment_state_engine import (
    AssignmentStateEngine,
    AssignmentState,
)

__all__ = [
    "OrderMonitorService",
    "MessageClassifier", "MessageClass", "ClassificationResult",
    "OrderParser", "OrderValidator",
    "PendingOrder", "PendingOrderStatus",
    "CurrentWorkerContext", "WorkerContextEntry",
    "AssignmentWindow", "WindowStatus",
    "OrderTimeline", "OrderTimelineEntry",
    "WorkerTimeline", "WorkerTimelineEntry",
    "AssignmentTimeline", "AssignmentTimelineEntry", "AssignmentEvent",
    "AssignmentStateEngine", "AssignmentState",
]
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/oms_runner.py
# ================================================================
# Update: wire AssignmentStateEngine into the pipeline.
# The pipeline now passes ORDER and ASSIGNMENT messages to the
# state engine for observation.
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.oms.config.settings import get_settings
from app.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from app.oms.infrastructure.browser.raw_message import RawMessage
from app.oms.infrastructure.browser.message_cache import MessageCache
from app.oms.infrastructure.browser.checkpoint_store import CheckpointStore
from app.oms.infrastructure.browser.recovery_manager import RecoveryManager
from app.oms.infrastructure.browser.dom_observer import DOMObserver
from app.oms.application.classifier import MessageClassifier, MessageClass
from app.oms.application.parser import OrderParser
from app.oms.application.validator import OrderValidator
from app.oms.application.assignment_state_engine import AssignmentStateEngine
from app.oms.application.pipeline import MessagePipeline
from app.oms.domain.entities import Order, Staff
from app.oms.shared.logger import get_logger
from app.oms.events import dispatcher

log = get_logger("oms.runner")


def build_staff_directory(settings) -> dict:
    '''
    Build the staff directory from settings.
    Maps lowercase display name → Staff object.
    Add all known staff members here.
    '''
    staff_configs = getattr(settings.whatsapp, 'staff_members', [])
    directory     = {}

    # Fallback: use configured staff_number as the single staff member
    if not staff_configs and settings.whatsapp.staff_number:
        name = getattr(settings.whatsapp, 'staff_display_name', 'staff')
        staff = Staff(
            number      =settings.whatsapp.staff_number,
            group_name  =settings.whatsapp.group_name,
            display_name=name,
        )
        directory[name.lower()] = staff

    return directory


def build_pipeline(settings, state_engine: AssignmentStateEngine) -> MessagePipeline:
    return MessagePipeline(
        classifier   =MessageClassifier(),
        parser       =OrderParser(),
        validator    =OrderValidator(),
        state_engine =state_engine,
    )


# ── Event listeners ──────────────────────────────────────────────

@dispatcher.on("assignment.window.opened")
async def on_window_opened(**kwargs):
    log.info(
        f"Assignment window opened: {kwargs.get('window_id')!r} "
        f"(state v{kwargs.get('state_version')})"
    )


@dispatcher.on("assignment.worker_context.changed")
async def on_worker_context(**kwargs):
    log.info(
        f"Worker context → +{kwargs.get('worker_number')} "
        f"({kwargs.get('display_name')}) | "
        f"{kwargs.get('pending_updated', 0)} candidate(s) updated | "
        f"NOTE: not ownership"
    )


@dispatcher.on("assignment.pending_order.added")
async def on_pending_added(**kwargs):
    log.info(
        f"Pending order added: {kwargs.get('order_id')!r} | "
        f"customer: {kwargs.get('customer_name')!r} | "
        f"total pending: {kwargs.get('pending_count')}"
    )


@dispatcher.on("assignment.candidate_worker.set")
async def on_candidate_set(**kwargs):
    log.info(
        f"Candidate worker +{kwargs.get('candidate_worker')} "
        f"recorded on order {kwargs.get('order_id')!r} "
        f"(hint only — Day 6 decides ownership)"
    )


@dispatcher.on("assignment.state.updated")
async def on_state_updated(**kwargs):
    log.debug(
        f"State v{kwargs.get('state_version')}: "
        f"trigger={kwargs.get('trigger')}, "
        f"pending={kwargs.get('pending_count')}, "
        f"worker=+{kwargs.get('current_worker') or 'none'}"
    )


@dispatcher.on("assignment.window.closed")
async def on_window_closed(**kwargs):
    log.info(
        f"Window closed: {kwargs.get('window_id')!r} | "
        f"orders: {kwargs.get('order_count')} | "
        f"still pending: {kwargs.get('pending_count')}"
    )


@dispatcher.on("order.detected")
async def on_order_detected(order: Order, **kwargs):
    log.info(
        f"ORDER DETECTED: {order.order_id!r} | "
        f"{order.customer_name} | "
        f"{order.item_summary() if hasattr(order, 'item_summary') else ''}"
    )
    # Day 6 will resolve ownership from state engine here


async def main():
    log.info("Windwhirl OMS starting — Day 5 (Assignment State Engine)...")

    settings       = get_settings()
    staff_directory = build_staff_directory(settings)
    state_engine   = AssignmentStateEngine(staff_directory)
    pipeline       = build_pipeline(settings, state_engine)
    bootstrap      = BrowserBootstrap(settings)
    observer_task  = None

    @dispatcher.on("message.received")
    async def handle_message(message: RawMessage, **kwargs):
        await pipeline.process(message)

    @dispatcher.on("message.recovered")
    async def handle_recovered(message: RawMessage, **kwargs):
        await pipeline.process(message)

    try:
        await bootstrap.start()

        if settings.whatsapp.group_name:
            opened = await bootstrap.session_manager.open_target_group(
                settings.whatsapp.group_name
            )
            if not opened:
                log.warning(
                    f"Could not open group: {settings.whatsapp.group_name!r}"
                )
        else:
            log.warning("whatsapp.group_name not configured")
            await bootstrap.run_forever()
            return

        page = bootstrap.page

        checkpoint_store = CheckpointStore(
            group_name  =settings.whatsapp.group_name,
            data_dir    ="data",
            max_history =settings.observer.checkpoint_history_size,
        )
        cache = MessageCache(max_size=settings.observer.message_cache_size)

        recovery = RecoveryManager(
            page=page, checkpoint_store=checkpoint_store,
            cache=cache, cfg=settings,
        )
        await recovery.run()

        observer = DOMObserver(
            page=page, cache=cache,
            checkpoint_store=checkpoint_store, cfg=settings,
        )
        observer_task = asyncio.create_task(
            observer.run(), name="oms_dom_observer"
        )

        await bootstrap.run_forever()

    except KeyboardInterrupt:
        log.info("Shutting down.")
    except Exception as e:
        log.error(f"OMS runner error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if observer_task and not observer_task.done():
            observer_task.cancel()
            try:
                await observer_task
            except asyncio.CancelledError:
                pass
        log.info(f"State engine stats: {state_engine.stats()}")
        await bootstrap.stop()
        log.info("Windwhirl OMS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
"""


# ==============================================================
# ALSO UPDATE pipeline.py — add state_engine integration
# ==============================================================
# In application/pipeline.py, update __init__ and _run():
#
# ADD to imports:
#   from app.oms.application.assignment_state_engine import AssignmentStateEngine
#
# UPDATE __init__ signature to accept state_engine:
#   def __init__(self, classifier, parser, validator, state_engine):
#       self._classifier   = classifier
#       self._parser       = parser
#       self._validator    = validator
#       self._state_engine = state_engine
#       self._stats        = {...}
#
# UPDATE _run() method — in the ASSIGNMENT branch:
#   if result.message_class == MessageClass.ASSIGNMENT:
#       state = await self._state_engine.observe_assignment(
#           raw_text=message.raw_text,
#           message =message,
#       )
#       log.info(
#           f"Assignment observed. State v{state.state_version}: "
#           f"pending={state.pending_count}, "
#           f"worker=+{state.current_worker or 'none'}"
#       )
#       return None
#
# UPDATE _run() — in the ORDER success path (after validate):
#   state = await self._state_engine.observe_order(order, message)
#   log.info(
#       f"Order observed by state engine. "
#       f"State v{state.state_version}: "
#       f"pending={state.pending_count}"
#   )
#   await dispatcher.emit("order.detected", order=order, source="pipeline")
#   return order
# ==============================================================


# ==============================================================
# DAY 5 VERIFICATION
# ==============================================================
#
# Test 1 — All imports resolve:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.application.pending_order import PendingOrder
#   from app.oms.application.worker_context import CurrentWorkerContext
#   from app.oms.application.assignment_window import AssignmentWindow
#   from app.oms.application.order_timeline import OrderTimeline
#   from app.oms.application.worker_timeline import WorkerTimeline
#   from app.oms.application.assignment_timeline import AssignmentTimeline, AssignmentEvent
#   from app.oms.application.assignment_state_engine import AssignmentStateEngine
#   print('All Day 5 imports OK')
#   "
#
# Test 2 — State engine: order → worker → order scenario:
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.application.assignment_state_engine import AssignmentStateEngine
#   from app.oms.domain.entities import Order, OrderStatus, Staff
#   from app.oms.domain.value_objects import OrderItem
#   from app.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection
#
#   staff    = Staff('2348031111111', 'Nabeau Orders', 'Michael')
#   engine   = AssignmentStateEngine({'michael': staff})
#
#   def make_order(oid):
#       o = Order(order_id=oid, staff_number='', customer_name='Test',
#                 items=[], raw_text='test', source_message=None,
#                 status=OrderStatus.DETECTED)
#       return o
#
#   def make_msg(text, fp='fp1'):
#       return RawMessage(1, fp, 'Mgr', text, '10:00',
#                        MessageDirection.INCOMING, 'Nabeau Orders')
#
#   async def run():
#       # Order arrives first
#       s1 = await engine.observe_order(make_order('ORD-1'), make_msg('order', 'fp1'))
#       print(f'After order: pending={s1.pending_count}, worker={s1.current_worker!r}')
#
#       # Worker context set
#       s2 = await engine.observe_assignment('@Michael', make_msg('@Michael', 'fp2'))
#       print(f'After worker: pending={s2.pending_count}, worker={s2.current_worker!r}')
#
#       # Another order with context active
#       s3 = await engine.observe_order(make_order('ORD-2'), make_msg('order2', 'fp3'))
#       pending = engine.pending_orders
#       print(f'After 2nd order: pending={s3.pending_count}')
#       print(f'ORD-1 candidate: {pending[0].candidate_worker!r}')
#       print(f'ORD-2 candidate: {pending[1].candidate_worker!r}')
#       print(f'State version: {engine.state_version}')
#       print('Stats:', engine.stats())
#
#   asyncio.run(run())
#   "
#
# Expected:
#   After order: pending=1, worker=''
#   After worker: pending=1, worker='2348031111111'
#   After 2nd order: pending=2
#   ORD-1 candidate: '2348031111111'   (updated after worker context set)
#   ORD-2 candidate: '2348031111111'   (inherited from forward context)
#   State version: 4+
#
# ==============================================================
# WHAT DAY 6 BUILDS
# ==============================================================
# Day 6: Assignment Resolution Engine
#   Reads AssignmentState from Day 5.
#   Applies all 8 rules (explicit, sequential, forward, batch, etc.)
#   Determines final ownership for each pending order.
#   Emits "assignment.resolved" events.
#   Updates PendingOrder.mark_resolved() on each order.
#   Requests window closure when buffer empties.
#
# Day 6 receives state_engine.current_state and makes decisions.
# Day 5 engine is never changed — Day 6 only reads from it.
# Clean separation maintained.
# ==============================================================
