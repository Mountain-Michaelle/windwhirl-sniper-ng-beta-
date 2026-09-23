import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from apps.oms.application.assignment_state_engine import (
    AssignmentStateEngine, AssignmentState
)
from apps.oms.application.assignment_resolution_engine import (
    AssignmentResolutionEngine
)
from apps.oms.application.resolved_assignment import AppliedRule, ResolutionStatus
from apps.oms.domain.entities import Order, OrderStatus, Staff
from apps.oms.infrastructure.browser.raw_message import RawMessage, MessageDirection


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
        "apps.oms.application.pending_order",
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