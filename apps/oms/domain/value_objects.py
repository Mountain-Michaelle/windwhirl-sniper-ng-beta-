from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, List


# ---------------------------------------------------------------
# Value Objects (from your reference)
# ---------------------------------------------------------------
@dataclass(frozen=True)
class PhoneNumber:
    '''
    A normalized Nigerian phone number.
    frozen=True makes it immutable (value object behaviour).

    Stores in E.164 format: 2348XXXXXXXXX (13 digits, no +)
    Accepts any Nigerian format on creation and normalizes it.
    '''
    normalized: str      # E.164 without + e.g. "2348031234567"
    is_valid:   bool

    @staticmethod
    def from_raw(raw: str) -> "PhoneNumber":
        '''
        Parse and normalize a raw phone number string.
        Handles all Nigerian formats:
          08031234567    → 2348031234567
          +2348031234567 → 2348031234567
          +08031234567   → 2348031234567
          2348031234567  → 2348031234567
          234-803-123-4567 → 2348031234567
        '''
        if not raw:
            return PhoneNumber(normalized="", is_valid=False)

        # Strip everything except digits
        digits = re.sub(r"[^\d]", "", raw.strip())

        # Apply normalization rules
        if digits.startswith("0") and not digits.startswith("234"):
            digits = "234" + digits[1:]
        elif not digits.startswith("234"):
            if len(digits) == 10:
                digits = "234" + digits

        # Validate: 13 digits starting with 234
        if len(digits) == 13 and digits.startswith("234"):
            return PhoneNumber(normalized=digits, is_valid=True)

        return PhoneNumber(normalized=digits, is_valid=False)

    @property
    def display(self) -> str:
        '''Human-readable format: +234 803 123 4567'''
        if not self.is_valid or len(self.normalized) != 13:
            return self.normalized
        n = self.normalized
        return f"+{n[:3]} {n[3:6]} {n[6:9]} {n[9:]}"

    def __str__(self) -> str:
        return self.display if self.is_valid else self.normalized

    def __bool__(self) -> bool:
        return self.is_valid


@dataclass(frozen=True)
class OrderItem:
    '''
    A single product line in an order.
    frozen=True: immutable value object.
    '''
    raw_text:   str
    product:    str
    quantity:   int   = 1
    unit_price: float = 0.0

    @property
    def total_price(self) -> float:
        return self.quantity * self.unit_price

    def __str__(self) -> str:
        qty_str = f"{self.quantity}x " if self.quantity != 1 else ""
        return f"{qty_str}{self.product}"


@dataclass(frozen=True)
class CustomerInfo:
    '''
    A customer's contact and delivery information.
    frozen=True: immutable value object.
    '''
    name:    str            = ""
    phone:   Optional[PhoneNumber] = None
    address: str            = ""

    def __post_init__(self):
        # Ensure phone is always a PhoneNumber instance
        if self.phone is None:
            object.__setattr__(self, "phone", PhoneNumber("", False))
        elif isinstance(self.phone, str):
            object.__setattr__(self, "phone", PhoneNumber.from_raw(self.phone))

    @property
    def has_phone(self) -> bool:
        return self.phone is not None and self.phone.is_valid

    @property
    def has_address(self) -> bool:
        return bool(self.address.strip())

    def __str__(self) -> str:
        parts = [self.name]
        if self.has_phone:
            parts.append(str(self.phone))
        if self.has_address:
            parts.append(self.address)
        return " | ".join(p for p in parts if p)


# ---------------------------------------------------------------
# Parser
# ---------------------------------------------------------------
BUBBLE_RE = re.compile(
    r"^\[(\d{1,2}/\d{1,2}(?:/\d{2,4})?,\s*\d{1,2}:\d{2}\s*[AP]M)\]\s*([^:]+):\s?",
    re.MULTILINE,
)

ORDER_START_RE = re.compile(
    r"(?im)^\s*(?:select\s+your\s+(?:preferred\s+)?package(?:\s+below)?|product\b|reorder|follow\s*up\s*reorders?)\b.*$"
)

FIELD_LABELS = {
    "name": r"input\s*(?:your\s+)?full\s*name|^name\b",
    "phone": r"input\s*(?:your\s+)?phone\s*number|^phone(?:\s*number)?\b|^tel\b",
    "whatsapp": r"input\s*(?:your\s+)?whatsapp\s*number|^whatsapp(?:\s*number)?\b",
    "address": r"input\s*(?:your\s+)?full\s*address|^address\b",
    "delivery": r"when\s+do\s+you\s+want\s+us\s+to\s+deliver\s+to\s+you",
    "product": r"select\s+your\s+(?:preferred\s+)?package(?:\s+below)?|^product\b|reorder|follow\s*up\s*reorders?",
    "price": r"^price\b",
}

ALL_LABELS_RE = re.compile(
    r"^\**\s*(?:" + "|".join(FIELD_LABELS.values()) + r")\b",
    re.IGNORECASE,
)

KNOWN_PRODUCT_ALIASES = [
    (r"combo\s*set.*?serum.*?cream|serum\s*&\s*1?\s*cream|c[uo]mbo\s*set", "Sadoer Collagen Combo Set (Serum + Cream)"),
    (r"sad[oe]or\s*collagen\s*face\s*cream|collagen\s*face\s*cream\s*24k", "Sadoer Collagen Face Cream"),
    (r"collagen\s*(?:face\s*cream\s*and\s*)?serum", "Sadoer Collagen Serum"),
    (r"advanced?\s*collagen\s*body(?:\s*lotion)?", "Advanced Collagen Body Lotion"),
    (r"scar\s*repair\s*cream", "Scar Repair Cream"),
    (r"collagen\s*hand\s*cream", "Collagen Hand Cream"),
]

NON_PRODUCT_RE = re.compile(
    r"^(?:free\s+)?(?:door\s*step\s*)?delivery$|^(?:free\s+)?shipping$",
    re.IGNORECASE,
)

NOISE_LINE_RE = re.compile(
    r"^select\s+your\s+(?:preferred\s+)?package(?:\s+below)?\s*:?$|^product\s*:?$|^reorder\s*:?$|^follow\s*up\s*reorders?\s*:?$",
    re.IGNORECASE,
)


def split_bubbles(raw_export: str) -> list[tuple[str, str, str]]:
    """Split a pasted WhatsApp export into (timestamp, sender, body) tuples."""
    matches = list(BUBBLE_RE.finditer(raw_export))
    bubbles = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_export)
        body = raw_export[start:end].strip("\n")
        bubbles.append((m.group(1), m.group(2).strip(), body))
    return bubbles


def split_orders(bubble_body: str) -> list[str]:
    """Split a bubble body into individual order blocks."""
    starts = [m.start() for m in ORDER_START_RE.finditer(bubble_body)]
    if len(starts) <= 1:
        return [bubble_body]
    blocks = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(bubble_body)
        blocks.append(bubble_body[s:e].strip())
    return blocks


def _extract_field(lines: list[str], label_pattern: str, lookahead: int = 4) -> str:
    """Extract field value from lines using label pattern."""
    pat = re.compile(rf"^\**\s*(?:{label_pattern})\s*[:\-]?\s*(.*)$", re.IGNORECASE)
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        m = pat.match(line)
        if not m:
            continue
        inline = m.group(1).strip(" .,;:*?")
        if inline:
            return inline
        # value is on a following line
        for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
            nxt = lines[j].strip(" .,;:*?")
            if not nxt:
                continue
            if ALL_LABELS_RE.match(nxt):
                break
            return nxt
    return ""


def _clean_segment(seg: str) -> str:
    seg = seg.strip(" .,;:-*")
    seg = re.sub(r"\s+", " ", seg)
    return seg


def _canonical_name(raw: str) -> str:
    """Look up a clean display name for a known product."""
    low = raw.lower()
    for pattern, canonical in KNOWN_PRODUCT_ALIASES:
        if re.search(pattern, low, re.IGNORECASE):
            return canonical
    return raw


def extract_items(product_block: str) -> List[OrderItem]:
    """
    Extract order items from product block.
    Returns list of OrderItem objects.
    """
    # Strip trailing price
    block = re.sub(r"=\s*#?\s*[\d,]+(?:\.\d+)?\s*$", "", product_block, flags=re.MULTILINE)

    raw_segments = []
    for line in block.split("\n"):
        # Handle both "+" and "plus" as separators
        for part in re.split(r"\s*\+\s*|\s*plus\s*", line):
            raw_segments.append(part)

    items: List[OrderItem] = []
    seen_products = {}

    for seg in raw_segments:
        seg = _clean_segment(seg)
        if not seg or NOISE_LINE_RE.match(seg):
            continue

        # Extract quantity
        qty_m = re.match(r"^(\d+)\s*(.*)$", seg)
        if qty_m:
            quantity = int(qty_m.group(1))
            rest = qty_m.group(2).strip()
        else:
            quantity = 1
            rest = seg

        if not rest or NON_PRODUCT_RE.match(rest.strip(" .")):
            continue

        canonical = _canonical_name(rest)

        # Merge duplicates
        if canonical in seen_products:
            existing = seen_products[canonical]
            # Create new item with max quantity
            seen_products[canonical] = OrderItem(
                raw_text=rest,
                product=canonical,
                quantity=max(existing.quantity, quantity)
            )
        else:
            seen_products[canonical] = OrderItem(
                raw_text=rest,
                product=canonical,
                quantity=quantity
            )

    return list(seen_products.values())


def extract_price(text: str) -> float:
    """Extract price from text."""
    m = re.search(r"=\s*#?\s*([\d,]+(?:\.\d+)?)", text)
    if not m:
        m = re.search(r"price\s*[:\-]?\s*#?\s*([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return 0.0
    return 0.0


def parse_order_block(block: str) -> dict:
    """
    Parse an order block into structured data.
    Returns a dictionary containing CustomerInfo, items, and metadata.
    """
    lines = block.split("\n")
    
    # Check if this is a reorder format
    is_reorder_format = False
    for line in lines[:5]:
        if re.search(r'^reorder\b|^follow\s*up\s*reorders?\b', line.strip(), re.IGNORECASE):
            is_reorder_format = True
            break

    # Extract fields (honorifics are preserved)
    name = _extract_field(lines, FIELD_LABELS["name"]).strip()
    
    phone_raw = _extract_field(lines, FIELD_LABELS["phone"])
    whatsapp_raw = _extract_field(lines, FIELD_LABELS["whatsapp"])
    
    # Prefer whatsapp number if phone didn't yield a valid one
    phone = PhoneNumber.from_raw(phone_raw)
    if not phone.is_valid and whatsapp_raw:
        phone = PhoneNumber.from_raw(whatsapp_raw)

    address = _extract_field(lines, FIELD_LABELS["address"]).strip()
    delivery = _extract_field(lines, FIELD_LABELS["delivery"]).strip()

    # Extract product block
    prod_start = None
    for i, raw_line in enumerate(lines):
        if re.match(r"^\**\s*(?:" + FIELD_LABELS["product"] + r")", raw_line.strip(), re.IGNORECASE):
            prod_start = i
            break
    
    if prod_start is not None:
        prod_end = len(lines)
        for j in range(prod_start + 1, len(lines)):
            if re.match(
                r"^\**\s*(?:" + FIELD_LABELS["name"] + "|" + FIELD_LABELS["price"] + r")",
                lines[j].strip(),
                re.IGNORECASE,
            ):
                prod_end = j
                break
        product_text = "\n".join(lines[prod_start:prod_end])
    else:
        product_text = block

    # Extract items and price
    items = extract_items(product_text)
    price = extract_price(product_text)
    if not price:
        price = extract_price(block)

    # If no items found and it's reorder format, try harder
    if not items and is_reorder_format:
        items = extract_items(block)

    # Create CustomerInfo object
    customer = CustomerInfo(
        name=name,
        phone=phone,
        address=address
    )

    return {
        "customer": customer,
        "delivery": delivery,
        "items": items,
        "price": price,
        "raw_block": block  # Useful for debugging
    }


# ---------------------------------------------------------------
# Test
# ---------------------------------------------------------------
def test_parser():
    sample = """[7/6, 10:38 AM] Sales Manager Nabeu: *6th July*
*Body lotion*

Select Your Package
  1 Advanced Collagen Body Lotion + 1 Free Scar Repair Cream + Free Collagen Hand Cream + Free doorstep delivery = #28,500
Input your Full Name
  Wealth uriri
Input Phone Number
  08088214154
Input Whatsapp Number
  08088214154
Input Full Address
  Uduruwhon quarter after Robinson clinic Udu Rd ujevwo town
When do you want us to deliver to you?
  Today
Do you have any questions?
  Not at all"""

    result = parse_order_block(sample)
    
    print("=" * 60)
    print("Parsed Order")
    print("=" * 60)
    print(f"Customer: {result['customer']}")
    print(f"Delivery: {result['delivery']}")
    print(f"Price: #{result['price']}")
    print("\nItems:")
    for item in result['items']:
        print(f"  - {item}")
    
    # Test CustomerInfo properties
    customer = result['customer']
    print(f"\nCustomer Info:")
    print(f"  Has Phone: {customer.has_phone}")
    print(f"  Has Address: {customer.has_address}")
    print(f"  Phone Display: {customer.phone.display if customer.has_phone else 'N/A'}")


if __name__ == "__main__":
    test_parser()