# ==============================================================
# WINDWHIRL OMS — DAY 10.6: SHEET → DB SAFE SYNC-BACK LAYER
# (Extends Day 10.5. Day 10.5 pushed DB → Sheet. This adds the
#  reverse direction: user edits made directly in Google Sheets
#  are pulled back into the DB safely, without ID tampering,
#  accidental deletes, or a rushed/half-typed sync breaking data.)
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → sync/sync_models.py            (UPDATE — new operations)
#   FILE 2  → sync/google_provider.py        (UPDATE — hidden row_key,
#                                              action column, status
#                                              write-back, protection,
#                                              header validation,
#                                              trigger/last-edit cells)
#   FILE 3  → sync/inbound_sync_service.py   (NEW — trigger polling,
#                                              debounce/cooldown,
#                                              30-min inactivity fallback,
#                                              per-row safe processing)
#   FILE 4  → sync/reconciliation.py         (NEW — detects orphaned /
#                                              duplicated rows, never
#                                              auto-deletes anything)
#   FILE 5  → sync/sync_worker.py            (UPDATE — soft-delete only,
#                                              header-validation halt)
#   FILE 6  → infrastructure/persistence/schema.py            (ADD)
#   FILE 7  → infrastructure/persistence/order_repository.py  (ADD)
#   FILE 8  → oms_runner.py                  (ADD wiring)
#   FILE 9  → apps_script/sync_trigger.gs    (Sheet-side menu, onEdit
#                                              tracker, _action dropdown,
#                                              sniper_action dropdown)
#
# KEY INVARIANTS (from our conversation, now enforced in code):
#   - The visible "database_id" column is COSMETIC for sync purposes.
#     Rows are matched to the DB using a hidden, protected `_row_key`
#     column the user cannot meaningfully edit.
#   - A row with a missing/unknown `_row_key` is SKIPPED and FLAGGED,
#     never guessed at and never allowed to overwrite another record.
#   - Deletion is NEVER inferred from a row simply disappearing from
#     the sheet. Real delete = user explicitly types the sheet's
#     `_action` value to DELETE. Even then, it is a SOFT delete
#     (archived), never a hard delete triggered from the sheet.
#   - Header/schema changes (a column deleted) HALT the sync entirely
#     for that run rather than silently syncing partial/misaligned data.
#   - One bad row never kills the whole batch — every row is processed
#     in its own try/except and gets a visible status written back.
#   - Sync is triggered either manually (sheet button → flag cell) or
#     automatically ~30 minutes after the LAST EDIT (not a fixed clock),
#     with a short debounce so a sync never fires mid-edit.
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/sync/sync_models.py   (UPDATE)
# ================================================================
# ==============================================================

"""
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
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/sync/google_provider.py   (UPDATE)
# ================================================================
# PURPOSE:
#   Same "only component that talks to Google" role as Day 10.5,
#   now extended with everything needed for SAFE inbound sync:
#     - a hidden, protected `_row_key` column (the real match key)
#     - an `_action` column for explicit delete requests
#     - a `_sync_note` column so users see per-row sync results
#     - header/schema validation before touching any data
#     - trigger cell (manual "Sync Now") + last-edit cell (for the
#       inactivity-based auto-sync and debounce)
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.oms.shared.logger import get_logger

log = get_logger(__name__)


# ----------------------------------------------------------------
# Visible, user-facing columns (unchanged order/meaning from Day 10.5)
# ----------------------------------------------------------------
SHEET_COLUMNS = [
    "database_id",        # A  — cosmetic to sync; users may edit/break this freely
    "google_row_id",      # B
    "campaign",            # C
    "package",             # D
    "customer_name",       # E
    "phone_number",        # F
    "whatsapp_number",     # G
    "delivery_address",    # H
    "delivery_request",    # I
    "order_date",           # J
    "customer_question",    # K
    "assigned_worker",      # L
    "assignment_status",    # M
    "duplicate_status",     # N
    "is_valid",              # O
    "quality_score",         # P
    "sync_status",            # Q
    "created_at",              # R
    "updated_at",               # S
    "sniper_action",              # T — worker-editable status, null=True/blank=True
    "comments",                    # U — worker-editable free text, null=True/blank=True
]

# ----------------------------------------------------------------
# Day 10.6.1: allowed values for the sniper_action column. Anything
# a worker types that doesn't match this list (case-insensitive) is
# ignored on sync-back — the field is simply left untouched rather
# than accepting junk into the DB.
# ----------------------------------------------------------------
ALLOWED_SNIPER_ACTIONS = [
    "Pending",
    "Confirmed",
    "Awaiting",
    "Delivered",
    "Commitment Fee Requested",
    "Not Picking Calls",
    "Switched Off",
    "Shipped",
    "Scheduled",
    "Failed",
    "Cancelled",
    "Returned",
    "Cash Remitted",
    "After-Sale Call",
    "Deleted",
    "Banned",
]
_ALLOWED_SNIPER_ACTIONS_LOOKUP = {v.strip().lower(): v for v in ALLOWED_SNIPER_ACTIONS}


def normalize_sniper_action(raw: str) -> Optional[str]:
    '''
    Returns the canonical status string if raw matches one of the
    allowed values (case/whitespace-insensitive), else None.
    Blank/None is a valid "no value yet" state — also returns None,
    callers should treat that as "nothing to change", not an error.
    '''
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    return _ALLOWED_SNIPER_ACTIONS_LOOKUP.get(cleaned.lower())


# ----------------------------------------------------------------
# Day 10.6: hidden / control columns appended after the visible ones.
# These are NOT part of HEADER_ROW's user-friendly casing — they are
# meant to look like system columns, and they get locked down.
# ----------------------------------------------------------------
ROW_KEY_COLUMN      = "_row_key"      # V — true match key, hidden + protected
ACTION_COLUMN       = "_action"       # W — user sets to "DELETE" to soft-delete
STATUS_NOTE_COLUMN  = "_sync_note"    # X — sync writes results back here

ALL_COLUMNS = SHEET_COLUMNS + [ROW_KEY_COLUMN, ACTION_COLUMN, STATUS_NOTE_COLUMN]
HEADER_ROW  = [c.replace("_", " ").title() if not c.startswith("_") else c
               for c in ALL_COLUMNS]

# Fixed control cells, outside the data range (column Z), so they never
# collide with real order data even as rows are added/removed.
TRIGGER_CELL    = "Z1"   # Apps Script "Sync Now" button writes REQUESTED here
LAST_EDIT_CELL  = "Z2"   # Apps Script onEdit() writes an ISO timestamp here

DELETE_VALUE = "DELETE"


class GoogleSheetsProvider:
    '''
    Google Sheets API wrapper — outbound (DB → Sheet, from Day 10.5)
    AND inbound (Sheet → DB, new in Day 10.6) both live here, since
    this remains the single component allowed to talk to Google.
    '''

    def __init__(self, cfg):
        self._cfg         = cfg
        self._client       = None
        self._spreadsheet  = None
        self._worksheet    = None
        self._connected    = False

    # ------------------------------------------------------------
    # Connection lifecycle — unchanged from Day 10.5
    # ------------------------------------------------------------
    async def connect(self) -> bool:
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            creds_path = Path(self._cfg.google.credentials_file)
            if not creds_path.exists():
                log.error(f"GoogleSheetsProvider: credentials file not found: {creds_path}")
                return False

            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            credentials = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
            self._client      = gspread.authorize(credentials)
            self._spreadsheet = self._client.open_by_key(self._cfg.google.spreadsheet_id)
            self._worksheet   = await self._get_or_create_worksheet(self._cfg.google.sheet_name)
            self._connected   = True
            log.info(f"GoogleSheetsProvider: connected to '{self._cfg.google.sheet_name}'")
            return True
        except Exception as e:
            log.error(f"GoogleSheetsProvider: connection failed: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        self._client = self._spreadsheet = self._worksheet = None
        self._connected = False
        log.info("GoogleSheetsProvider: disconnected.")

    async def ping(self) -> bool:
        if not self._connected or not self._worksheet:
            return False
        try:
            self._worksheet.cell(1, 1)
            return True
        except Exception:
            self._connected = False
            return False

    # ------------------------------------------------------------
    # Header / schema validation — Day 10.6
    # Run this FIRST, every inbound sync. If it fails, HALT — do not
    # attempt to sync any row on a sheet with a broken/missing column.
    # ------------------------------------------------------------
    async def validate_headers(self) -> tuple[bool, list[str]]:
        '''
        Returns (is_valid, missing_columns).
        A missing/renamed column (e.g. someone deleted "Phone Number")
        means row values would be misaligned — better to halt loudly
        than silently write garbage into the wrong DB fields.
        '''
        if not self._worksheet:
            return False, ["<not connected>"]
        try:
            first_row = self._worksheet.row_values(1)
            missing = [h for h in HEADER_ROW if h not in first_row]
            return (len(missing) == 0), missing
        except Exception as e:
            log.error(f"GoogleSheetsProvider: header validation failed: {e}")
            return False, ["<could not read header row>"]

    async def ensure_headers(self) -> None:
        if not self._worksheet:
            return
        try:
            first_row = self._worksheet.row_values(1)
            if not first_row or first_row[0] != HEADER_ROW[0]:
                self._worksheet.insert_row(HEADER_ROW, index=1)
                log.info("GoogleSheetsProvider: headers written to row 1.")
        except Exception as e:
            log.warning(f"GoogleSheetsProvider: could not ensure headers: {e}")

    # ------------------------------------------------------------
    # Protect the hidden row_key column — Day 10.6
    # Run once at startup. warningOnly=False hard-locks the column
    # to everyone except the service account. Users can still see it
    # if unhidden, but cannot edit it through normal sheet use.
    # ------------------------------------------------------------
    async def ensure_row_key_protection(self) -> None:
        if not self._worksheet or not self._spreadsheet:
            return
        try:
            col_index = ALL_COLUMNS.index(ROW_KEY_COLUMN)  # 0-based
            self._spreadsheet.batch_update({
                "requests": [{
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {
                                "sheetId": self._worksheet.id,
                                "startColumnIndex": col_index,
                                "endColumnIndex": col_index + 1,
                            },
                            "description": "System row key — do not edit",
                            "warningOnly": False,
                            "editors": {"users": [self._cfg.google.service_account_email]},
                        }
                    }
                }]
            })
            # Also hide the column so it's out of the way visually.
            self._worksheet.hide_columns(col_index, col_index + 1)
            log.info("GoogleSheetsProvider: _row_key column protected and hidden.")
        except Exception as e:
            # Not fatal — protection is defense-in-depth, not the only guard.
            log.warning(f"GoogleSheetsProvider: could not protect _row_key column: {e}")

    # ------------------------------------------------------------
    # Trigger / last-edit control cells — Day 10.6
    # ------------------------------------------------------------
    async def read_trigger_flag(self) -> str:
        if not self._worksheet:
            return ""
        try:
            return (self._worksheet.acell(TRIGGER_CELL).value or "").strip().upper()
        except Exception:
            return ""

    async def clear_trigger_flag(self) -> None:
        if not self._worksheet:
            return
        try:
            self._worksheet.update_acell(TRIGGER_CELL, "IDLE")
        except Exception as e:
            log.warning(f"GoogleSheetsProvider: could not clear trigger flag: {e}")

    async def read_last_edit_time(self) -> Optional[datetime]:
        '''Written by the sheet's onEdit() Apps Script trigger (see FILE 9).'''
        if not self._worksheet:
            return None
        try:
            raw = self._worksheet.acell(LAST_EDIT_CELL).value
            return datetime.fromisoformat(raw) if raw else None
        except Exception:
            return None

    # ------------------------------------------------------------
    # Reading rows for inbound sync — Day 10.6
    # ------------------------------------------------------------
    async def pull_all_rows(self) -> list[dict]:
        '''
        Returns each data row as a dict INCLUDING its sheet row number
        (1-based, accounting for the header row), so results/status can
        be written back to the exact right row.
        '''
        if not self._worksheet:
            return []
        records = self._worksheet.get_all_records(expected_headers=HEADER_ROW)
        rows = []
        for i, record in enumerate(records, start=2):  # row 1 is the header
            record["_sheet_row_number"] = i
            rows.append(record)
        return rows

    async def write_row_status(self, sheet_row_number: int, note: str) -> None:
        '''Writes a visible status (e.g. "✅ synced", "⚠️ needs review") back.'''
        if not self._worksheet:
            return
        try:
            col_index = ALL_COLUMNS.index(STATUS_NOTE_COLUMN) + 1  # 1-based
            self._worksheet.update_cell(sheet_row_number, col_index, note)
        except Exception as e:
            log.warning(f"GoogleSheetsProvider: could not write row status: {e}")

    def generate_row_key(self) -> str:
        return uuid.uuid4().hex[:12]

    async def write_row_key(self, sheet_row_number: int, row_key: str) -> None:
        '''Used only when creating a brand-new row that has no key yet.'''
        if not self._worksheet:
            return
        col_index = ALL_COLUMNS.index(ROW_KEY_COLUMN) + 1
        self._worksheet.update_cell(sheet_row_number, col_index, row_key)

    # ------------------------------------------------------------
    # Outbound (DB → Sheet) — unchanged in spirit from Day 10.5,
    # just extended to also stamp a fresh _row_key on brand-new rows.
    # ------------------------------------------------------------
    async def append_order(self, record) -> Optional[int]:
        if not self._connected or not self._worksheet:
            return None
        try:
            row_key = getattr(record, "row_key", None) or self.generate_row_key()
            row_data = self._record_to_row(record, row_key)
            self._worksheet.append_row(
                row_data, value_input_option="USER_ENTERED", insert_data_option="INSERT_ROWS",
            )
            row_number = await self.find_by_database_id(record.order_id)
            log.info(f"GoogleSheetsProvider: appended order {record.order_id!r} → row {row_number}")
            return row_number
        except Exception as e:
            log.error(f"GoogleSheetsProvider: append failed for {record.order_id!r}: {e}")
            raise

    async def update_order(self, google_row_id: int, record) -> bool:
        if not self._connected or not self._worksheet:
            return False
        try:
            row_key = getattr(record, "row_key", None) or self.generate_row_key()
            row_data = self._record_to_row(record, row_key)
            col_end = self._col_letter(len(ALL_COLUMNS))
            range_name = f"A{google_row_id}:{col_end}{google_row_id}"
            self._worksheet.update(range_name, [row_data])
            return True
        except Exception as e:
            log.error(f"GoogleSheetsProvider: update failed for row {google_row_id}: {e}")
            raise

    async def find_by_database_id(self, order_id: str) -> Optional[int]:
        if not self._worksheet:
            return None
        try:
            cell = self._worksheet.find(order_id, in_column=1)
            return cell.row if cell else None
        except Exception:
            return None

    async def find_by_row_key(self, row_key: str) -> Optional[int]:
        '''The Day 10.6 replacement for trusting the visible id column.'''
        if not self._worksheet or not row_key:
            return None
        try:
            col_index = ALL_COLUMNS.index(ROW_KEY_COLUMN) + 1
            cell = self._worksheet.find(row_key, in_column=col_index)
            return cell.row if cell else None
        except Exception:
            return None

    async def create_sheet_if_missing(self, sheet_name: str) -> None:
        if not self._spreadsheet:
            return
        existing = [ws.title for ws in self._spreadsheet.worksheets()]
        if sheet_name not in existing:
            self._spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(ALL_COLUMNS))
            log.info(f"GoogleSheetsProvider: created sheet '{sheet_name}'")

    async def _get_or_create_worksheet(self, sheet_name: str):
        await self.create_sheet_if_missing(sheet_name)
        ws = self._spreadsheet.worksheet(sheet_name)
        first_row = ws.row_values(1)
        if not first_row or first_row[0] != HEADER_ROW[0]:
            ws.insert_row(HEADER_ROW, index=1)
        return ws

    def _record_to_row(self, record, row_key: str) -> list:
        def fmt_dt(dt):
            return dt.strftime("%d/%m/%Y %H:%M") if hasattr(dt, "strftime") else (dt or "")
        def safe(val, default=""):
            return default if val is None else str(val)

        base = [
            safe(record.order_id), safe(getattr(record, "google_row_id", "")),
            safe(record.campaign), safe(record.package_name), safe(record.customer_name),
            safe(record.phone_number), safe(record.whatsapp_number), safe(record.delivery_address),
            safe(record.delivery_request), safe(record.order_date_raw), safe(record.customer_question),
            safe(record.worker_number), safe(record.assignment_status), safe(record.duplicate_status),
            "Yes" if record.is_valid else "No",
            f"{record.quality_score:.0%}" if record.quality_score is not None else "",
            safe(getattr(record, "sync_status", "PENDING")),
            fmt_dt(record.created_at), fmt_dt(record.updated_at),
            safe(getattr(record, "sniper_action", None)),   # blank if null in DB
            safe(getattr(record, "comments", None)),        # blank if null in DB
        ]
        # Day 10.6 control columns: row_key, action (blank), status note (blank)
        return base + [row_key, "", ""]

    @staticmethod
    def _col_letter(n: int) -> str:
        # supports > 26 columns, unlike the Day 10.5 chr()-only version
        letters = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    @property
    def is_connected(self) -> bool:
        return self._connected
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/sync/inbound_sync_service.py   (NEW)
# ================================================================
# PURPOSE:
#   Everything about WHEN and HOW a sheet-edit sync happens safely.
#     - InboundSyncTrigger: decides WHEN to fire (manual button via
#       the flag cell, or ~30 min after the last edit, with a short
#       debounce so nothing fires mid-keystroke).
#     - InboundSyncProcessor: decides HOW each row is applied — this
#       is where every rule from our conversation is enforced.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from app.oms.sync.sync_models import InboundSyncSummary, RowSyncResult, TriggerSource
from app.oms.sync.google_provider import GoogleSheetsProvider, DELETE_VALUE, ROW_KEY_COLUMN, ACTION_COLUMN
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class InboundSyncTrigger:
    '''
    Decides WHEN an inbound sync should run. Never runs the sync
    itself — just calls the given async callback when conditions
    are met, then goes back to polling.

    Rules (from our conversation):
      - Manual: user sets the sheet's trigger cell to REQUESTED.
        We still wait a short EDIT_COOLDOWN after the last detected
        edit before actually running, so a manual click right after
        typing doesn't sync a half-finished row.
      - Auto: fires ~AUTO_SYNC_AFTER after the last edit — measured
        from the last edit, not a fixed wall-clock interval — and
        only if the sheet has actually changed since the last sync.
    '''

    POLL_INTERVAL_SECONDS = 15
    EDIT_COOLDOWN_SECONDS = 60          # let a burst of edits settle
    AUTO_SYNC_AFTER        = timedelta(minutes=30)

    def __init__(self, provider: GoogleSheetsProvider):
        self._provider        = provider
        self._last_sync_time  = datetime.now()
        self._running         = False

    async def poll_loop(self, on_trigger) -> None:
        '''
        on_trigger: async callable(TriggerSource) -> InboundSyncSummary
        '''
        self._running = True
        log.info("InboundSyncTrigger: polling started.")

        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL_SECONDS)

                flag           = await self._provider.read_trigger_flag()
                last_edit_time = await self._provider.read_last_edit_time()
                now            = datetime.now()

                edits_are_settled = (
                    last_edit_time is not None
                    and (now - last_edit_time).total_seconds() >= self.EDIT_COOLDOWN_SECONDS
                )
                nothing_changed_since_last_sync = (
                    last_edit_time is not None and last_edit_time <= self._last_sync_time
                )

                if flag == "REQUESTED" and edits_are_settled:
                    await self._fire(on_trigger, TriggerSource.MANUAL)
                    await self._provider.clear_trigger_flag()
                    continue

                if flag == "REQUESTED" and not edits_are_settled:
                    # User clicked sync while still typing — wait, don't rush it.
                    log.debug("InboundSyncTrigger: manual sync requested but edits not settled yet.")
                    continue

                due_for_auto_sync = (now - self._last_sync_time) >= self.AUTO_SYNC_AFTER
                if due_for_auto_sync and edits_are_settled and not nothing_changed_since_last_sync:
                    await self._fire(on_trigger, TriggerSource.AUTO_INACTIVITY)
                    continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"InboundSyncTrigger: poll loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

        log.info("InboundSyncTrigger: polling stopped.")

    async def _fire(self, on_trigger, source: TriggerSource) -> None:
        log.info(f"InboundSyncTrigger: firing sync ({source.value})")
        await on_trigger(source)
        self._last_sync_time = datetime.now()

    def stop(self) -> None:
        self._running = False


class InboundSyncProcessor:
    '''
    Applies sheet edits to the DB, one row at a time, safely.
    This is where every rule from our conversation is enforced.
    '''

    def __init__(self, provider: GoogleSheetsProvider, order_repo):
        self._provider = provider
        self._repo     = order_repo

    async def run_once(self, trigger: TriggerSource = TriggerSource.STARTUP_CATCHUP) -> InboundSyncSummary:
        summary = InboundSyncSummary(trigger=trigger)

        # Rule: schema/header changes HALT the run. Never sync partial data.
        headers_ok, missing = await self._provider.validate_headers()
        if not headers_ok:
            summary.halted     = True
            summary.halt_reason = f"Missing/renamed columns: {missing}"
            summary.finished_at = datetime.now()
            log.error(f"InboundSyncProcessor: HALTED — {summary.halt_reason}")
            return summary

        rows = await self._provider.pull_all_rows()

        for row in rows:
            # Rule: one bad row never kills the batch.
            try:
                result = await self._process_row(row)
            except Exception as e:
                result = RowSyncResult(
                    sheet_row_number=row.get("_sheet_row_number", -1),
                    row_key=row.get(ROW_KEY_COLUMN),
                    outcome="error",
                    note=f"⚠️ error: {e}",
                )
                log.error(f"InboundSyncProcessor: row {result.sheet_row_number} failed: {e}")

            summary.results.append(result)
            await self._provider.write_row_status(result.sheet_row_number, result.note)

        summary.finished_at = datetime.now()
        log.info(
            f"InboundSyncProcessor: run complete "
            f"(synced={summary.synced}, archived={summary.archived}, "
            f"flagged={summary.flagged}, errors={summary.errored})"
        )
        return summary

    async def _process_row(self, row: dict) -> RowSyncResult:
        sheet_row_number = row["_sheet_row_number"]
        row_key           = (row.get(ROW_KEY_COLUMN) or "").strip()
        action             = (row.get(ACTION_COLUMN) or "").strip().upper()

        # Rule: never trust the visible "database_id" column. Resolve by
        # hidden row_key only. Missing/unknown key → skip and flag,
        # never guess which DB record it might be.
        record = await self._repo.get_by_row_key(row_key) if row_key else None
        if record is None:
            return RowSyncResult(sheet_row_number, row_key or None, "flagged", "⚠️ needs review — unmatched row")

        # Rule: real delete only on an explicit action, and it's a soft
        # delete (archive) — never triggered just because a row vanished.
        if action == DELETE_VALUE:
            await self._repo.mark_archived(record.order_id)
            return RowSyncResult(sheet_row_number, row_key, "archived", "🗑 archived")

        # Otherwise: apply the sheet's edits to the DB record.
        ignored_fields = await self._repo.apply_sheet_edits(record.order_id, row)

        if ignored_fields:
            # Row still syncs — just this one field's bad value didn't stick.
            note = f"✅ synced (ignored invalid: {', '.join(ignored_fields)})"
        else:
            note = "✅ synced"
        return RowSyncResult(sheet_row_number, row_key, "synced", note)
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/sync/reconciliation.py   (NEW)
# ================================================================
# PURPOSE:
#   Catches the failure mode the hidden-key trick can't: a whole row
#   (including its row_key) being deleted, or copy-pasted into a
#   duplicate row. This NEVER deletes or overwrites anything — it
#   only reports, so a human can decide what to do.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.oms.sync.google_provider import GoogleSheetsProvider, ROW_KEY_COLUMN
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


@dataclass
class ReconciliationReport:
    checked_at:          datetime = field(default_factory=datetime.now)
    missing_from_sheet:  list[str] = field(default_factory=list)  # active in DB, gone from sheet
    duplicate_in_sheet:  list[str] = field(default_factory=list)  # same row_key appears twice+


class ReconciliationService:
    '''
    Run this periodically (e.g. once a day, or right after every
    inbound sync) — NOT on every single sync, since it reads the
    entire sheet and full active DB set.
    '''

    def __init__(self, provider: GoogleSheetsProvider, order_repo):
        self._provider = provider
        self._repo     = order_repo

    async def run(self) -> ReconciliationReport:
        report = ReconciliationReport()

        db_keys = set(await self._repo.list_active_row_keys())

        sheet_rows = await self._provider.pull_all_rows()
        sheet_keys = [r.get(ROW_KEY_COLUMN) for r in sheet_rows if r.get(ROW_KEY_COLUMN)]

        seen = set()
        for k in sheet_keys:
            if k in seen:
                report.duplicate_in_sheet.append(k)
            seen.add(k)

        report.missing_from_sheet = sorted(db_keys - seen)

        if report.missing_from_sheet:
            log.warning(
                f"ReconciliationService: {len(report.missing_from_sheet)} active "
                f"record(s) have no matching row in the sheet — possible accidental "
                f"row deletion. Not auto-actioned; review required."
            )
        if report.duplicate_in_sheet:
            log.warning(
                f"ReconciliationService: {len(report.duplicate_in_sheet)} row_key(s) "
                f"appear more than once in the sheet — possible copy-paste duplication."
            )

        return report
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/sync/sync_worker.py   (UPDATE)
# ================================================================
# ADD to the existing SynchronizationWorker (Day 10.5). Only the
# new/changed pieces are shown — everything else stays as-is.
# ================================================================
# ==============================================================

"""
# ADD this branch inside SynchronizationWorker._process(), alongside
# the existing INSERT_ORDER / UPDATE_ORDER / BATCH_INSERT branches:

            elif job.operation == SyncOperation.ARCHIVE_ORDER:
                await self._repo.mark_archived(job.order_id)  # soft delete, always

# ADD a distinct handling path in _handle_failure() for header/schema
# problems, so they are HALTED rather than retried (retrying a broken
# schema just wastes attempts and delays the alert):

    async def _handle_failure(self, job, error: Exception) -> None:
        if isinstance(error, SchemaMismatchError):
            job.mark_halted(str(error))
            await self._repo.update_sync_status(job.order_id, "HALTED", error=str(error))
            log.error(f"SyncWorker: HALTED — schema mismatch: {error}")
            return
        # ... existing retry / permanent-failure logic from Day 10.5 unchanged ...
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/infrastructure/persistence/schema.py   (ADD)
# ================================================================
# ==============================================================

"""
# ADD these columns to the OrderRecord class, after the Day 10.5
# google_row_id / sync_status / last_sync_time / last_sync_error columns:

    # Day 10.6 — sheet sync-back safety fields
    row_key      = Column(String,  unique=True, nullable=True, index=True)
    is_archived  = Column(Boolean, default=False, nullable=False)
    archived_at  = Column(DateTime, nullable=True)

    # Day 10.6.1 — worker-editable fields, synced sheet ↔ DB
    sniper_action = Column(String, nullable=True, blank=True)   # must be one of ALLOWED_SNIPER_ACTIONS or null
    comments      = Column(Text,   nullable=True, blank=True)   # free text, no validation

# ADD to __table_args__:
    Index("ix_orders_row_key", "row_key"),
    Index("ix_orders_is_archived", "is_archived"),
    Index("ix_orders_sniper_action", "sniper_action"),

# SQLite dev migration (add alongside the Day 10.5 migration block):
#   new_cols = {
#       ...
#       'row_key':       'ALTER TABLE orders ADD COLUMN row_key TEXT',
#       'is_archived':   'ALTER TABLE orders ADD COLUMN is_archived BOOLEAN DEFAULT 0',
#       'archived_at':   'ALTER TABLE orders ADD COLUMN archived_at DATETIME',
#       'sniper_action': 'ALTER TABLE orders ADD COLUMN sniper_action TEXT',
#       'comments':      'ALTER TABLE orders ADD COLUMN comments TEXT',
#   }
#
# NOTE: "blank=True" is Django-flavored shorthand carried over from the
# request — plain SQLAlchemy only needs nullable=True; there's no
# separate "blank" concept, an empty string "" is already a valid value
# for a nullable String/Text column.
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/infrastructure/persistence/order_repository.py (ADD)
# ================================================================
# ==============================================================

"""
# ADD to OrderRepository class:

    async def get_by_row_key(self, row_key: str):
        '''The Day 10.6 lookup — never resolve by the visible sheet id.'''
        with self._sf() as session:
            return (
                session.query(OrderRecord)
                       .filter(OrderRecord.row_key == row_key, OrderRecord.is_archived == False)
                       .one_or_none()
            )

    async def mark_archived(self, order_id: str) -> None:
        '''Soft delete only. Real hard-delete is a separate, deliberate
        admin-only operation and is never reachable from the sheet.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return
            record.is_archived = True
            record.archived_at = datetime.now()
            session.commit()
            self._log.info(f"OrderRepository: order {order_id!r} archived (soft delete).")

    async def list_active_row_keys(self) -> list[str]:
        with self._sf() as session:
            rows = (
                session.query(OrderRecord.row_key)
                       .filter(OrderRecord.row_key.isnot(None), OrderRecord.is_archived == False)
                       .all()
            )
            return [r[0] for r in rows]

    async def apply_sheet_edits(self, order_id: str, sheet_row: dict) -> list[str]:
        '''
        Maps editable sheet columns onto the DB record. Only fields the
        sheet is allowed to edit are applied — server-controlled fields
        (sync_status, timestamps, row_key itself) are never taken from
        the sheet, even if present in the row dict.

        Returns a list of field names that were IGNORED because their
        sheet value didn't pass validation (currently: sniper_action)
        — the caller uses this to add a note to the row's status.
        '''
        from app.oms.sync.google_provider import normalize_sniper_action

        PLAIN_EDITABLE_FIELDS = {
            "customer_name":     "customer_name",
            "phone_number":      "phone_number",
            "whatsapp_number":   "whatsapp_number",
            "delivery_address":  "delivery_address",
            "delivery_request":  "delivery_request",
            "customer_question": "customer_question",
            "assigned_worker":   "worker_number",
            "comments":          "comments",   # free text — null=True/blank=True, no validation
        }

        ignored_fields: list[str] = []

        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return ignored_fields

            for sheet_field, db_field in PLAIN_EDITABLE_FIELDS.items():
                if sheet_field in sheet_row:
                    setattr(record, db_field, sheet_row[sheet_field] or None)

            # sniper_action: must match ALLOWED_SNIPER_ACTIONS (case-insensitive)
            # or be blank. Anything else is silently ignored — the sheet cell
            # is left as the worker typed it, but the DB is never polluted.
            if "sniper_action" in sheet_row:
                raw = sheet_row["sniper_action"]
                if not str(raw or "").strip():
                    record.sniper_action = None   # explicitly cleared — valid
                else:
                    canonical = normalize_sniper_action(raw)
                    if canonical is None:
                        ignored_fields.append("sniper_action")
                    else:
                        record.sniper_action = canonical

            record.updated_at = datetime.now()
            session.commit()

        return ignored_fields

    async def ensure_row_key(self, order_id: str, generate_fn) -> str:
        '''Assigns a row_key on first sync-out if the record doesn't have one yet.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return ""
            if not record.row_key:
                record.row_key = generate_fn()
                session.commit()
            return record.row_key
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/oms_runner.py   (ADD wiring)
# ================================================================
# ==============================================================

"""
# 1. ADD imports:
from app.oms.sync.inbound_sync_service import InboundSyncTrigger, InboundSyncProcessor
from app.oms.sync.reconciliation import ReconciliationService

# 2. ADD after the Day 10.5 outbound sync wiring (build_sync):
def build_inbound_sync(provider, order_repo):
    processor = InboundSyncProcessor(provider, order_repo)
    trigger   = InboundSyncTrigger(provider)
    reconciler = ReconciliationService(provider, order_repo)
    return processor, trigger, reconciler

# 3. In main(), after the outbound sync worker is started:
#
#   inbound_task = None
#   if settings.google.enabled:
#       processor, trigger, reconciler = build_inbound_sync(provider, order_repo)
#
#       await provider.ensure_row_key_protection()
#
#       async def on_trigger(source):
#           summary = await processor.run_once(source)
#           if summary.halted:
#               log.error(f"Inbound sync halted: {summary.halt_reason}")
#           # Reconciliation is cheap enough to run after every inbound sync;
#           # move to a daily cron-style task if the sheet grows very large.
#           await reconciler.run()
#           return summary
#
#       inbound_task = asyncio.create_task(trigger.poll_loop(on_trigger), name="inbound_sync")
#       log.info("Inbound (Sheet → DB) sync active.")
#
# 4. In the shutdown/finally block:
#
#   if inbound_task and not inbound_task.done():
#       trigger.stop()
#       try:
#           await asyncio.wait_for(inbound_task, timeout=15.0)
#       except asyncio.TimeoutError:
#           inbound_task.cancel()
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/apps_script/sync_trigger.gs
#  (Runs INSIDE Google Sheets via Extensions → Apps Script.
#   Not Python — included because the CLI-side pieces above depend
#   on the two cells and dropdown this script sets up.)
# ================================================================
# ==============================================================

"""
// -- Menu: adds a "Sync" menu with a "Sync Now" button --
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Sync')
    .addItem('Sync Now', 'requestSync')
    .addToUi();
}

function requestSync() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange('Z1').setValue('REQUESTED');
  SpreadsheetApp.getUi().alert('Sync requested — it will run once your edits settle.');
}

// -- onEdit: stamps the last-edit time on ANY edit. This is what
//    lets the Python side debounce (wait for edits to settle) and
//    schedule the 30-min-after-last-edit auto-sync. --
function onEdit(e) {
  var sheet = e.range.getSheet();
  sheet.getRange('Z2').setValue(new Date().toISOString());
}

// -- One-time setup: run manually once from the Apps Script editor.
//    Adds a dropdown to the _action column so users pick "DELETE"
//    from a list instead of free-typing it (avoids typos silently
//    failing to trigger, and avoids accidental case mismatches). --
function setupActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var actionColIndex = sheet.getRange('W1:W1').getColumn(); // "_action" column
  var range = sheet.getRange(2, actionColIndex, sheet.getMaxRows() - 1, 1);
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(['', 'DELETE'], true)
    .setAllowInvalid(false)
    .build();
  range.setDataValidation(rule);
}

// -- One-time setup: run manually once. Adds a searchable dropdown to
//    the "Sniper Action" column — as the worker types, Sheets narrows
//    the suggestion list to matching statuses. setAllowInvalid(false)
//    means only a listed status (or blank) can actually be entered,
//    matching the Python-side normalize_sniper_action() allow-list. --
function setupSniperActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var statuses = [
    'Pending', 'Confirmed', 'Awaiting', 'Delivered',
    'Commitment Fee Requested', 'Not Picking Calls', 'Switched Off',
    'Shipped', 'Scheduled', 'Failed', 'Cancelled', 'Returned',
    'Cash Remitted', 'After-Sale Call', 'Deleted', 'Banned',
  ];
  var sniperActionColIndex = sheet.getRange('T1:T1').getColumn(); // "Sniper Action" column
  var range = sheet.getRange(2, sniperActionColIndex, sheet.getMaxRows() - 1, 1);
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(statuses, true)   // true = show as suggestion dropdown while typing
    .setAllowInvalid(false)               // blank is still allowed; anything off-list is rejected
    .setHelpText('Pick a status — free typing outside this list is rejected.')
    .build();
  range.setDataValidation(rule);
}
"""


# ==============================================================
# DAY 10.6 VERIFICATION
# ==============================================================
#
# Test 1 — Header validation halts on a missing column:
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.sync.inbound_sync_service import InboundSyncProcessor
#   from app.oms.sync.sync_models import TriggerSource
#   # ... construct provider/repo against a test sheet with a deleted
#   # column, call processor.run_once(), assert summary.halted is True
#   "
#
# Test 2 — Unmatched row_key is flagged, not guessed:
#   Manually blank out the hidden _row_key on one row (temporarily
#   unhide/unprotect it for the test), run an inbound sync, confirm
#   that row's _sync_note becomes "⚠️ needs review" and no DB record
#   was touched.
#
# Test 3 — DELETE action soft-deletes only:
#   Set _action to DELETE on a row, run inbound sync, confirm the DB
#   record has is_archived=True and archived_at set, and that no row
#   was hard-deleted from the database.
#
# Test 4 — Debounce holds off a manual sync mid-edit:
#   Set Z1 to REQUESTED, then immediately edit Z2 to "now" via the
#   sheet; confirm InboundSyncTrigger does NOT fire until
#   EDIT_COOLDOWN_SECONDS has passed with no further edits.
#
# ==============================================================