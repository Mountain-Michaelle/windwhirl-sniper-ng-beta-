from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SyncOperation(str, Enum):
    INSERT_ORDER   = "INSERT_ORDER"
    UPDATE_ORDER   = "UPDATE_ORDER"
    BATCH_INSERT   = "BATCH_INSERT"
    BATCH_UPDATE   = "BATCH_UPDATE"
    # --- Day 10.6 additions (Sheet → DB direction) ---
    INBOUND_SYNC   = "INBOUND_SYNC"    # pull all edited rows from Sheet
    ARCHIVE_ORDER  = "ARCHIVE_ORDER"   # soft delete only, never hard delete
    RECONCILE      = "RECONCILE"       # compare DB vs Sheet row_keys


class SyncStatus(str, Enum):
    PENDING     = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED   = "COMPLETED"
    FAILED      = "FAILED"
    RETRYING    = "RETRYING"
    HALTED      = "HALTED"   # Day 10.6: schema/header mismatch, not retried


class TriggerSource(str, Enum):
    """Why an inbound sync run fired. Used for logging/audit only."""
    MANUAL           = "MANUAL"             # user clicked "Sync Now"
    AUTO_INACTIVITY  = "AUTO_INACTIVITY"    # 30 min since last edit, settled
    STARTUP_CATCHUP  = "STARTUP_CATCHUP"    # app restarted, catching up


class SchemaMismatchError(Exception):
    '''
    Raised when the sheet's header row doesn't match the expected
    ALL_COLUMNS layout — e.g. a worker deleted or renamed a column.
    This is NEVER retried like a transient network error: the sync
    worker halts the job immediately and surfaces it loudly, because
    retrying against a broken layout would just keep writing values
    into the wrong columns until a human fixes the sheet.
    '''
    pass


@dataclass
class SyncJob:
    '''
    One unit of work for the SynchronizationWorker.
    (Unchanged from Day 10.5 except row_key is now optional payload.)
    '''
    order_id:     str
    operation:    SyncOperation
    job_id:       str          = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status:       SyncStatus   = SyncStatus.PENDING
    retry_count:  int          = 0
    max_retries:  int          = 5
    priority:     int          = 10
    created_at:   datetime     = field(default_factory=datetime.now)
    last_attempt: Optional[datetime] = None
    error:        str          = ""
    payload:      dict         = field(default_factory=dict)

    @property
    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries

    @property
    def is_terminal(self) -> bool:
        return self.status in (SyncStatus.COMPLETED, SyncStatus.FAILED, SyncStatus.HALTED)

    def mark_attempt(self) -> None:
        self.last_attempt = datetime.now()
        self.retry_count += 1
        self.status       = SyncStatus.IN_PROGRESS

    def mark_completed(self) -> None:
        self.status = SyncStatus.COMPLETED
        self.error  = ""

    def mark_failed(self, error: str) -> None:
        self.status = SyncStatus.FAILED
        self.error  = error

    def mark_halted(self, error: str) -> None:
        '''Day 10.6: used for schema/header mismatches — not a retry case.'''
        self.status = SyncStatus.HALTED
        self.error  = error

    def mark_retry(self, error: str) -> None:
        self.status = SyncStatus.RETRYING
        self.error  = error


@dataclass
class RowSyncResult:
    '''
    Day 10.6: outcome of processing ONE sheet row during an inbound sync.
    Collected into an InboundSyncSummary so nothing is silently dropped.
    '''
    sheet_row_number: int
    row_key:          Optional[str]
    outcome:          str   # "synced" | "archived" | "flagged" | "error"
    note:              str  # human-readable status written back to sheet


@dataclass
class InboundSyncSummary:
    trigger:      TriggerSource
    started_at:   datetime = field(default_factory=datetime.now)
    finished_at:  Optional[datetime] = None
    halted:       bool = False
    halt_reason:  str  = ""
    results:      list[RowSyncResult] = field(default_factory=list)

    @property
    def synced(self)  -> int: return sum(1 for r in self.results if r.outcome == "synced")
    @property
    def archived(self) -> int: return sum(1 for r in self.results if r.outcome == "archived")
    @property
    def flagged(self) -> int: return sum(1 for r in self.results if r.outcome == "flagged")
    @property
    def errored(self) -> int: return sum(1 for r in self.results if r.outcome == "error")
