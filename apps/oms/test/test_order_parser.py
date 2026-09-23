import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from apps.oms.application.order_parser import OrderParser
from apps.oms.application.models.parsed_order import ExtractionStatus


FULL_ORDER = '''*Tiktok Sadoer*
Select Your Package: 1 Combo set -(1 serum & 1 Cream)
+ Free Doorstep Delivery = #29,500
Input your Full Name: Blessing Adeyemi
Input Phone Number: +08031234567
Input Whatsapp Number: +08031234567
Input Full Address: 12 Allen Avenue, Ikeja Lagos
When do you want us to deliver: Tomorrow
Do you have any questions: No
Order Date: 3rd July'''


PARTIAL_ORDER = '''*Facebook Sadoer*
Select Your Package: 2 Combo sets = #59,000
Input your Full Name: Emeka Okonkwo
Input Full Address: 5 Broad Street, Lagos Island'''


MISSING_ALL = '''Hi please I want to order the product
Thank you'''


MULTI_QUESTION = '''*Sadoer*
Input your Full Name: Fatima Abubakar
Input Phone Number: 07012345678
Input Full Address: Wuse 2, Abuja
When do you want us to deliver: Monday
Do you have any questions: Can I pay on delivery?
Also, do you deliver to Zone 4?
Order Date: 5th August'''


EXTRA_BLANK_LINES = '''*Body Lotion*

Select Your Package: 1 Body Lotion set = #15,000

Input your Full Name:   Titilayo Adekunle

Input Phone Number: 08098765432

Input Whatsapp Number: 08098765432

Input Full Address: 22 Bode Thomas Street, Surulere

When do you want us to deliver: Friday

Do you have any questions: None'''


async def parse(text, order_id="TEST-001"):
    parser = OrderParser()
    return await parser.parse(text, order_id=order_id, worker_number="2348XXXXXXXXX")


# ── Complete order ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_order_status():
    parsed = await parse(FULL_ORDER)
    assert parsed.status == ExtractionStatus.COMPLETE


@pytest.mark.asyncio
async def test_complete_order_campaign():
    parsed = await parse(FULL_ORDER)
    assert parsed.campaign == "Tiktok Sadoer"


@pytest.mark.asyncio
async def test_complete_order_customer():
    parsed = await parse(FULL_ORDER)
    assert parsed.customer_name == "Blessing Adeyemi"


@pytest.mark.asyncio
async def test_complete_order_phone():
    parsed = await parse(FULL_ORDER)
    assert parsed.phone_number == "08031234567"


@pytest.mark.asyncio
async def test_complete_order_whatsapp():
    parsed = await parse(FULL_ORDER)
    assert parsed.whatsapp_number == "08031234567"


@pytest.mark.asyncio
async def test_complete_order_package():
    parsed = await parse(FULL_ORDER)
    assert parsed.package is not None
    assert "Combo set" in parsed.package.name
    assert parsed.package.price_value == 29500.0


@pytest.mark.asyncio
async def test_complete_order_address():
    parsed = await parse(FULL_ORDER)
    assert parsed.delivery_address is not None
    assert "Allen" in parsed.delivery_address
    assert "Lagos" in parsed.delivery_address


@pytest.mark.asyncio
async def test_complete_order_delivery():
    parsed = await parse(FULL_ORDER)
    assert parsed.delivery_request == "Tomorrow"


@pytest.mark.asyncio
async def test_complete_order_no_question():
    '''Customer answered "No" — question should be None.'''
    parsed = await parse(FULL_ORDER)
    assert parsed.customer_question is None


@pytest.mark.asyncio
async def test_complete_order_date():
    parsed = await parse(FULL_ORDER)
    assert parsed.order_date_raw == "3rd July"
    # Date should be parsed (July 3 of current year)
    if parsed.order_date:
        assert parsed.order_date.month == 7
        assert parsed.order_date.day == 3


@pytest.mark.asyncio
async def test_raw_text_preserved():
    '''Raw text must always be preserved exactly.'''
    parsed = await parse(FULL_ORDER)
    assert parsed.raw_text == FULL_ORDER


# ── Partial order ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_order_status():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.status == ExtractionStatus.PARTIAL


@pytest.mark.asyncio
async def test_partial_order_has_name():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.customer_name == "Emeka Okonkwo"


@pytest.mark.asyncio
async def test_partial_order_missing_phone():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.phone_number is None
    assert "phone_number" in parsed.missing_fields


# ── Empty / unrecognized order ────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_order_status():
    parsed = await parse(MISSING_ALL)
    # No labels matched — should be EMPTY or PARTIAL
    assert parsed.status in (ExtractionStatus.EMPTY, ExtractionStatus.PARTIAL)


@pytest.mark.asyncio
async def test_raw_always_preserved_even_for_empty():
    parsed = await parse(MISSING_ALL)
    assert parsed.raw_text == MISSING_ALL


# ── Multi-line question ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_line_question():
    parsed = await parse(MULTI_QUESTION)
    assert parsed.customer_question is not None
    assert "delivery" in parsed.customer_question.lower()


@pytest.mark.asyncio
async def test_multi_question_customer():
    parsed = await parse(MULTI_QUESTION)
    assert parsed.customer_name == "Fatima Abubakar"


@pytest.mark.asyncio
async def test_multi_question_date():
    parsed = await parse(MULTI_QUESTION)
    assert parsed.order_date_raw is not None
    if parsed.order_date:
        assert parsed.order_date.month == 8
        assert parsed.order_date.day == 5


# ── Extra blank lines ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extra_blank_lines_customer():
    '''Extra blank lines must not break extraction.'''
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.customer_name == "Titilayo Adekunle"


@pytest.mark.asyncio
async def test_extra_blank_lines_phone():
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.phone_number == "08098765432"


@pytest.mark.asyncio
async def test_extra_blank_lines_no_question():
    '''Customer answered "None" — question should be None.'''
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.customer_question is None


@pytest.mark.asyncio
async def test_extra_blank_lines_package_price():
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.package is not None
    assert parsed.package.price_value == 15000.0


# ── Campaign extraction ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_campaign_facebook():
    parsed = await parse(PARTIAL_ORDER)
    assert parsed.campaign == "Facebook Sadoer"


@pytest.mark.asyncio
async def test_campaign_body_lotion():
    parsed = await parse(EXTRA_BLANK_LINES)
    assert parsed.campaign == "Body Lotion"
