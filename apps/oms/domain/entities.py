from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from apps.oms.domain.value_objects import CustomerInfo, OrderItem, PhoneNumber


class OrderStatus(str, Enum):
    '''
    The complete lifecycle of an order in the OMS.

    DETECTED     → Message was found in the group and parsed as an order.
                   Not yet confirmed or assigned.

    CONFIRMED    → Order details verified. Staff has acknowledged it.

    IN_PROGRESS  → Staff is actively handling this order.

    DISPATCHED   → Order has been sent out for delivery.

    DELIVERED    → Order successfully delivered to customer.

    CANCELLED    → Order was cancelled (customer or staff decision).

    FAILED       → Order could not be fulfilled (out of stock, no contact, etc.)

    Using (str, Enum) means the value stores as a plain string in the database
    and in logs — no special serialization needed.
    '''
    DETECTED    = "DETECTED"
    CONFIRMED   = "CONFIRMED"
    IN_PROGRESS = "IN_PROGRESS"
    DISPATCHED  = "DISPATCHED"
    DELIVERED   = "DELIVERED"
    CANCELLED   = "CANCELLED"
    FAILED      = "FAILED"

    def can_transition_to(self, new_status: "OrderStatus") -> bool:
        '''
        Business rule: which status transitions are valid.
        Prevents an order jumping from DETECTED directly to DELIVERED
        without going through the proper steps.

        Valid transitions:
            DETECTED    → CONFIRMED, CANCELLED
            CONFIRMED   → IN_PROGRESS, CANCELLED
            IN_PROGRESS → DISPATCHED, CANCELLED, FAILED
            DISPATCHED  → DELIVERED, FAILED
            DELIVERED   → (terminal — no further transitions)
            CANCELLED   → (terminal — no further transitions)
            FAILED      → CONFIRMED (retry after investigation)
        '''
        valid = {
            OrderStatus.DETECTED:    {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
            OrderStatus.CONFIRMED:   {OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED},
            OrderStatus.IN_PROGRESS: {OrderStatus.DISPATCHED, OrderStatus.CANCELLED, OrderStatus.FAILED},
            OrderStatus.DISPATCHED:  {OrderStatus.DELIVERED, OrderStatus.FAILED},
            OrderStatus.DELIVERED:   set(),
            OrderStatus.CANCELLED:   set(),
            OrderStatus.FAILED:      {OrderStatus.CONFIRMED},
        }
        return new_status in valid.get(self, set())


@dataclass
class RawMessage:
    '''
    A raw WhatsApp message as extracted from the group.
    This is NOT yet an order — it is the raw input before parsing.

    The parser will attempt to convert a RawMessage into an Order.
    If parsing fails, the message is logged and skipped.

    sender_number: WhatsApp number of who sent the message.
                   Format: 234XXXXXXXXXX (13 digits, no +)
    group_name:    The WhatsApp group this message came from.
    text:          The raw text content of the message.
    timestamp:     When the message was sent (from WhatsApp Web UI).
    message_id:    WhatsApp's internal message identifier (if accessible).
                   Used for deduplication.
    '''
    sender_number: str
    group_name:    str
    text:          str
    timestamp:     datetime
    message_id:    str = ""

    def is_from_staff(self, staff_number: str) -> bool:
        '''True if this message was sent by the configured staff member.'''
        # Normalise both numbers for comparison (strip + and spaces)
        clean_sender = self.sender_number.replace("+", "").replace(" ", "")
        clean_staff  = staff_number.replace("+", "").replace(" ", "")
        return clean_sender == clean_staff

    def preview(self, max_chars: int = 60) -> str:
        '''Short preview of message text for logging.'''
        if len(self.text) <= max_chars:
            return self.text
        return self.text[:max_chars] + "..."


@dataclass
class Order:
    '''
    A confirmed order detected from a WhatsApp group message.
    Created by the Parser when it successfully interprets a RawMessage.

    This is the central entity of the OMS — everything else
    (storage, sheets, notifications) revolves around Order objects.

    order_id:       Unique identifier. Generated from message content
                    or assigned by the database on insert.
    staff_number:   The staff member this order is assigned to.
    customer_name:  Customer name from the message (may be partial).
    customer_phone: Customer WhatsApp number (may be empty if not in message).
    items:          List of ordered items as parsed from the message.
                    Each item is a string description e.g. "2x Sadoer Combo Set"
    raw_text:       The original message text this order was parsed from.
                    Kept for audit trail and re-parsing if needed.
    source_message: The RawMessage this order was created from.
    status:         Current order lifecycle state.
    detected_at:    When the order was first detected.
    updated_at:     When the order status last changed.
    notes:          Optional operator notes about this order.
    '''
    order_id:       str
    staff_number:   str
    customer_name:  str
    raw_text:       str
    source_message: RawMessage
    status:         OrderStatus        = OrderStatus.DETECTED
    customer_phone: str                = ""
    detected_at:    datetime           = field(default_factory=datetime.now)
    updated_at:     datetime           = field(default_factory=datetime.now)
    notes:          str                = ""
    customer:       CustomerInfo       = field(default_factory=CustomerInfo)
    items:          list[OrderItem]    = field(default_factory=list)
    total_price:    float              = 0.0
    delivery_address: str             = ""
    payment_method:   str             = ""
    
    
    def transition_to(self, new_status: OrderStatus) -> None:
        '''
        Move this order to a new status.
        Enforces valid transitions — raises ValueError if the
        transition is not allowed by business rules.

        Args:
            new_status: The status to transition to.

        Raises:
            ValueError: If the transition is not valid.
        '''
        if not self.status.can_transition_to(new_status):
            raise ValueError(
                f"Cannot transition order {self.order_id} "
                f"from {self.status.value} to {new_status.value}. "
                f"This transition is not allowed by business rules."
            )
        self.status     = new_status
        self.updated_at = datetime.now()

    def is_terminal(self) -> bool:
        '''
        True if this order is in a terminal state (no further changes).
        Terminal states: DELIVERED, CANCELLED.
        '''
        return self.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED)

    def item_summary(self) -> str:
        '''Human-readable summary of ordered items.'''
        if not self.items:
            return "(no items parsed)"
        return ", ".join(self.items)

    def __repr__(self):
        return (
            f"Order(id={self.order_id!r}, "
            f"customer={self.customer_name!r}, "
            f"status={self.status.value}, "
            f"items={len(self.items)})"
        )
    def item_summary(self) -> str:
            '''Human-readable summary of ordered items for logging.'''
            if not self.items:
                return "(no items parsed)"
            return ", ".join(str(item) for item in self.items)

    @property
    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)
        

@dataclass
class Staff:
    '''
    Represents the staff member whose orders this OMS instance tracks.
    One OMS instance = one staff member = one WhatsApp group.

    number:      WhatsApp number. Format: 234XXXXXXXXXX
    display_name: Name as it appears in WhatsApp (optional).
    group_name:  The WhatsApp group this staff member operates in.
    '''
    number:       str
    group_name:   str
    display_name: str = ""

    def __repr__(self):
        name = f" ({self.display_name})" if self.display_name else ""
        return f"Staff(+{self.number}{name}, group={self.group_name!r})"