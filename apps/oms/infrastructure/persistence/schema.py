from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, create_engine, func, Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    '''SQLAlchemy declarative base. All ORM models inherit from this.'''
    pass


class OrderRecord(Base):
    '''
    Persisted form of a ValidatedOrder.

    One row per order. Created when "order.validated" event fires.
    Updated when assignment is resolved or duplicate detected.

    Primary key: order_id (OMS-assigned, not auto-increment).
    '''
    __tablename__ = "orders"

    # Identity
    order_id        = Column(String,  primary_key=True)
    parsed_id       = Column(String,  nullable=True)
    worker_number   = Column(String,  nullable=True)   # Set after assignment

    # Customer fields (as extracted — never normalized)
    customer_name   = Column(String,  nullable=True)
    phone_number    = Column(String,  nullable=True)
    whatsapp_number = Column(String,  nullable=True)

    # Package
    package_name    = Column(String,  nullable=True)
    package_desc    = Column(String,  nullable=True)
    price_raw       = Column(String,  nullable=True)
    price_value     = Column(Float,   nullable=True)

    # Delivery
    delivery_address  = Column(Text,   nullable=True)
    delivery_request  = Column(String, nullable=True)
    order_date_raw    = Column(String, nullable=True)

    # Campaign / context
    campaign          = Column(String, nullable=True)
    customer_question = Column(Text,   nullable=True)

    # Validation summary
    is_valid          = Column(Boolean, default=True,  nullable=False)
    quality_score     = Column(Float,   default=0.0,   nullable=False)
    validation_flags  = Column(String,  nullable=True)   # CSV of flag names
    validation_errors = Column(Text,    nullable=True)   # JSON of error codes
    missing_fields    = Column(String,  nullable=True)   # CSV

    # Status
    assignment_status  = Column(String, default="PENDING",  nullable=False)
    duplicate_status   = Column(String, default="UNIQUE",   nullable=False)
    duplicate_group_id = Column(String, nullable=True)

    # Raw message preserved
    raw_text           = Column(Text,   nullable=True)

    # Timestamps
    detected_at        = Column(DateTime, nullable=True)
    validated_at       = Column(DateTime, nullable=True)
    assigned_at        = Column(DateTime, nullable=True)
    created_at         = Column(DateTime, default=func.now(), nullable=False)
    updated_at         = Column(DateTime, onupdate=func.now())
     # Google Sheets sync fields (added Day 10.5)
    google_row_id   = Column(Integer, nullable=True)   # Row number in sheet
    sync_status     = Column(String,  default="PENDING", nullable=False)
    last_sync_time  = Column(DateTime, nullable=True)
    last_sync_error = Column(Text,    nullable=True)
     # Day 10.6 — sheet sync-back safety fields
    row_key      = Column(String,  unique=True, nullable=True, index=True)
    is_archived  = Column(Boolean, default=False, nullable=False)
    archived_at  = Column(DateTime, nullable=True)

    # Day 10.6.1 — worker-editable fields, synced sheet ↔ DB
    sniper_action = Column(String, nullable=True)   # must be one of ALLOWED_SNIPER_ACTIONS or null
    comments      = Column(Text,   nullable=True)   # free text, no validation

    # Relationships
    assignments = relationship("AssignmentRecord", back_populates="order",
                               cascade="all, delete-orphan")
    duplicate_memberships = relationship("DuplicateMemberRecord",
                                         back_populates="order",
                                         cascade="all, delete-orphan")

    # Indexes for common queries
    __table_args__ = (
        Index("ix_orders_worker_number",  "worker_number"),
        Index("ix_orders_phone_number",   "phone_number"),
        Index("ix_orders_assignment_status", "assignment_status"),
        Index("ix_orders_created_at",     "created_at"),
        Index("ix_orders_sync_status", "sync_status"),
    )

    def __repr__(self):
        return (
            f"OrderRecord("
            f"order_id={self.order_id!r}, "
            f"customer={self.customer_name!r}, "
            f"worker={self.worker_number!r}, "
            f"status={self.assignment_status!r})"
        )


class AssignmentRecord(Base):
    '''
    One row per assignment event.
    Append-only — never updated. New assignments add new rows.

    order_id is a foreign key to OrderRecord.
    Multiple rows per order are normal (reassignments).
    '''
    __tablename__ = "assignments"

    id              = Column(Integer,  primary_key=True, autoincrement=True)
    history_id      = Column(String,   nullable=True,  unique=True)
    order_id        = Column(String,   ForeignKey("orders.order_id"), nullable=False)
    worker_number   = Column(String,   nullable=False)
    worker_name     = Column(String,   nullable=True)
    rule            = Column(String,   nullable=False)   # AppliedRule value
    status          = Column(String,   nullable=False)   # ResolutionStatus value
    window_id       = Column(String,   nullable=True)
    previous_worker = Column(String,   nullable=True)
    notes           = Column(Text,     nullable=True)
    resolved_at     = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=func.now(), nullable=False)

    # Relationship
    order = relationship("OrderRecord", back_populates="assignments")

    __table_args__ = (
        Index("ix_assignments_order_id",    "order_id"),
        Index("ix_assignments_worker",      "worker_number"),
    )

    def __repr__(self):
        return (
            f"AssignmentRecord("
            f"order={self.order_id!r}, "
            f"worker={self.worker_number!r}, "
            f"rule={self.rule!r})"
        )


class DuplicateGroupRecord(Base):
    '''
    One row per DuplicateGroup.
    group_id is the primary key (OMS-assigned UUID fragment).
    '''
    __tablename__ = "duplicate_groups"

    group_id            = Column(String,  primary_key=True)
    canonical_order_id  = Column(String,  ForeignKey("orders.order_id"), nullable=False)
    classification      = Column(String,  nullable=False)
    resolved            = Column(Boolean, default=False, nullable=False)
    resolution_notes    = Column(Text,    nullable=True)
    created_at          = Column(DateTime, default=func.now(), nullable=False)
    updated_at          = Column(DateTime, onupdate=func.now())

    # Relationships
    members = relationship("DuplicateMemberRecord",
                           back_populates="group",
                           cascade="all, delete-orphan")

    def __repr__(self):
        return (
            f"DuplicateGroupRecord("
            f"group_id={self.group_id!r}, "
            f"canonical={self.canonical_order_id!r})"
        )


class DuplicateMemberRecord(Base):
    '''
    Join table: order membership in duplicate groups.
    One row per (group_id, order_id) pair.
    '''
    __tablename__ = "duplicate_members"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    group_id    = Column(String,  ForeignKey("duplicate_groups.group_id"), nullable=False)
    order_id    = Column(String,  ForeignKey("orders.order_id"),           nullable=False)
    is_canonical = Column(Boolean, default=False, nullable=False)
    added_at    = Column(DateTime, default=func.now(), nullable=False)

    # Relationships
    group = relationship("DuplicateGroupRecord", back_populates="members")
    order = relationship("OrderRecord", back_populates="duplicate_memberships")

    __table_args__ = (
        Index("ix_dup_members_group_id", "group_id"),
        Index("ix_dup_members_order_id", "order_id"),
    )
