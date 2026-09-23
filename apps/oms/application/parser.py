from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Set, Tuple

from apps.oms.domain.entities import Order, OrderStatus
from apps.oms.domain.value_objects import CustomerInfo, OrderItem, PhoneNumber
from apps.oms.domain.interfaces import IParser
from apps.oms.infrastructure.browser.raw_message import RawMessage
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class OrderParser(IParser):
    '''
    Extracts structured order information from informal WhatsApp messages.
    Implements the IParser domain interface.

    Handles real Nabeau Store order message formats:
      Structured:   "Customer: Blessing Okafor\nPhone: 08031234567\n..."
      Semi-struct:  "blessing adeyemi - 2 sadoer sets - ikeja"
      Informal:     "pls add order for Emeka, wants collagen set"
      Mixed:        "New order\nName: Mrs Fatima\nQty: 1 Sadoer\n..."
      Reorder:      "Reorder \n6 July \nProduct \n  1 collagen face cream..."
      Multi-item:   "1 Product A + 1 Product B + Free Product C = #28,500"

    Usage:
        parser = OrderParser(staff_number="2348XXXXXXXXX")
        order  = parser.parse(message, staff_number)
        if order is None:
            # Message does not contain enough info to make an order
    '''

    # ── Product vocabulary ──────────────────────────────────────
    # Maps any recognized variant to the canonical product name.
    # This is for ENRICHMENT only - unknown products are still captured.
    # Add new products here as they become known, but the parser
    # will work even without them.
    PRODUCT_MAP = {
        # Sadoer Collagen Combo Set variants
        r"combo\s*set.*?serum.*?cream|serum\s*&\s*1?\s*cream|c[uo]mbo\s*set": 
            "Sadoer Collagen Combo Set (Serum + Cream)",
        r"sadoer\s*(collagen\s*)?(combo\s*)?(set|pack|bundle)": 
            "Sadoer Collagen Combo Set",
        r"sad[oe]or\s*collagen\s*face\s*cream|collagen\s*face\s*cream\s*24k": 
            "Sadoer Collagen Face Cream",
        r"collagen\s*(?:face\s*cream\s*and\s*)?serum": 
            "Sadoer Collagen Serum",
        r"advanced?\s*collagen\s*body(?:\s*lotion)?": 
            "Advanced Collagen Body Lotion",
        r"scar\s*repair\s*cream": 
            "Scar Repair Cream",
        r"collagen\s*hand\s*cream": 
            "Collagen Hand Cream",
        r"collagen\s*body\s*lotion": 
            "Collagen Body Lotion",
        r"vitamin\s*c\s*serum": 
            "Vitamin C Serum",  # Example of future product
    }

    # ── Field label patterns ────────────────────────────────────
    # These match labelled fields like "Name: Blessing" or "Phone: 080..."
    # More comprehensive than the original version
    LABEL_PATTERNS = {
        "name": [
            r"(?:input\s*(?:your\s+)?full\s*name|^name\b|customer\s*name|client\s*name)\s*[:\-]?\s*(.+?)(?:\n|$|,|\|)",
            r"(?:customer|name|client|buyer)\s*[:\-]\s*(.+?)(?:\n|$|,|\|)",
            r"(?:for|to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b",
        ],
        "phone": [
            r"(?:input\s*(?:your\s+)?phone\s*number|^phone(?:\s*number)?\b|^tel\b)\s*[:\-]?\s*([+\d\s\-]{10,15})",
            r"(?:phone|number|tel|call|contact|whatsapp)\s*[:\-]?\s*([+\d\s\-]{10,15})",
            r"\b((?:0|\+?234)[789][01]\d{8})\b",  # Nigerian phone anywhere
        ],
        "whatsapp": [
            r"(?:input\s*(?:your\s+)?whatsapp\s*number|^whatsapp(?:\s*number)?\b)\s*[:\-]?\s*([+\d\s\-]{10,15})",
        ],
        "address": [
            r"(?:input\s*(?:your\s+)?full\s*address|^address\b)\s*[:\-]?\s*(.+?)(?:\n(?=\s*[A-Za-z])|$)",
            r"(?:address|location|delivery|deliver\s+to|area)\s*[:\-]\s*(.+?)(?:\n|$)",
            r"(?:no\.?\s*\d+[,\s]+\w+)",  # Street number pattern
        ],
        "delivery": [
            r"when\s+do\s+you\s+want\s+us\s+to\s+deliver\s+to\s+you\s*[:\-]?\s*(.+?)(?:\n|$)",
        ],
        "product": [
            r"select\s+your\s+(?:preferred\s+)?package(?:\s+below)?\s*[:\-]?\s*(.*?)(?=\s*(?:input|name|phone|address|when|do you|$))",
            r"^product\s*[:\-]?\s*(.*?)(?=\s*(?:name|phone|address|$))",
            r"reorder\s*(?:.*?)(?=\s*(?:product|name|phone|address|$))",
            r"follow\s*up\s*reorders?\s*(?:.*?)(?=\s*(?:product|name|phone|address|$))",
        ],
        "price": [
            r"=\s*#?\s*([\d,]+(?:\.\d+)?)",
            r"price\s*[:\-]?\s*#?\s*([\d,]+(?:\.\d+)?)",
        ],
        "quantity": [
            r"(?:qty|quantity|pieces?|units?|sets?|packs?)\s*[:\-]?\s*(\d+)",
            r"(\d+)\s*(?:piece|unit|set|pack|bottle)s?",
            r"\bx\s*(\d+)\b",
            r"\b(\d+)\s*x\b",
        ],
    }

    # ── Honorifics (PRESERVED, not removed) ──────────────────
    # We keep these for reference but DO NOT remove them
    HONORIFICS_PATTERN = re.compile(
        r"^(mr\.?|mrs\.?|miss\.?|ms\.?|dr\.?|prof\.?|engr\.?|"
        r"alhaji\.?|alhaja\.?|chief\.?|barr\.?|pastor\.?)\s+",
        re.IGNORECASE
    )

    # ── Non-product keywords to filter out ────────────────────
    NON_PRODUCT_RE = re.compile(
        r"^(?:free\s+)?(?:door\s*step\s*)?delivery$|^(?:free\s+)?shipping$",
        re.IGNORECASE,
    )

    NOISE_LINE_RE = re.compile(
        r"^select\s+your\s+(?:preferred\s+)?package(?:\s+below)?\s*:?$|"
        r"^product\s*:?$|"
        r"^reorder\s*:?$|"
        r"^follow\s*up\s*reorders?\s*:?$",
        re.IGNORECASE,
    )

    def __init__(self, staff_number: str = ""):
        self._staff_number = staff_number
        self._product_patterns = {re.compile(p, re.IGNORECASE): n for p, n in self.PRODUCT_MAP.items()}

    def looks_like_order(self, message: RawMessage) -> bool:
        '''
        Quick pre-check: does this message likely contain an order?
        Returns True if at least one product keyword or order intent is found.
        '''
        text = message.raw_text.lower()
        
        # Check for product keywords
        for pattern in self.PRODUCT_MAP:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        
        # Check for reorder keywords
        if re.search(r'\b(reorder|follow\s*up\s*reorders?)\b', text, re.IGNORECASE):
            return True
        
        # Check for order intent
        order_intent = re.search(
            r"\b(order|customer|delivery|purchase|buy|package)\b",
            text,
            re.IGNORECASE
        )
        return bool(order_intent)

    def parse(
        self,
        message: RawMessage,
        staff_number: str
    ) -> Optional[Order]:
        '''
        Parse a RawMessage into an Order domain entity.

        Returns Order if parsing extracted enough to make an order.
        Returns None if the message does not contain order content.

        Does NOT raise — returns None on any failure.
        '''
        text = message.raw_text.strip()

        if not text:
            return None

        try:
            # Extract all fields
            customer_name = self._extract_name(text)
            phone_raw = self._extract_phone(text)
            whatsapp_raw = self._extract_whatsapp(text)
            address = self._extract_address(text)
            delivery = self._extract_delivery(text)
            
            # Extract items (multiple products per message)
            items = self._extract_items(text)
            
            # If no items found, try reorder format
            if not items and self._is_reorder_format(text):
                items = self._extract_items_from_reorder(text)
            
            # If still no items, try one more fallback
            if not items:
                items = self._extract_items_fallback(text)

            # Extract price
            price = self._extract_price(text)

            # Minimum viability: need at least a name OR a phone, AND at least one item
            if not customer_name and not phone_raw:
                log.debug(f"Parser: no customer name or phone found in: {text[:60]!r}")
                return None

            if not items:
                log.debug(f"Parser: no items found in: {text[:60]!r}")
                return None

            # Prefer whatsapp number if phone didn't yield a valid one
            phone = PhoneNumber.from_raw(phone_raw) if phone_raw else PhoneNumber("", False)
            if not phone.is_valid and whatsapp_raw:
                phone = PhoneNumber.from_raw(whatsapp_raw)

            customer = CustomerInfo(
                name=customer_name,
                phone=phone,
                address=address,
            )

            # Generate a deterministic order ID from message fingerprint
            order_id = f"ORD-{message.fingerprint[:8].upper()}"

            order = Order(
                order_id=order_id,
                staff_number=staff_number,
                customer_name=customer_name or str(phone),
                customer=customer,
                items=items,
                raw_text=message.raw_text,
                source_message=None,  # Will be set by pipeline
                status=OrderStatus.DETECTED,
                detected_at=datetime.now(),
            )

            log.debug(
                f"Parser: extracted order {order_id}\n"
                f"  Customer: {customer}\n"
                f"  Items: {order.item_summary()}\n"
                f"  Price: #{price if price else 'N/A'}"
            )

            return order

        except Exception as e:
            log.warning(
                f"Parser error for message {message.fingerprint[:8]!r}: {e}",
                exc_info=True
            )
            return None

    # ── Private extraction methods ──────────────────────────────

    def _is_reorder_format(self, text: str) -> bool:
        """Check if text is in reorder format"""
        return bool(re.search(r'^reorder\b|^follow\s*up\s*reorders?\b', text, re.IGNORECASE))

    def _extract_name(self, text: str) -> str:
        '''
        Extract customer name from message text.
        PRESERVES honorifics - does NOT remove them.
        '''
        # Try labelled field patterns first
        for pattern in self.LABEL_PATTERNS["name"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip(" .,;:")
                # HONORIFICS ARE PRESERVED - no removal
                if 2 <= len(name) <= 50:
                    return name.title()

        # Fallback: first line that looks like a name (2+ words, no numbers)
        for line in text.split("\n"):
            line = line.strip()
            if (
                2 <= len(line.split()) <= 4
                and not re.search(r"\d", line)
                and not re.search(r"order|customer|item|phone|address", line, re.I)
            ):
                # HONORIFICS ARE PRESERVED - no removal
                if line:
                    return line.title()

        return ""

    def _extract_phone(self, text: str) -> str:
        '''
        Extract the first recognizable Nigerian phone number.
        Handles formats with commas (e.g., "8,030,576,262")
        '''
        # Remove commas from numbers before processing
        cleaned_text = re.sub(r'(\d),(\d)', r'\1\2', text)
        
        # Labelled field first
        for pattern in self.LABEL_PATTERNS["phone"]:
            match = re.search(pattern, cleaned_text, re.IGNORECASE)
            if match:
                raw = re.sub(r"[^\d+]", "", match.group(1))
                if len(raw) >= 10:
                    return raw

        # Scan for Nigerian phone pattern anywhere in text
        phone_match = re.search(
            r"\b((?:\+?234|0)[789][01]\d{8})\b",
            cleaned_text
        )
        if phone_match:
            return phone_match.group(1)

        return ""

    def _extract_whatsapp(self, text: str) -> str:
        '''Extract whatsapp number specifically'''
        for pattern in self.LABEL_PATTERNS["whatsapp"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                raw = re.sub(r"[^\d+]", "", match.group(1))
                if len(raw) >= 10:
                    return raw
        return ""

    def _extract_address(self, text: str) -> str:
        '''
        Extract delivery address from message text.
        Captures full detailed addresses, not just city names.
        '''
        # Labelled field
        for pattern in self.LABEL_PATTERNS["address"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                addr = match.group(1) if match.lastindex is not None else match.group(0)
                addr = addr.strip(" .,;:")
                if len(addr) >= 4:
                    return addr.title()

        # Try to capture everything after "Address" or "Location"
        addr_match = re.search(
            r"(?:address|location|delivery|deliver\s+to)\s*[:\-]?\s*(.+?)(?=\s*(?:input|name|phone|when|do you|$))",
            text,
            re.IGNORECASE | re.DOTALL
        )
        if addr_match:
            addr = addr_match.group(1).strip()
            if len(addr) >= 4:
                return addr.title()

        # Known Nigerian city/area names as fallback
        city_pattern = (
            r"\b(lagos(\s+island|\s+mainland|\s+state)?|"
            r"abuja|port\s*harcourt|ph|ibadan|kano|"
            r"enugu|benin(\s+city)?|aba|onitsha|"
            r"ikeja|lekki|victoria\s+island|vi|"
            r"surulere|yaba|ikorodu|festac|"
            r"ajah|sangotedo|ibeju)\b"
        )
        city_match = re.search(city_pattern, text, re.IGNORECASE)
        if city_match:
            return city_match.group(0).title()

        return ""

    def _extract_delivery(self, text: str) -> str:
        '''Extract delivery timeframe'''
        for pattern in self.LABEL_PATTERNS["delivery"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                delivery = match.group(1).strip(" .,;:")
                if delivery:
                    return delivery
        return ""

    def _extract_items(self, text: str) -> List[OrderItem]:
        '''
        Extract ALL product mentions from the text.
        Handles formats like: "1 Product A + 1 Product B + Free Product C"
        Returns a list of OrderItem value objects.
        
        This is the primary extraction method that handles most formats.
        '''
        items = []
        
        # Strategy 1: Split by '+' and newlines to find all products
        # First, try to find product block
        product_block = text
        for pattern in self.LABEL_PATTERNS["product"]:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                product_block = match.group(1) if match.lastindex is not None else match.group(0)
                break
        
        # Split into segments
        segments = self._split_into_segments(product_block)
        
        for seg in segments:
            seg = seg.strip()
            if not seg or self.NOISE_LINE_RE.match(seg):
                continue
            
            # Check if this segment contains a product
            product_match = self._match_product(seg)
            if not product_match:
                continue
            
            # Extract quantity
            quantity = self._extract_quantity_from_segment(seg)
            
            # Get canonical product name or use the original text
            product_text = product_match.group(0).strip()
            canonical = self._get_canonical_product_name(product_text)
            
            # Check if it's a non-product (delivery, shipping, etc.)
            if self.NON_PRODUCT_RE.match(product_text):
                continue
            
            # Create item with original text preserved
            items.append(OrderItem(
                raw_text=product_text,
                product=canonical if canonical else product_text,
                quantity=quantity,
            ))
        
        # Deduplicate by canonical product name (merge quantities)
        return self._deduplicate_items(items)

    def _extract_items_from_reorder(self, text: str) -> List[OrderItem]:
        '''
        Special handling for reorder format:
        Reorder 
        6 July 
        Product 
          1 collagen face cream and serum 
        1 advance collagen body lotion 
        1 free repair cream 
        1 free hand cream=#47,500
        '''
        # Extract everything after "Product" or "Product:"
        product_match = re.search(
            r"product\s*[:\-]?\s*(.*?)(?=\s*(?:name|phone|address|$))",
            text,
            re.IGNORECASE | re.DOTALL
        )
        if not product_match:
            return []
        
        product_block = product_match.group(1)
        return self._extract_items(product_block)

    def _extract_items_fallback(self, text: str) -> List[OrderItem]:
        '''
        Final fallback: scan the entire text for product patterns.
        '''
        items = []
        
        # Check for known products anywhere in text
        for pattern, canonical in self.PRODUCT_MAP.items():
            for match in re.finditer(pattern, text, re.IGNORECASE):
                product_text = match.group(0)
                
                # Skip if it's a non-product
                if self.NON_PRODUCT_RE.match(product_text):
                    continue
                
                # Extract quantity from context
                quantity = 1
                # Look for quantity in the surrounding text
                start = max(0, match.start() - 20)
                end = min(len(text), match.end() + 20)
                window = text[start:end]
                
                qty_match = re.search(r'(\d+)\s*(?:x|piece|unit|set|pack)?', window)
                if qty_match:
                    try:
                        quantity = int(qty_match.group(1))
                    except ValueError:
                        pass
                
                items.append(OrderItem(
                    raw_text=product_text,
                    product=canonical,
                    quantity=quantity,
                ))
        
        return self._deduplicate_items(items)

    def _split_into_segments(self, text: str) -> List[str]:
        '''Split text into product segments by '+' and newlines'''
        segments = []
        # Split by '+' and newlines
        for part in re.split(r'\s*\+\s*|\n', text):
            # Split by "plus" as well
            for subpart in re.split(r'\s*plus\s*', part):
                segments.append(subpart)
        return segments

    def _match_product(self, segment: str) -> Optional[re.Match]:
        '''Check if segment contains a product'''
        for pattern in self._product_patterns.keys():
            match = pattern.search(segment)
            if match:
                return match
        return None

    def _extract_quantity_from_segment(self, segment: str) -> int:
        '''Extract quantity from a product segment'''
        # Check for explicit quantity patterns
        for pattern in self.LABEL_PATTERNS["quantity"]:
            match = re.search(pattern, segment, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1))
                except (ValueError, IndexError):
                    pass
        
        # Check for number at start of segment
        qty_match = re.match(r'^(\d+)\s*(.*)$', segment)
        if qty_match:
            try:
                return int(qty_match.group(1))
            except ValueError:
                pass
        
        return 1

    def _get_canonical_product_name(self, product_text: str) -> str:
        '''
        Get canonical product name if known, otherwise return empty string.
        Unknown products will use their raw text instead.
        '''
        for pattern, canonical in self.PRODUCT_MAP.items():
            if re.search(pattern, product_text, re.IGNORECASE):
                return canonical
        return ""  # Unknown product - will use raw text

    def _deduplicate_items(self, items: List[OrderItem]) -> List[OrderItem]:
        '''
        Deduplicate items by product name, merging quantities.
        If same product appears multiple times, take the maximum quantity.
        '''
        seen: Dict[str, OrderItem] = {}
        
        for item in items:
            key = item.product
            if key in seen:
                # Merge: take max quantity
                existing = seen[key]
                seen[key] = OrderItem(
                    raw_text=existing.raw_text,
                    product=existing.product,
                    quantity=max(existing.quantity, item.quantity),
                    unit_price=max(existing.unit_price, item.unit_price),
                )
            else:
                seen[key] = item
        
        return list(seen.values())

    def _extract_price(self, text: str) -> float:
        '''
        Extract price from text.
        Handles formats: "= #28,500", "Price: 47,500", "#28,500"
        '''
        for pattern in self.LABEL_PATTERNS["price"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    price_str = match.group(1).replace(',', '').strip()
                    return float(price_str)
                except (ValueError, IndexError):
                    pass
        return 0.0