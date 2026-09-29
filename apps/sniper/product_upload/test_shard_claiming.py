"""Tests for dynamic row-claiming helpers used by ``--workers`` shards.

Pure logic + real temp files: no browser, no CRM.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import apps.sniper.run_upload as ru  # noqa: E402
from apps.sniper.work_queue import OrderClaimQueue  # noqa: E402


def _order(name: str, price: str = "1000") -> dict:
    return {"customer_name": name, "phone_number": "0801",
            "product": "Collagen", "price": price, "address": "Ikeja, Lagos"}


# ------------------------------------------------------------------ dedupe
def test_dedupe_orders_drops_repeated_keys_and_counts_them():
    orders = [_order("Ada"), _order("Bola"), _order("Ada"), _order("Ada")]

    unique, dropped = ru._dedupe_orders(orders)

    assert [o["customer_name"] for o in unique] == ["Ada", "Bola"]
    assert dropped == 2


def test_dedupe_orders_keeps_rows_whose_key_cannot_be_computed():
    orders = [{"customer_name": "", "phone_number": "", "product": "", "price": ""},
              _order("Ada")]

    unique, dropped = ru._dedupe_orders(orders)

    # The unkeyable row is kept (it cannot collide with anything).
    assert len(unique) == 2 and dropped == 0


# --------------------------------------------------------- claimed iteration
def test_without_claim_queue_rows_keep_file_order():
    orders = [_order(f"C{i}") for i in range(3)]
    todo = [(i + 1, o) for i, o in enumerate(orders)]

    got = [row_number for row_number, _ in ru._iter_claimed_rows(todo, orders, None)]

    assert got == [1, 2, 3]


def test_workers_steal_rows_and_every_row_runs_once(tmp_path):
    orders = [_order(f"C{i}") for i in range(4)]
    todo = [(i + 1, o) for i, o in enumerate(orders)]
    worker_a = OrderClaimQueue(tmp_path / "claims")
    worker_b = OrderClaimQueue(tmp_path / "claims")

    gen_a = ru._iter_claimed_rows(todo, orders, worker_a)
    gen_b = ru._iter_claimed_rows(todo, orders, worker_b)

    # Interleave: B is slower, A keeps stealing the next free row.
    first_a = next(gen_a)[0]          # A claims 1
    first_b = next(gen_b)[0]          # B claims 2 (1 is taken)
    second_a = next(gen_a)[0]         # A finishes 1, claims 3
    second_b = next(gen_b)[0]         # B finishes 2, claims 4

    assert sorted([first_a, first_b, second_a, second_b]) == [1, 2, 3, 4]
    assert list(gen_a) == [] and list(gen_b) == []


def test_already_done_rows_are_skipped_without_shifting_positions(tmp_path):
    orders = [_order(f"C{i}") for i in range(3)]
    # Row 1 is already done in the progress DB, so it is not in ``todo``.
    todo = [(2, orders[1]), (3, orders[2])]
    queue = OrderClaimQueue(tmp_path / "claims")

    got = [row_number for row_number, _ in ru._iter_claimed_rows(todo, orders, queue)]

    assert got == [2, 3]


def test_crashed_row_is_offered_again_to_the_same_worker(tmp_path):
    orders = [_order("Ada"), _order("Bola")]
    todo = [(1, orders[0]), (2, orders[1])]
    queue = OrderClaimQueue(tmp_path / "claims")

    crashed = ru._iter_claimed_rows(todo, orders, queue)
    assert next(crashed)[0] == 1
    crashed.close()                    # session died before mark_complete

    resumed = [row_number for row_number, _ in ru._iter_claimed_rows(todo, orders, queue)]
    assert resumed == [1, 2]


# ------------------------------------------------------------- planning row
def test_plan_row_first_blank_address_is_failure_without_browser(monkeypatch):
    monkeypatch.setattr(ru, "_handle_missing_address", lambda *a, **k: True)

    plan, failure, missing_address = ru._plan_row(
        _order("Ada", price="1000") | {"address": ""}, 7, ["Collagen"], {})

    assert plan is None
    assert missing_address is True
    assert "no address" in failure


def test_plan_row_second_blank_address_uses_lagos_default(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ru, "_handle_missing_address", lambda *a, **k: False)

    def fake_build(order, catalog):
        seen["order"] = order
        return ru.ExecutionPlan(order={}, matches=(), state="Lagos")

    monkeypatch.setattr(ru, "build_execution_plan", fake_build)

    plan, failure, missing_address = ru._plan_row(
        _order("Ada") | {"address": ""}, 8, ["Collagen"], {})

    assert plan is not None
    # The Lagos-default path marks the plan so the run reports it.
    assert plan.used_state_fallback is True
    assert failure is None and missing_address is False
    # The Lagos default is what planning sees; the raw order is untouched.
    assert seen["order"]["address"] == "Lagos"


def test_plan_row_healthy_address_reaches_planning_unchanged(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ru, "_handle_missing_address",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("healthy row must not consult the counter")))

    def fake_build(order, catalog):
        seen["order"] = order
        return ru.ExecutionPlan(order={}, matches=(), state=None)

    monkeypatch.setattr(ru, "build_execution_plan", fake_build)
    order = _order("Ada")

    plan, failure, missing_address = ru._plan_row(order, 9, ["Collagen"], {})

    assert plan is not None and plan.used_state_fallback is False
    assert failure is None and missing_address is False
    assert seen["order"]["address"] == order["address"]


def test_plan_row_planning_error_is_reported_as_failure(monkeypatch):
    monkeypatch.setattr(ru, "_handle_missing_address", lambda *a, **k: False)

    def fake_build(order, catalog):
        raise ru.PlanningError("no sellable product found in Excel product text")

    monkeypatch.setattr(ru, "build_execution_plan", fake_build)

    plan, failure, missing_address = ru._plan_row(_order("Ada"), 10, ["Collagen"], {})

    assert plan is None and missing_address is False
    assert "no sellable product" in failure
