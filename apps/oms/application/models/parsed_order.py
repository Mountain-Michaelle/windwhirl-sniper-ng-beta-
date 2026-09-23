from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class ExtractionStatus(str, Enum):
    '''
    How completely the parser was able to extract fields.
    NOT a validity indicator — only extraction completeness.

    COMPLETE: All key fields (name, phone, package, address) extracted.
    PARTIAL:  Some fields extracted, some missing.
    EMPTY:    No fields could be extracted at all.
    '''
    COMPLETE = "COMPLETE"
    PARTIAL  = "PARTIAL"
    EMPTY    = "EMPTY"


@dataclass(frozen=True)
class PackageInfo:
    '''
    Extracted package details.
    frozen=True: immutable value object.

    name:        Short package name e.g. "1 Combo set"
    description: Additional package details e.g. "(1 serum & 1 Cream)"
    price_raw:   Price as written e.g. "#29,500" or "29500"
    price_value: Numeric price if parseable, else None.
    '''
    name:        str            = ""
    description: str            = ""
    price_raw:   str            = ""
    price_value: Optional[float] = None

    @property
    def has_price(self) -> bool:
        return self.price_value is not None

    def __str__(self) -> str:
        parts = [self.name]
        if self.description:
            parts.append(self.description)
        if self.price_raw:
            parts.append(f"= {self.price_raw}")
        return " ".join(p for p in parts if p)


@dataclass
class ParsedOrder:
    '''
    A structured order extracted from a raw WhatsApp message.

    Immutable business data — no methods that modify state.
    Every Optional field may be None if not found in the message.
    The raw_text field always contains the original message in full.

    Fields:
        parsed_id:       Unique ID for this ParsedOrder instance.
        order_id:        The OMS order ID from the resolution engine.
        worker_number:   Phone number of the assigned worker.
        customer_name:   Customer full name (not validated).
        phone_number:    Customer phone (not validated).
        whatsapp_number: Customer WhatsApp (may differ from phone).
        package:         Extracted package details.
        delivery_address: Full delivery address as written.
        delivery_request: When customer wants delivery ("Tomorrow", etc.)
        customer_question: Anything after "Do you have any questions".
        campaign:        Product campaign e.g. "Tiktok Sadoer".
        order_date_raw:  Order date as written e.g. "3rd July".
        order_date:      Parsed date if successfully normalized, else None.
        raw_text:        The original WhatsApp message, never modified.
        parsed_at:       When this ParsedOrder was created.
        parser_version:  Which parser version produced this.
        status:          COMPLETE, PARTIAL, or EMPTY.
        missing_fields:  List of field names that could not be extracted.
        notes:           Extraction notes for debugging.
    '''
    parsed_id:          str                    = field(
                            default_factory=lambda: str(uuid.uuid4())[:8]
                        )
    order_id:           str                    = ""
    worker_number:      str                    = ""
    customer_name:      Optional[str]          = None
    phone_number:       Optional[str]          = None
    whatsapp_number:    Optional[str]          = None
    package:            Optional[PackageInfo]  = None
    delivery_address:   Optional[str]          = None
    delivery_request:   Optional[str]          = None
    customer_question:  Optional[str]          = None
    campaign:           Optional[str]          = None
    order_date_raw:     Optional[str]          = None
    order_date:         Optional[date]         = None
    raw_text:           str                    = ""
    parsed_at:          datetime               = field(default_factory=datetime.now)
    parser_version:     str                    = "1.0"
    status:             ExtractionStatus       = ExtractionStatus.EMPTY
    missing_fields:     list[str]              = field(default_factory=list)
    notes:              list[str]              = field(default_factory=list)

    # KEY FIELDS — used for COMPLETE vs PARTIAL determination
    KEY_FIELDS = ["customer_name", "phone_number", "package", "delivery_address"]

    def compute_status(self) -> "ParsedOrder":
        '''
        Compute and return a new ParsedOrder with updated status
        and missing_fields based on which key fields are present.

        Returns a new instance (frozen semantics even though not frozen).
        '''
        missing = [
            f for f in self.KEY_FIELDS
            if getattr(self, f) is None
        ]
        extracted = [f for f in self.KEY_FIELDS if f not in missing]

        if not extracted:
            status = ExtractionStatus.EMPTY
        elif not missing:
            status = ExtractionStatus.COMPLETE
        else:
            status = ExtractionStatus.PARTIAL

        self.missing_fields.clear()
        self.missing_fields.extend(missing)
        object.__setattr__(self, 'status', status) if hasattr(self, '__dataclass_fields__') else None
        # For non-frozen dataclass, direct assignment:
        self.status = status
        return self

    def summary(self) -> str:
        '''Short human-readable summary for logging.'''
        parts = []
        if self.customer_name:
            parts.append(f"customer={self.customer_name!r}")
        if self.phone_number:
            parts.append(f"phone={self.phone_number!r}")
        if self.package:
            parts.append(f"package={self.package.name!r}")
        if self.delivery_address:
            parts.append(f"address={self.delivery_address[:30]!r}")
        return f"ParsedOrder({', '.join(parts) or 'empty'})"

    def __repr__(self):
        return (
            f"ParsedOrder("
            f"id={self.parsed_id!r}, "
            f"status={self.status.value}, "
            f"order={self.order_id!r}, "
            f"customer={self.customer_name!r})"
        )
        

     