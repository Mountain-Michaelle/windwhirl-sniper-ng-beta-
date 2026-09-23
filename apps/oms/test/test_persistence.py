import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from apps.oms.infrastructure.persistence.database import Database
from apps.oms.infrastructure.persistence.order_repository import OrderRepository
from apps.oms.infrastructure.persistence.assignment_repository import AssignmentRepository
from apps.oms.infrastructure.persistence.duplicate_repository import DuplicateRepository
from apps.oms.application.models.parsed_order import ParsedOrder, PackageInfo
from apps.oms.application.models.validated_order import ValidatedOrder
from apps.oms.application.models.validation_report import ValidationReport
from apps.oms.application.models.duplicate_group import DuplicateGroup


# ── Test DB fixture ───────────────────────────────────────────────

def make_test_db():
    '''Create an in-memory SQLite database for testing.'''
    db = Database("sqlite:///:memory:")
    db.init()
    return db


def make_order_repo(db: Database) -> OrderRepository:
    return OrderRepository(db.session_factory)


def make_assignment_repo(db: Database) -> AssignmentRepository:
    return AssignmentRepository(db.session_factory)


def make_duplicate_repo(db: Database) -> DuplicateRepository:
    return DuplicateRepository(db.session_factory)


def make_validated(order_id: str = "ORD-001", **kwargs) -> ValidatedOrder:
    parsed = ParsedOrder(
        order_id        =order_id,
        worker_number   =kwargs.get("worker_number", ""),
        customer_name   =kwargs.get("customer_name",  "Blessing Adeyemi"),
        phone_number    =kwargs.get("phone_number",   "08031234567"),
        whatsapp_number =kwargs.get("whatsapp_number","08031234567"),
        package         =PackageInfo("1 Combo Set", "", "#29,500", 29500.0),
        delivery_address=kwargs.get("address", "12 Allen Ave, Ikeja Lagos"),
        delivery_request="Tomorrow",
        raw_text        ="raw test message",
    )
    report = ValidationReport()
    report.is_valid     = True
    report.quality_score= 0.9
    return ValidatedOrder(parsed_order=parsed, report=report)


# ── Database initialization ───────────────────────────────────────

def test_database_init():
    db = make_test_db()
    assert db.session_factory is not None


def test_database_creates_tables():
    db = make_test_db()
    # If tables weren't created, queries below would fail
    repo = make_order_repo(db)
    assert repo is not None


# ── OrderRepository ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_and_retrieve_order():
    db   = make_test_db()
    repo = make_order_repo(db)
    va   = make_validated("ORD-001")

    await repo.save_validated_order(va)
    record = await repo.get_by_id("ORD-001")

    assert record is not None
    assert record.order_id      == "ORD-001"
    assert record.customer_name == "Blessing Adeyemi"
    assert record.phone_number  == "08031234567"


@pytest.mark.asyncio
async def test_upsert_order():
    '''Saving same order_id twice updates, not duplicates.'''
    db   = make_test_db()
    repo = make_order_repo(db)
    va   = make_validated("ORD-001")

    await repo.save_validated_order(va)
    await repo.save_validated_order(va)  # Second save

    records = await repo.get_today()
    assert len([r for r in records if r.order_id == "ORD-001"]) == 1


@pytest.mark.asyncio
async def test_update_assignment():
    db   = make_test_db()
    repo = make_order_repo(db)
    va   = make_validated("ORD-001")
    await repo.save_validated_order(va)

    await repo.update_assignment("ORD-001", "2348031111111")
    record = await repo.get_by_id("ORD-001")

    assert record.worker_number    == "2348031111111"
    assert record.assignment_status == "ASSIGNED"


@pytest.mark.asyncio
async def test_get_today_returns_todays_orders():
    db   = make_test_db()
    repo = make_order_repo(db)

    await repo.save_validated_order(make_validated("ORD-A"))
    await repo.save_validated_order(make_validated("ORD-B"))

    records = await repo.get_today()
    order_ids = [r.order_id for r in records]

    assert "ORD-A" in order_ids
    assert "ORD-B" in order_ids


@pytest.mark.asyncio
async def test_get_pending():
    db   = make_test_db()
    repo = make_order_repo(db)

    await repo.save_validated_order(make_validated("ORD-001"))
    await repo.save_validated_order(make_validated("ORD-002"))
    await repo.update_assignment("ORD-001", "2348XXXXXXXXX")

    pending = await repo.get_pending()
    pending_ids = [r.order_id for r in pending]

    assert "ORD-002" in pending_ids
    assert "ORD-001" not in pending_ids  # Was assigned


@pytest.mark.asyncio
async def test_get_by_worker():
    db   = make_test_db()
    repo = make_order_repo(db)

    await repo.save_validated_order(make_validated("ORD-001"))
    await repo.save_validated_order(make_validated("ORD-002"))
    await repo.update_assignment("ORD-001", "2348031111111")
    await repo.update_assignment("ORD-002", "2348032222222")

    worker1_orders = await repo.get_by_worker("2348031111111")
    assert len(worker1_orders) == 1
    assert worker1_orders[0].order_id == "ORD-001"


@pytest.mark.asyncio
async def test_get_in_window():
    db   = make_test_db()
    repo = make_order_repo(db)

    await repo.save_validated_order(make_validated("ORD-001"))
    await repo.save_validated_order(make_validated("ORD-002"))

    since = datetime.now().replace(hour=0, minute=0, second=0)
    candidates = await repo.get_in_window(since, exclude_id="ORD-001")

    order_ids = [r.order_id for r in candidates]
    assert "ORD-002" in order_ids
    assert "ORD-001" not in order_ids  # Excluded


@pytest.mark.asyncio
async def test_count_by_status():
    db   = make_test_db()
    repo = make_order_repo(db)

    await repo.save_validated_order(make_validated("ORD-001"))
    await repo.save_validated_order(make_validated("ORD-002"))
    await repo.update_assignment("ORD-001", "2348XXXXXXXXX")

    counts = await repo.count_by_status()
    assert counts.get("PENDING", 0) >= 1
    assert counts.get("ASSIGNED", 0) >= 1


# ── DuplicateRepository ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_and_retrieve_group():
    db   = make_test_db()
    orepo = make_order_repo(db)
    drepo = make_duplicate_repo(db)

    # Must save orders first (FK constraint)
    await orepo.save_validated_order(make_validated("ORD-A"))
    await orepo.save_validated_order(make_validated("ORD-B"))

    group = DuplicateGroup(
        canonical_order_id="ORD-A",
        classification    ="LIKELY_DUPLICATE",
    )
    group.add_member("ORD-B")

    await drepo.save_group(group)
    retrieved = await drepo.get_group(group.group_id)

    assert retrieved is not None
    assert retrieved.canonical_order_id == "ORD-A"


@pytest.mark.asyncio
async def test_get_groups_for_order():
    db    = make_test_db()
    orepo = make_order_repo(db)
    drepo = make_duplicate_repo(db)

    await orepo.save_validated_order(make_validated("ORD-A"))
    await orepo.save_validated_order(make_validated("ORD-B"))

    group = DuplicateGroup(
        canonical_order_id="ORD-A",
        classification    ="CONFIRMED_DUPLICATE",
    )
    group.add_member("ORD-B")
    await drepo.save_group(group)

    groups = await drepo.get_groups_for_order("ORD-B")
    assert len(groups) >= 1
    assert groups[0].canonical_order_id == "ORD-A"


# ── Excel export ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_excel_export_daily(tmp_path):
    from apps.oms.infrastructure.persistence.excel_exporter import ExcelExporter

    db   = make_test_db()
    repo = make_order_repo(db)

    await repo.save_validated_order(make_validated("ORD-001"))
    await repo.save_validated_order(make_validated("ORD-002"))

    exporter = ExcelExporter(repo, reports_dir=str(tmp_path))
    path     = await exporter.export_daily()

    assert path.exists()
    assert path.suffix == ".xlsx"
    assert path.stat().st_size > 0


@pytest.mark.asyncio
async def test_excel_export_by_worker(tmp_path):
    from apps.oms.infrastructure.persistence.excel_exporter import ExcelExporter

    db   = make_test_db()
    repo = make_order_repo(db)

    va = make_validated("ORD-001")
    await repo.save_validated_order(va)
    await repo.update_assignment("ORD-001", "2348031111111")

    exporter = ExcelExporter(repo, reports_dir=str(tmp_path))
    path     = await exporter.export_by_worker("2348031111111")

    assert path.exists()
    assert "2348031111111" in path.name
