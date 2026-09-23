from __future__ import annotations

import re
from typing import Optional

from apps.oms.application.models.parsed_order import ParsedOrder, ExtractionStatus
from apps.oms.application.field_extractors import (
    PackageExtractor,
    CustomerExtractor,
    PhoneExtractor,
    WhatsAppExtractor,
    AddressExtractor,
    DeliveryExtractor,
    CampaignExtractor,
    QuestionExtractor,
    DateExtractor,
)
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)

# All known label prefixes (normalized) for section detection
# Order matters — longer/more specific labels must come first
# to prevent "phone" matching before "phone number"
ALL_LABELS = [
    "select your package",
    "select package",
    "input your full name",
    "input full name",
    "input phone number",
    "input your whatsapp number",
    "input whatsapp number",
    "input full address",
    "input your full address",
    "input address",
    "when do you want us to deliver",
    "when do you want delivery",
    "do you have any questions",
    "any questions",
    "full name",
    "customer name",
    "phone number",
    "phone no",
    "whatsapp number",
    "whatsapp no",
    "delivery address",
    "full address",
    "delivery date",
    "delivery time",
    "preferred delivery",
    "customer question",
    "additional notes",
    "additional note",
    "order date",
    "ordered on",
    "package",
    "product",
    "item",
    "customer",
    "phone",
    "whatsapp",
    "address",
    "location",
    "delivery",
    "questions",
    "question",
    "remarks",
    "notes",
    "note",
    "date",
    "name",
    "mobile",
    "tel",
    "contact",
    "wa",
]


def normalize_label(text: str) -> str:
    '''Normalize a label for comparison. Never use on values.'''
    return re.sub(r'\s+', ' ', text.lower().strip(' :*-_')) 


class OrderParser:
    '''
    Converts a raw WhatsApp order message into a structured ParsedOrder.

    Coordinates 9 independent field extractors. Each extractor
    receives the full text and the pre-parsed sections dict.
    Failures in one extractor never affect others.

    Usage:
        parser = OrderParser()
        parsed = await parser.parse(
            raw_text="*Tiktok Sadoer*\\nSelect Your Package: ...",
            order_id="ORD-001",
            worker_number="2348XXXXXXXXX",
        )
    '''

    PARSER_VERSION = "1.0"

    def __init__(self):
        self._package   = PackageExtractor()
        self._customer  = CustomerExtractor()
        self._phone     = PhoneExtractor()
        self._whatsapp  = WhatsAppExtractor()
        self._address   = AddressExtractor()
        self._delivery  = DeliveryExtractor()
        self._campaign  = CampaignExtractor()
        self._question  = QuestionExtractor()
        self._date      = DateExtractor()

    async def parse(
        self,
        raw_text:     str,
        order_id:     str = "",
        worker_number: str = "",
    ) -> ParsedOrder:
        '''
        Parse a raw WhatsApp order message into a ParsedOrder.

        Args:
            raw_text:      The complete raw WhatsApp message text.
            order_id:      OMS order ID from resolution engine.
            worker_number: Assigned worker phone number.

        Returns:
            ParsedOrder with all extractable fields populated.
            Never raises — all extractor errors are caught internally.
        '''
        log.debug(
            f"OrderParser: parsing order {order_id!r} "
            f"({len(raw_text)} chars)"
        )

        parsed = ParsedOrder(
            order_id      =order_id,
            worker_number =worker_number,
            raw_text      =raw_text,
            parser_version=self.PARSER_VERSION,
        )

        notes = []

        # ── Step 1: Section the message by labels ─────────────────
        sections = self._section_message(raw_text)

        log.debug(
            f"OrderParser: found {len(sections)} section(s): "
            f"{list(sections.keys())}"
        )

        # ── Step 2: Run each extractor independently ──────────────
        # Each extractor is wrapped in try/except.
        # One failure NEVER stops the others.

        # Campaign (reads full text, not sections)
        try:
            parsed.campaign = self._campaign.extract(raw_text, sections)
            if parsed.campaign:
                log.debug(f"Campaign: {parsed.campaign!r}")
        except Exception as e:
            notes.append(f"campaign_extractor error: {e}")
            log.warning(f"OrderParser: campaign extractor failed: {e}")

        # Customer name
        try:
            parsed.customer_name = self._customer.extract(raw_text, sections)
            if parsed.customer_name:
                log.debug(f"Customer: {parsed.customer_name!r}")
            else:
                notes.append("customer_name: not found")
        except Exception as e:
            notes.append(f"customer_extractor error: {e}")
            log.warning(f"OrderParser: customer extractor failed: {e}")

        # Phone number
        try:
            parsed.phone_number = self._phone.extract(raw_text, sections)
            if parsed.phone_number:
                log.debug(f"Phone: {parsed.phone_number!r}")
            else:
                notes.append("phone_number: not found")
        except Exception as e:
            notes.append(f"phone_extractor error: {e}")
            log.warning(f"OrderParser: phone extractor failed: {e}")

        # WhatsApp number
        try:
            parsed.whatsapp_number = self._whatsapp.extract(raw_text, sections)
            if parsed.whatsapp_number:
                log.debug(f"WhatsApp: {parsed.whatsapp_number!r}")
        except Exception as e:
            notes.append(f"whatsapp_extractor error: {e}")
            log.warning(f"OrderParser: whatsapp extractor failed: {e}")

        # Package
        try:
            parsed.package = self._package.extract(raw_text, sections)
            if parsed.package:
                log.debug(
                    f"Package: {parsed.package.name!r} "
                    f"price={parsed.package.price_value}"
                )
            else:
                notes.append("package: not found")
        except Exception as e:
            notes.append(f"package_extractor error: {e}")
            log.warning(f"OrderParser: package extractor failed: {e}")

        # Delivery address
        try:
            parsed.delivery_address = self._address.extract(raw_text, sections)
            if parsed.delivery_address:
                log.debug(f"Address: {parsed.delivery_address[:40]!r}")
            else:
                notes.append("delivery_address: not found")
        except Exception as e:
            notes.append(f"address_extractor error: {e}")
            log.warning(f"OrderParser: address extractor failed: {e}")

        # Delivery request
        try:
            parsed.delivery_request = self._delivery.extract(raw_text, sections)
            if parsed.delivery_request:
                log.debug(f"Delivery: {parsed.delivery_request!r}")
        except Exception as e:
            notes.append(f"delivery_extractor error: {e}")
            log.warning(f"OrderParser: delivery extractor failed: {e}")

        # Customer question
        try:
            parsed.customer_question = self._question.extract(raw_text, sections)
            if parsed.customer_question:
                log.debug(f"Question: {parsed.customer_question!r}")
        except Exception as e:
            notes.append(f"question_extractor error: {e}")
            log.warning(f"OrderParser: question extractor failed: {e}")

        # Order date
        try:
            raw_date, parsed_date = self._date.extract(raw_text, sections)
            parsed.order_date_raw = raw_date
            parsed.order_date     = parsed_date
            if raw_date:
                log.debug(f"Date: {raw_date!r} → {parsed_date}")
        except Exception as e:
            notes.append(f"date_extractor error: {e}")
            log.warning(f"OrderParser: date extractor failed: {e}")

        # ── Step 3: Compute status ────────────────────────────────
        parsed.notes.extend(notes)
        parsed.compute_status()

        # ── Step 4: Emit event ────────────────────────────────────
        event_map = {
            ExtractionStatus.COMPLETE: "order.parsed",
            ExtractionStatus.PARTIAL:  "order.partially_parsed",
            ExtractionStatus.EMPTY:    "order.empty_parsed",
        }
        event_name = event_map.get(parsed.status, "order.parsed")

        await dispatcher.emit(
            event_name,
            parsed_order  =parsed,
            order_id      =order_id,
            status        =parsed.status.value,
            missing_fields=parsed.missing_fields,
        )

        log.info(
            f"OrderParser: {parsed.status.value} | "
            f"{parsed.summary()} | "
            f"missing={parsed.missing_fields}"
        )

        return parsed

    def _section_message(self, text: str) -> dict[str, str]:
        '''
        Split a raw message into sections by recognized labels.

        Algorithm:
          1. Process each line
          2. Check if line starts with a known label (after normalization)
          3. If yes: start a new current_label section
          4. If no: append line to the current section's value
          5. Handles both "Label: value" (same line) and
             "Label:\\nvalue" (value on next line) formats

        Returns:
            dict mapping normalized_label → raw_value_string
        '''
        sections:      dict[str, str] = {}
        current_label: Optional[str]  = None
        current_lines: list[str]      = []

        def flush():
            if current_label is not None:
                sections[current_label] = "\n".join(current_lines).strip()

        for line in text.splitlines():
            stripped   = line.strip()
            normalized = normalize_label(stripped)

            matched_label = None
            inline_value  = ""

            # Check all known labels against this line
            for label in ALL_LABELS:
                if normalized.startswith(label):
                    matched_label = label
                    # Extract any inline value after the label
                    rest = stripped[len(label):].strip(" :\t")
                    # Remove leading colon/asterisk
                    rest = re.sub(r'^[:\-\s]+', '', rest).strip()
                    inline_value = rest
                    break

            if matched_label:
                # Save the previous section
                flush()
                current_label = matched_label
                current_lines = [inline_value] if inline_value else []
            elif current_label is not None:
                # Continuation of current section
                current_lines.append(line)
            # Lines before any label are ignored (handled by campaign extractor)

        flush()  # Save the last section
        return sections
