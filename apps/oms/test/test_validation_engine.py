import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from apps.oms.application.validation.validation_engine import ValidationEngine
from apps.oms.application.models.parsed_order import ParsedOrder, PackageInfo
from apps.oms.application.models.validation_error import ErrorCode
from apps.oms.application.models.validation_warning import WarningCode
from apps.oms.application.models.validation_report import ValidationFlag
from apps.oms.application.validators.phone_validator import validate_nigerian_phone


# ── Helpers ──────────────────────────────────────────────────────

def make_valid_order(**overrides) -> ParsedOrder:
    '''Build a fully valid ParsedOrder. Override fields as needed.'''
    defaults = dict(
        order_id          ="ORD-001",
        worker_number     ="2348XXXXXXXXX",
        customer_name     ="Blessing Adeyemi",
        phone_number      ="08031234567",
        whatsapp_number   ="08031234567",
        package           =PackageInfo(
                               name="1 Combo Set",
                               description="(1 serum & 1 cream)",
                               price_raw="#29,500",
                               price_value=29500.0,
                           ),
        delivery_address  ="12 Allen Avenue, Ikeja Lagos",
        delivery_request  ="Tomorrow",
        raw_text          ="raw message text",
    )
    defaults.update(overrides)
    return ParsedOrder(**defaults)


async def validate(order: ParsedOrder):
    engine = ValidationEngine()
    return await engine.validate(order)


# ── Valid order ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_order_passes():
    order     = make_valid_order()
    validated = await validate(order)
    assert validated.is_valid is True


@pytest.mark.asyncio
async def test_valid_order_quality_score():
    order     = make_valid_order()
    validated = await validate(order)
    assert validated.quality_score >= 0.8


@pytest.mark.asyncio
async def test_valid_order_has_valid_flag():
    order     = make_valid_order()
    validated = await validate(order)
    assert "VALID" in validated.flags


@pytest.mark.asyncio
async def test_parsed_order_unchanged():
    '''ParsedOrder must never be modified by the engine.'''
    order          = make_valid_order()
    original_name  = order.customer_name
    original_phone = order.phone_number
    validated      = await validate(order)
    assert validated.parsed_order.customer_name == original_name
    assert validated.parsed_order.phone_number  == original_phone


# ── Phone validation ──────────────────────────────────────────────

@pytest.mark.parametrize("phone", [
    "08031234567",
    "07012345678",
    "09012345678",
    "08123456789",
    "09134567890",
    "+2348031234567",
    "2348031234567",
])
def test_valid_nigerian_phones(phone):
    is_valid, reason = validate_nigerian_phone(phone)
    assert is_valid, f"Expected valid: {phone!r} — {reason}"


@pytest.mark.parametrize("phone", [
    "123456",              # Too short
    "0123456789012345",    # Too long
    "0601234567",          # Invalid prefix
    "hello",               # Letters
    "",                    # Empty
    "+442071234567",       # UK number
])
def test_invalid_nigerian_phones(phone):
    is_valid, _ = validate_nigerian_phone(phone)
    assert not is_valid, f"Expected invalid: {phone!r}"


@pytest.mark.asyncio
async def test_invalid_phone_produces_error():
    order     = make_valid_order(phone_number="0601234567")
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.PHONE_INVALID.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_missing_phone_produces_error():
    order     = make_valid_order(phone_number=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.PHONE_MISSING.value in validated.report.error_codes()


# ── WhatsApp validation ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_whatsapp_is_warning():
    order     = make_valid_order(whatsapp_number=None)
    validated = await validate(order)
    # Missing WhatsApp is a warning, not an error
    assert validated.is_valid
    assert WarningCode.WHATSAPP_MISSING.value in validated.report.warning_codes()


@pytest.mark.asyncio
async def test_invalid_whatsapp_produces_error():
    order     = make_valid_order(whatsapp_number="0601234567")
    validated = await validate(order)
    assert ErrorCode.WHATSAPP_INVALID.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_different_phone_whatsapp_is_warning():
    order     = make_valid_order(
        phone_number    ="08031234567",
        whatsapp_number ="07012345678",
    )
    validated = await validate(order)
    # Different numbers is allowed — warning only
    assert validated.is_valid
    assert WarningCode.PHONE_WHATSAPP_DIFFER.value in validated.report.warning_codes()


# ── Address validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_address_fails():
    order     = make_valid_order(delivery_address=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.ADDRESS_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_short_address_warning():
    order     = make_valid_order(delivery_address="Lagos")
    validated = await validate(order)
    # Short but has text — warning not error
    assert WarningCode.ADDRESS_SHORT.value in validated.report.warning_codes()


@pytest.mark.asyncio
async def test_numbers_only_address_fails():
    order     = make_valid_order(delivery_address="12345 67890")
    validated = await validate(order)
    assert ErrorCode.ADDRESS_NO_TEXT.value in validated.report.error_codes()


# ── Package validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_package_fails():
    order     = make_valid_order(package=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.PACKAGE_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_package_no_description_warning():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="#29,500", price_value=29500.0
        )
    )
    validated = await validate(order)
    assert WarningCode.PACKAGE_DESC_MISSING.value in validated.report.warning_codes()


# ── Price validation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_price_error():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="", price_value=None
        )
    )
    validated = await validate(order)
    assert ErrorCode.PRICE_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_negative_price_error():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="-29500", price_value=-29500.0
        )
    )
    validated = await validate(order)
    assert ErrorCode.PRICE_NEGATIVE.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_price_raw_but_no_numeric_warning():
    order = make_valid_order(
        package=PackageInfo(
            name="1 Combo Set", description="",
            price_raw="#??,???", price_value=None
        )
    )
    validated = await validate(order)
    assert WarningCode.PRICE_UNEXTRACTED.value in validated.report.warning_codes()


# ── Missing name ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_name_fails():
    order     = make_valid_order(customer_name=None)
    validated = await validate(order)
    assert not validated.is_valid
    assert ErrorCode.NAME_MISSING.value in validated.report.error_codes()


@pytest.mark.asyncio
async def test_single_word_name_warning():
    order     = make_valid_order(customer_name="Blessing")
    validated = await validate(order)
    assert WarningCode.NAME_SINGLE_WORD.value in validated.report.warning_codes()


# ── Delivery validation ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_delivery_request_warning():
    order     = make_valid_order(delivery_request=None)
    validated = await validate(order)
    # Missing delivery is a warning, not an error
    assert validated.is_valid
    assert WarningCode.DELIVERY_MISSING.value in validated.report.warning_codes()


# ── Quality score ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quality_score_decreases_with_missing_fields():
    full     = await validate(make_valid_order())
    partial  = await validate(make_valid_order(
        phone_number=None,
        delivery_request=None,
    ))
    assert full.quality_score > partial.quality_score


# ── INCOMPLETE and PARTIAL flags ──────────────────────────────────

@pytest.mark.asyncio
async def test_incomplete_flag_when_required_missing():
    order     = make_valid_order(customer_name=None)
    validated = await validate(order)
    assert "INCOMPLETE" in validated.flags


@pytest.mark.asyncio
async def test_partial_flag_when_some_present():
    order     = make_valid_order(delivery_address=None)
    validated = await validate(order)
    # Has name + phone + package but missing address
    assert "PARTIAL" in validated.flags or "INCOMPLETE" in validated.flags
