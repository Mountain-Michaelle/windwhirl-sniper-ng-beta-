import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from apps.oms.application.duplicate.similarity import (
    levenshtein_distance, levenshtein_ratio, normalize_for_comparison, phone_normalize
)
from apps.oms.application.duplicate.phone_matcher import PhoneMatcher
from apps.oms.application.duplicate.name_matcher import NameMatcher
from apps.oms.application.duplicate.address_matcher import AddressMatcher
from apps.oms.application.duplicate.duplicate_detection_engine import (
    DuplicateDetectionEngine, THRESHOLD_CONFIRMED, THRESHOLD_LIKELY
)
from apps.oms.application.models.parsed_order import ParsedOrder, PackageInfo
from apps.oms.application.models.validated_order import ValidatedOrder
from apps.oms.application.models.validation_report import ValidationReport
from apps.oms.application.models.duplicate_result import DuplicateClassification


# ── Helpers ──────────────────────────────────────────────────────

def make_order(
    order_id: str,
    customer: str = "Blessing Adeyemi",
    phone:    str = "08031234567",
    wa:       str = "08031234567",
    address:  str = "12 Allen Avenue, Ikeja Lagos",
    **kwargs,
) -> ParsedOrder:
    return ParsedOrder(
        order_id        =order_id,
        worker_number   ="2348XXX",
        customer_name   =customer,
        phone_number    =phone,
        whatsapp_number =wa,
        package         =PackageInfo("1 Combo Set", "", "#29,500", 29500.0),
        delivery_address=address,
        raw_text        ="raw text",
        parsed_at       =kwargs.get('parsed_at', datetime.now()),
    )


def make_validated(order: ParsedOrder) -> ValidatedOrder:
    return ValidatedOrder(
        parsed_order=order,
        report      =ValidationReport(),
    )


async def check(order_a, order_b=None, window=48.0):
    engine = DuplicateDetectionEngine(window_hours=window)
    va = make_validated(order_a)

    if order_b:
        # Register order_b first so it's in the store
        engine._store.register_order(order_b)

    return await engine.check(va)


# ── Levenshtein ───────────────────────────────────────────────────

def test_levenshtein_identical():
    assert levenshtein_distance("blessing", "blessing") == 0


def test_levenshtein_empty():
    assert levenshtein_distance("", "abc") == 3
    assert levenshtein_distance("abc", "") == 3


def test_levenshtein_ratio_identical():
    assert levenshtein_ratio("Blessing Adeyemi", "Blessing Adeyemi") == 1.0


def test_levenshtein_ratio_completely_different():
    assert levenshtein_ratio("Blessing Adeyemi", "Emeka Okonkwo") < 0.30


def test_levenshtein_ratio_similar_names():
    ratio = levenshtein_ratio(
        normalize_for_comparison("Blessing Adeyemi"),
        normalize_for_comparison("Blessing Adeyemi-Okafor"),
    )
    assert ratio >= 0.75, f"Expected >= 0.75, got {ratio}"


def test_levenshtein_ratio_honorific_stripped():
    ratio = levenshtein_ratio(
        normalize_for_comparison("Mrs Blessing Adeyemi"),
        normalize_for_comparison("Blessing Adeyemi"),
    )
    assert ratio >= 0.80, f"Expected >= 0.80, got {ratio}"


# ── Phone normalize ───────────────────────────────────────────────

def test_phone_normalize_international():
    assert phone_normalize("+2348031234567") == "08031234567"
    assert phone_normalize("2348031234567")  == "08031234567"


def test_phone_normalize_local():
    assert phone_normalize("08031234567") == "08031234567"


def test_phone_normalize_float():
    # Excel artifact
    assert phone_normalize("8031234567.0") == "8031234567"


# ── Phone matcher ─────────────────────────────────────────────────

def test_phone_match_same():
    pm = PhoneMatcher()
    a  = make_order("A", phone="08031234567")
    b  = make_order("B", phone="08031234567")
    r  = pm.compare(a, b)
    assert r.matched is True
    assert r.score == 1.0


def test_phone_match_different():
    pm = PhoneMatcher()
    a  = make_order("A", phone="08031234567")
    b  = make_order("B", phone="08099999999")
    r  = pm.compare(a, b)
    assert r.matched is False
    assert r.score == 0.0


def test_phone_match_via_whatsapp():
    '''Phone in A matches WhatsApp in B.'''
    pm = PhoneMatcher()
    a  = make_order("A", phone="08031234567", wa="08031234567")
    b  = make_order("B", phone="08099999999", wa="08031234567")
    r  = pm.compare(a, b)
    assert r.matched is True


# ── Name matcher ──────────────────────────────────────────────────

def test_name_match_identical():
    nm = NameMatcher()
    a  = make_order("A", customer="Blessing Adeyemi")
    b  = make_order("B", customer="Blessing Adeyemi")
    r  = nm.compare(a, b)
    assert r.matched is True
    assert r.score == 1.0


def test_name_match_similar():
    nm    = NameMatcher()
    a     = make_order("A", customer="Blessing Adeyemi")
    b     = make_order("B", customer="Blessing Adeyemi-Okafor")
    r     = nm.compare(a, b)
    assert r.score >= 0.75


def test_name_no_match_different():
    nm = NameMatcher()
    a  = make_order("A", customer="Blessing Adeyemi")
    b  = make_order("B", customer="Emeka Okonkwo")
    r  = nm.compare(a, b)
    assert r.matched is False


def test_name_match_with_honorific():
    nm = NameMatcher()
    a  = make_order("A", customer="Mrs Blessing Adeyemi")
    b  = make_order("B", customer="Blessing Adeyemi")
    r  = nm.compare(a, b)
    assert r.score >= 0.80


# ── Address matcher ───────────────────────────────────────────────

def test_address_match_identical():
    am = AddressMatcher()
    a  = make_order("A", address="12 Allen Avenue, Ikeja Lagos")
    b  = make_order("B", address="12 Allen Avenue, Ikeja Lagos")
    r  = am.compare(a, b)
    assert r.matched is True


def test_address_no_match_different():
    am = AddressMatcher()
    a  = make_order("A", address="12 Allen Avenue, Ikeja Lagos")
    b  = make_order("B", address="5 Broad Street, Lagos Island")
    r  = am.compare(a, b)
    assert r.matched is False


# ── Full engine — confirmed duplicate ─────────────────────────────

@pytest.mark.asyncio
async def test_confirmed_duplicate_same_order():
    '''Same phone + same name + same address → CONFIRMED.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")

    results = await check(a, b)

    assert len(results) == 1
    assert results[0].classification == DuplicateClassification.CONFIRMED_DUPLICATE
    assert results[0].final_score >= THRESHOLD_CONFIRMED


@pytest.mark.asyncio
async def test_likely_duplicate_phone_only():
    '''Same phone, different name → LIKELY.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")
    b = make_order("B", phone="08031234567", customer="Mrs Blessing",
                   address="Ikeja, Lagos")

    results = await check(a, b)

    assert len(results) == 1
    assert results[0].classification in (
        DuplicateClassification.CONFIRMED_DUPLICATE,
        DuplicateClassification.LIKELY_DUPLICATE,
    )


@pytest.mark.asyncio
async def test_unique_different_phone():
    '''Different phone, different name → UNIQUE.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi",
                   address="12 Allen Avenue, Ikeja Lagos")
    b = make_order("B", phone="07099999999", customer="Emeka Okonkwo",
                   address="5 Broad Street, Lagos Island")

    results = await check(a, b)

    assert len(results) == 1
    assert results[0].classification == DuplicateClassification.UNIQUE


@pytest.mark.asyncio
async def test_outside_window_not_duplicate():
    '''Same phone but outside time window → UNIQUE (returning customer).'''
    old_time = datetime.now() - timedelta(hours=72)
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi",
                   parsed_at=old_time)

    # b is outside the 48h window
    results = await check(a, b, window=48.0)

    assert len(results) == 0 or all(
        r.classification == DuplicateClassification.UNIQUE
        for r in results
    )


@pytest.mark.asyncio
async def test_no_candidates_returns_empty():
    '''New order with no existing orders → empty results.'''
    a       = make_order("A")
    engine  = DuplicateDetectionEngine(window_hours=48)
    va      = make_validated(a)
    results = await engine.check(va)
    assert results == []


@pytest.mark.asyncio
async def test_duplicate_flag_set_on_validated_order():
    '''DUPLICATE_PENDING flag set on ValidatedOrder when duplicate found.'''
    from apps.oms.application.models.validation_report import ValidationFlag

    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi")

    engine = DuplicateDetectionEngine(window_hours=48)
    engine._store.register_order(b)
    va = make_validated(a)
    await engine.check(va)

    assert ValidationFlag.DUPLICATE_PENDING in va.report.flags


@pytest.mark.asyncio
async def test_group_created_for_duplicates():
    '''Duplicate group created when duplicates found.'''
    a = make_order("A", phone="08031234567", customer="Blessing Adeyemi")
    b = make_order("B", phone="08031234567", customer="Blessing Adeyemi")

    engine = DuplicateDetectionEngine(window_hours=48)
    engine._store.register_order(b)
    va = make_validated(a)
    await engine.check(va)

    group = engine._store.get_group_for_order("A")
    assert group is not None
    assert group.has_member("A")
    assert group.has_member("B")


@pytest.mark.asyncio
async def test_stats():
    a = make_order("A")
    b = make_order("B")

    engine = DuplicateDetectionEngine()
    await engine.check(make_validated(a))
    await engine.check(make_validated(b))

    s = engine.stats()
    assert s["total_orders"] >= 2
