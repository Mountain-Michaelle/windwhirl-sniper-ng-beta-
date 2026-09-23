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
#   - Only WORKER_EDITABLE_COLUMNS accept typed input from a worker —
#     every other column (reference data, sync metadata, control) is
#     locked down with its own protected range, not just the row key.
#   - A "Scheduled" status is only ever accepted together with a valid
#     Scheduled Date + Time. This is re-checked on every sync, not just
#     the first time — a status reset to "Scheduled" later with a stale
#     or missing date is rejected again, every time.
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
from datetime import datetime, date, time
from pathlib import Path
from typing import Optional

from app.oms.shared.logger import get_logger

log = get_logger(__name__)


# ----------------------------------------------------------------
# Day 10.6.2: COLUMN LAYOUT — reorganized so the fields workers touch
# every day sit up front, reference/system fields sit in the middle,
# and the two truly hidden/system-only columns sit at the very end.
#
#   A–H   WORKER-EDITABLE  (only these accept free typing from a worker)
#   I–T   REFERENCE        (worker can read, cannot edit — protected)
#   U–X   SYNC METADATA    (system-written — protected)
#   Y–Z   CONTROL          (system-only — protected, Y is also hidden)
#   AA1/2 TRIGGER CELLS    (outside the data range entirely)
# ----------------------------------------------------------------
ALL_COLUMNS = [
    # ---- WORKER-EDITABLE (A–H) ----
    "customer_name",       # A
    "phone_number",        # B
    "whatsapp_number",     # C
    "comments",             # D — free text, null=True/blank=True
    "sniper_action",         # E — dropdown-enforced status
    "scheduled_date",         # F — required only when sniper_action = Scheduled
    "scheduled_time",          # G — required only when sniper_action = Scheduled
    "_action",                   # H — type DELETE to soft-archive this row

    # ---- REFERENCE (I–T) — read-only for workers ----
    "database_id",         # I  — cosmetic; real match key is _row_key
    "campaign",             # J
    "package",               # K
    "delivery_address",       # L
    "delivery_request",         # M
    "order_date",                 # N
    "customer_question",           # O
    "assigned_worker",               # P
    "assignment_status",               # Q
    "duplicate_status",                  # R
    "quality_score",                       # S
    "is_valid",                              # T

    # ---- SYNC METADATA (U–X) — system-written, read-only for workers ----
    "sync_status",          # U
    "created_at",             # V
    "updated_at",               # W
    "google_row_id",              # X

    # ---- CONTROL (Y–Z) — system-only, protected ----
    "_row_key",       # Y — hidden + protected, the true match key
    "_sync_note",       # Z — protected; sync engine writes results here
]

# Columns a worker is allowed to type into. Everything else in
# ALL_COLUMNS gets a protected range so nothing else accepts input
# outside the value the sync engine (or a dropdown) put there.
WORKER_EDITABLE_COLUMNS = {
    "customer_name", "phone_number", "whatsapp_number", "comments",
    "sniper_action", "scheduled_date", "scheduled_time", "_action",
}

HEADER_ROW = [c.replace("_", " ").title() if not c.startswith("_") else c
              for c in ALL_COLUMNS]

# Fixed control cells, placed one column past the last data column (Z)
# so they never collide with real order data even as rows are added.
TRIGGER_CELL    = "AA1"   # Apps Script "Sync Now" button writes REQUESTED here
LAST_EDIT_CELL  = "AA2"   # Apps Script onEdit() writes an ISO timestamp here

ROW_KEY_COLUMN      = "_row_key"
ACTION_COLUMN       = "_action"
STATUS_NOTE_COLUMN  = "_sync_note"
SCHEDULED_DATE_COLUMN = "scheduled_date"
SCHEDULED_TIME_COLUMN = "scheduled_time"
SNIPER_ACTION_COLUMN  = "sniper_action"

DELETE_VALUE     = "DELETE"
SCHEDULED_STATUS = "Scheduled"

# ----------------------------------------------------------------
# Day 10.6.1: allowed values for the sniper_action column. Anything
# a worker types that doesn't match this list (case-insensitive) is
# ignored on sync-back — the field is simply left untouched rather
# than accepting junk into the DB.
# ----------------------------------------------------------------
ALLOWED_SNIPER_ACTIONS = [
    "Pending", "Confirmed", "Awaiting", "Delivered",
    "Commitment Fee Requested", "Not Picking Calls", "Switched Off",
    "Shipped", "Scheduled", "Failed", "Cancelled", "Returned",
    "Cash Remitted", "After-Sale Call", "Deleted", "Banned",
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


def parse_scheduled_datetime(date_raw, time_raw) -> Optional[datetime]:
    '''
    Combines the sheet's Scheduled Date + Scheduled Time cells into one
    datetime. Returns None if either piece is missing or unparseable —
    callers must treat that as "not actually scheduled yet", even if
    sniper_action says "Scheduled".
    '''
    if not date_raw or not time_raw:
        return None
    try:
        # gspread date-validated cells commonly come back as "DD/MM/YYYY"
        # or already-parsed datetime/date objects depending on render option.
        if isinstance(date_raw, datetime):
            d = date_raw.date()
        elif isinstance(date_raw, date):
            d = date_raw
        else:
            d = datetime.strptime(str(date_raw).strip(), "%d/%m/%Y").date()

        if isinstance(time_raw, datetime):
            t = time_raw.time()
        elif isinstance(time_raw, time):
            t = time_raw
        else:
            raw_t = str(time_raw).strip()
            fmt = "%H:%M:%S" if raw_t.count(":") == 2 else "%H:%M"
            t = datetime.strptime(raw_t, fmt).time()

        return datetime.combine(d, t)
    except Exception:
        return None


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
    # Day 10.6.2: protect EVERY column a worker shouldn't be typing
    # into — not just _row_key. Each non-editable column gets its own
    # protected range, locked to the service account only. Run once
    # at startup; safe to call repeatedly (existing protections are
    # left as-is by the Sheets API, this only adds new ones).
    # ------------------------------------------------------------
    async def ensure_field_protection(self) -> None:
        if not self._worksheet or not self._spreadsheet:
            return
        try:
            requests = []
            for i, col_name in enumerate(ALL_COLUMNS):
                if col_name in WORKER_EDITABLE_COLUMNS:
                    continue  # leave these open for workers to type into
                requests.append({
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {
                                "sheetId": self._worksheet.id,
                                "startColumnIndex": i,
                                "endColumnIndex": i + 1,
                            },
                            "description": f"System field ({col_name}) — do not edit",
                            "warningOnly": False,
                            "editors": {"users": [self._cfg.google.service_account_email]},
                        }
                    }
                })

            if requests:
                self._spreadsheet.batch_update({"requests": requests})

            # Hide the truly internal match-key column only — the rest
            # (reference/sync-metadata/_sync_note) stay visible since
            # workers still need to read them, just not edit them.
            row_key_index = ALL_COLUMNS.index(ROW_KEY_COLUMN)
            self._worksheet.hide_columns(row_key_index, row_key_index + 1)

            log.info(
                f"GoogleSheetsProvider: protected {len(requests)} non-editable "
                f"column(s); _row_key hidden."
            )
        except Exception as e:
            # Not fatal — protection is defense-in-depth, not the only guard.
            log.warning(f"GoogleSheetsProvider: could not apply field protection: {e}")

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
    # Outbound (DB → Sheet) — extended to write scheduled_date/time
    # and to stamp a fresh _row_key on brand-new rows.
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
            col_index = ALL_COLUMNS.index("database_id") + 1
            cell = self._worksheet.find(order_id, in_column=col_index)
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
        '''
        Column order MUST match ALL_COLUMNS exactly — worker-editable
        fields first, reference fields, sync metadata, then control.
        '''
        def fmt_dt(dt):
            return dt.strftime("%d/%m/%Y %H:%M") if hasattr(dt, "strftime") else (dt or "")
        def fmt_date(dt):
            return dt.strftime("%d/%m/%Y") if hasattr(dt, "strftime") else ""
        def fmt_time(dt):
            return dt.strftime("%H:%M") if hasattr(dt, "strftime") else ""
        def safe(val, default=""):
            return default if val is None else str(val)

        scheduled_at = getattr(record, "scheduled_at", None)

        return [
            # ---- WORKER-EDITABLE (A–H) ----
            safe(record.customer_name), safe(record.phone_number), safe(record.whatsapp_number),
            safe(getattr(record, "comments", None)),
            safe(getattr(record, "sniper_action", None)),
            fmt_date(scheduled_at), fmt_time(scheduled_at),
            "",  # _action — always starts blank on write-out

            # ---- REFERENCE (I–T) ----
            safe(record.order_id), safe(record.campaign), safe(record.package_name),
            safe(record.delivery_address), safe(record.delivery_request), safe(record.order_date_raw),
            safe(record.customer_question), safe(record.worker_number), safe(record.assignment_status),
            safe(record.duplicate_status), f"{record.quality_score:.0%}" if record.quality_score is not None else "",
            "Yes" if record.is_valid else "No",

            # ---- SYNC METADATA (U–X) ----
            safe(getattr(record, "sync_status", "PENDING")),
            fmt_dt(record.created_at), fmt_dt(record.updated_at),
            safe(getattr(record, "google_row_id", "")),

            # ---- CONTROL (Y–Z) ----
            row_key, "",   # _row_key, _sync_note (blank on write-out)
        ]

    @staticmethod
    def _col_letter(n: int) -> str:
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

        if "scheduled_date" in ignored_fields:
            note = "⚠️ synced, but 'Scheduled' needs a valid Date + Time — status not changed"
        elif ignored_fields:
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
# ADD import at the top of sync_worker.py:
from app.oms.sync.sync_models import SchemaMismatchError

# ADD this branch inside SynchronizationWorker._process(), alongside
# the existing INSERT_ORDER / UPDATE_ORDER / BATCH_INSERT branches:

            elif job.operation == SyncOperation.ARCHIVE_ORDER:
                await self._repo.mark_archived(job.order_id)  # soft delete, always

# ADD a header check at the START of _insert()/_update()/_batch_insert()
# (the three places that actually write a row's worth of columns into
# the sheet) so a broken layout is caught BEFORE any write, not after:

    async def _insert(self, job: SyncJob) -> None:
        await self._assert_schema_ok()
        record = await self._repo.get_by_id(job.order_id)
        ...  # rest unchanged from Day 10.5

    async def _update(self, job: SyncJob) -> None:
        await self._assert_schema_ok()
        record = await self._repo.get_by_id(job.order_id)
        ...  # rest unchanged from Day 10.5

    async def _assert_schema_ok(self) -> None:
        '''Raises SchemaMismatchError if the sheet's header row has
        drifted from ALL_COLUMNS — e.g. a column was deleted/renamed.'''
        is_valid, missing = await self._provider.validate_headers()
        if not is_valid:
            raise SchemaMismatchError(f"Sheet header mismatch — missing/renamed: {missing}")

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
    sniper_action = Column(String,   nullable=True)   # must be one of ALLOWED_SNIPER_ACTIONS or null
    comments      = Column(Text,     nullable=True)   # free text, no validation
    scheduled_at  = Column(DateTime, nullable=True)    # set only when sniper_action == "Scheduled"

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
#       'scheduled_at':  'ALTER TABLE orders ADD COLUMN scheduled_at DATETIME',
#   }
#
# NOTE: nullable=True alone is enough in SQLAlchemy — an empty string
# is already a valid value for a nullable String/Text column, there's
# no separate "blank" flag like in Django.
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
        Maps editable sheet columns onto the DB record. ONLY the columns
        in WORKER_EDITABLE_COLUMNS are ever applied — reference fields
        (delivery_address, assigned_worker, etc.) are read-only in the
        sheet now (protected ranges) and are never taken from sheet_row
        even if present, since a worker should never be able to change
        them from here.

        Returns a list of field names that were IGNORED because their
        sheet value didn't pass validation — the caller uses this to
        add a note to the row's status. Currently:
          - "sniper_action"    → value not in ALLOWED_SNIPER_ACTIONS
          - "scheduled_date"   → sniper_action is "Scheduled" but no
                                  valid date+time was given (business
                                  rule: a Scheduled order must always
                                  carry a real schedule, every time the
                                  status is set — not just once)
        '''
        from app.oms.sync.google_provider import (
            normalize_sniper_action, parse_scheduled_datetime, SCHEDULED_STATUS,
        )

        # Only genuinely worker-editable free-text fields. Everything
        # else (delivery_address, assigned_worker, etc.) is reference
        # data now and is intentionally NOT read from sheet_row here.
        PLAIN_EDITABLE_FIELDS = {
            "customer_name":   "customer_name",
            "phone_number":    "phone_number",
            "whatsapp_number": "whatsapp_number",
            "comments":        "comments",   # free text — nullable, no validation
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
            new_sniper_action = record.sniper_action
            if "sniper_action" in sheet_row:
                raw = sheet_row["sniper_action"]
                if not str(raw or "").strip():
                    new_sniper_action = None   # explicitly cleared — valid
                else:
                    canonical = normalize_sniper_action(raw)
                    if canonical is None:
                        ignored_fields.append("sniper_action")
                    else:
                        new_sniper_action = canonical

            # scheduled_date / scheduled_time: only meaningful together,
            # and only enforced when the (possibly just-set) status is
            # "Scheduled". This check runs every single time — if the
            # worker changes the status back to "Scheduled" again later
            # with a stale/blank date, it's caught again, not just once.
            if new_sniper_action == SCHEDULED_STATUS:
                scheduled_at = parse_scheduled_datetime(
                    sheet_row.get("scheduled_date"), sheet_row.get("scheduled_time"),
                )
                if scheduled_at is None:
                    # Reject the status change itself — a "Scheduled" order
                    # with no real date/time is not accepted. Keep whatever
                    # sniper_action the record already had.
                    ignored_fields.append("scheduled_date")
                else:
                    record.sniper_action = new_sniper_action
                    record.scheduled_at  = scheduled_at
            else:
                record.sniper_action = new_sniper_action
                record.scheduled_at  = None   # any non-Scheduled status clears it,
                                               # even if stale date/time cells
                                               # linger from a bypassed paste edit

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
#       await provider.ensure_field_protection()
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
// ==============================================================
// WINDWHIRL OMS — GOOGLE SHEETS APPS SCRIPT
// PATH: windwhirl/apps_script/sync_trigger.gs
// ==============================================================
//
// COLUMN LAYOUT (must match google_provider.py ALL_COLUMNS exactly).
// Worker-editable fields come first so the daily-use columns are
// always the ones on screen; reference/system fields trail behind.
// Actual protection (only the service account may write outside the
// worker-editable columns) is applied server-side by Python's
// GoogleSheetsProvider.ensure_field_protection() — this script only
// adds the dropdowns/pickers and visual layout.
//
// ── WORKER-EDITABLE (A–H) ─────────────────────────────────────
//  A  customer_name
//  B  phone_number
//  C  whatsapp_number
//  D  comments             ← free text
//  E  sniper_action        ← dropdown, must match allowed list
//  F  scheduled_date       ← required only when E = "Scheduled"
//  G  scheduled_time       ← required only when E = "Scheduled"
//  H  _action              ← type DELETE to soft-archive this row
//
// ── REFERENCE (I–T) — protected, read-only for workers ────────
//  I  database_id   J  campaign          K  package
//  L  delivery_address                   M  delivery_request
//  N  order_date     O  customer_question
//  P  assigned_worker                    Q  assignment_status
//  R  duplicate_status                   S  quality_score
//  T  is_valid
//
// ── SYNC METADATA (U–X) — protected ────────────────────────────
//  U  sync_status   V  created_at   W  updated_at   X  google_row_id
//
// ── CONTROL (Y–Z) — protected, Y is also hidden ────────────────
//  Y  _row_key       (hidden + protected, the true match key)
//  Z  _sync_note      (protected — sync engine writes status here)
//
// ── TRIGGER CELLS (outside the data range) ─────────────────────
//  AA1  REQUESTED / IDLE   ← "Sync Now" button writes here
//  AA2  ISO timestamp      ← onEdit() writes here on every edit
// ==============================================================

var COL = {
  CUSTOMER_NAME:     1,   // A
  PHONE_NUMBER:      2,   // B
  WHATSAPP_NUMBER:   3,   // C
  COMMENTS:          4,   // D
  SNIPER_ACTION:     5,   // E
  SCHED_DATE:        6,   // F
  SCHED_TIME:        7,   // G
  ACTION:            8,   // H

  DATABASE_ID:       9,   // I
  CAMPAIGN:          10,  // J
  PACKAGE:           11,  // K
  DELIVERY_ADDRESS:  12,  // L
  DELIVERY_REQUEST:  13,  // M
  ORDER_DATE:        14,  // N
  CUSTOMER_QUESTION: 15,  // O
  ASSIGNED_WORKER:   16,  // P
  ASSIGNMENT_STATUS: 17,  // Q
  DUPLICATE_STATUS:  18,  // R
  QUALITY_SCORE:     19,  // S
  IS_VALID:          20,  // T

  SYNC_STATUS:       21,  // U
  CREATED_AT:        22,  // V
  UPDATED_AT:        23,  // W
  GOOGLE_ROW_ID:     24,  // X

  ROW_KEY:           25,  // Y  — hidden + protected
  SYNC_NOTE:         26,  // Z  — protected
};

var TRIGGER_CELL   = 'AA1';
var LAST_EDIT_CELL = 'AA2';

var SNIPER_ACTION_STATUSES = [
  'Pending', 'Confirmed', 'Awaiting', 'Delivered',
  'Commitment Fee Requested', 'Not Picking Calls', 'Switched Off',
  'Shipped', 'Scheduled', 'Failed', 'Cancelled', 'Returned',
  'Cash Remitted', 'After-Sale Call', 'Deleted', 'Banned',
];


// ==============================================================
// MENU
// ==============================================================

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('OMS Sync')
    .addItem('Sync Now', 'requestSync')
    .addSeparator()
    .addItem('Setup Sheet (run once)', 'runAllSetup')
    .addToUi();
}


// ==============================================================
// SYNC TRIGGER
// ==============================================================

function requestSync() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange(TRIGGER_CELL).setValue('REQUESTED');
  SpreadsheetApp.getUi().alert(
    'Sync requested.\\n' +
    'It will run once your edits settle (about 60 seconds).'
  );
}


// ==============================================================
// onEdit — stamps last-edit time + manages Scheduled date/time
// ==============================================================

function onEdit(e) {
  var sheet       = e.range.getSheet();
  var editedRange = e.range;

  // Always stamp the last-edit time so the Python side can debounce
  // and schedule the 30-min-after-last-edit auto-sync correctly.
  // AA2 sits outside the data area, safe to touch on every edit.
  sheet.getRange(LAST_EDIT_CELL).setValue(new Date().toISOString());

  // Only handle single-cell edits in real data rows below this point.
  if (editedRange.getNumRows() !== 1 || editedRange.getNumColumns() !== 1) return;
  var row = editedRange.getRow();
  var col = editedRange.getColumn();
  if (row < 2) return;  // ignore header row

  if (col !== COL.SNIPER_ACTION) return;

  var newValue = editedRange.getValue();
  var dateCell = sheet.getRange(row, COL.SCHED_DATE);
  var timeCell = sheet.getRange(row, COL.SCHED_TIME);

  if (newValue === 'Scheduled') {
    var dateRule = SpreadsheetApp.newDataValidation()
      .requireDate()
      .setAllowInvalid(false)
      .setHelpText('Pick the scheduled delivery date.')
      .build();
    dateCell.setDataValidation(dateRule);
    dateCell.setNumberFormat('dd/mm/yyyy');

    // Sheets has no dedicated "time" validation rule, so this still
    // uses requireDate() (times are stored as date fractions), but we
    // format the cell as a time so it displays and enters like one.
    var timeRule = SpreadsheetApp.newDataValidation()
      .requireDate()
      .setAllowInvalid(false)
      .setHelpText('Pick the scheduled delivery time.')
      .build();
    timeCell.setDataValidation(timeRule);
    timeCell.setNumberFormat('hh:mm');

    SpreadsheetApp.getActive().toast(
      'Fill in Scheduled Date (F) and Time (G) for this row — required for "Scheduled" to be accepted.',
      'Scheduled Delivery',
      6
    );
  } else {
    // Status changed away from "Scheduled" → clear stale date/time so
    // an old schedule can never linger under a different status.
    dateCell.clearContent().clearDataValidations();
    timeCell.clearContent().clearDataValidations();
  }
}


// ==============================================================
// ONE-TIME SETUP — Extensions → Apps Script → select runAllSetup → Run
// ==============================================================

function runAllSetup() {
  setupSniperActionDropdown();
  setupActionDropdown();
  setupScheduledColumnFormatting();
  applyColumnWidths();
  applyHeaderFormatting();
  hideRowKeyColumn();
  SpreadsheetApp.getUi().alert(
    'Setup complete.\\n\\n' +
    '• Sniper Action dropdown active (column E)\\n' +
    '• Delete action dropdown active (column H)\\n' +
    '• Scheduled Date / Time formatting ready (F, G)\\n' +
    '• Column widths + header colours applied\\n' +
    '• _row_key (Y) hidden\\n\\n' +
    'Note: locking columns I–Z to system-only edits is handled by the ' +
    'backend (ensure_field_protection) the first time it connects.'
  );
}

function setupSniperActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var range = sheet.getRange(2, COL.SNIPER_ACTION, sheet.getMaxRows() - 1, 1);
  var rule  = SpreadsheetApp.newDataValidation()
    .requireValueInList(SNIPER_ACTION_STATUSES, true)  // suggestion dropdown while typing
    .setAllowInvalid(false)                             // off-list values rejected client-side too
    .setHelpText('Pick a status — values outside this list are rejected.')
    .build();
  range.setDataValidation(rule);
}

function setupActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var range = sheet.getRange(2, COL.ACTION, sheet.getMaxRows() - 1, 1);
  var rule  = SpreadsheetApp.newDataValidation()
    .requireValueInList(['', 'DELETE'], true)
    .setAllowInvalid(false)
    .setHelpText('Set to DELETE to soft-archive this order.')
    .build();
  range.setDataValidation(rule);
}

function setupScheduledColumnFormatting() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange(1, COL.SCHED_DATE).setValue('Scheduled Date');
  sheet.getRange(1, COL.SCHED_TIME).setValue('Scheduled Time');
}


// ==============================================================
// COLUMN WIDTHS — grouped, readable, single source of truth (COL.*)
// ==============================================================

function applyColumnWidths() {
  var sheet  = SpreadsheetApp.getActiveSheet();
  var widths = {};

  // Worker-editable — the columns used every day, generous widths
  widths[COL.CUSTOMER_NAME]     = 150;
  widths[COL.PHONE_NUMBER]      = 130;
  widths[COL.WHATSAPP_NUMBER]   = 130;
  widths[COL.COMMENTS]          = 220;
  widths[COL.SNIPER_ACTION]     = 170;
  widths[COL.SCHED_DATE]        = 120;
  widths[COL.SCHED_TIME]        = 100;
  widths[COL.ACTION]            = 90;

  // Reference — read-only, keep tighter
  widths[COL.DATABASE_ID]       = 110;
  widths[COL.CAMPAIGN]          = 110;
  widths[COL.PACKAGE]           = 150;
  widths[COL.DELIVERY_ADDRESS]  = 200;
  widths[COL.DELIVERY_REQUEST]  = 120;
  widths[COL.ORDER_DATE]        = 100;
  widths[COL.CUSTOMER_QUESTION] = 180;
  widths[COL.ASSIGNED_WORKER]   = 120;
  widths[COL.ASSIGNMENT_STATUS] = 120;
  widths[COL.DUPLICATE_STATUS]  = 120;
  widths[COL.QUALITY_SCORE]     = 90;
  widths[COL.IS_VALID]          = 70;

  // Sync metadata — narrow
  widths[COL.SYNC_STATUS]       = 100;
  widths[COL.CREATED_AT]        = 130;
  widths[COL.UPDATED_AT]        = 130;
  widths[COL.GOOGLE_ROW_ID]     = 90;

  // Control
  widths[COL.ROW_KEY]           = 100;
  widths[COL.SYNC_NOTE]         = 220;

  for (var col in widths) {
    sheet.setColumnWidth(parseInt(col), widths[col]);
  }
}


// ==============================================================
// HEADER FORMATTING — colour-coded groups
// ==============================================================

function applyHeaderFormatting() {
  var sheet  = SpreadsheetApp.getActiveSheet();
  var header = sheet.getRange(1, 1, 1, COL.SYNC_NOTE);  // A1:Z1

  header
    .setFontWeight('bold')
    .setFontSize(10)
    .setFontColor('#FFFFFF')
    .setVerticalAlignment('middle')
    .setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
  sheet.setRowHeight(1, 40);

  var groups = [
    // [startCol, endCol, hex background]
    [COL.CUSTOMER_NAME,     COL.ACTION,            '#1E6B3C'],  // Worker-editable — green
    [COL.DATABASE_ID,       COL.IS_VALID,           '#1B4F8A'],  // Reference — blue
    [COL.SYNC_STATUS,       COL.GOOGLE_ROW_ID,      '#4A4A4A'],  // Sync metadata — grey
    [COL.ROW_KEY,           COL.SYNC_NOTE,          '#7A0000'],  // Control — dark red
  ];

  groups.forEach(function(g) {
    sheet.getRange(1, g[0], 1, g[1] - g[0] + 1).setBackground(g[2]);
  });

  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(3);  // keep customer_name/phone/whatsapp visible while scrolling
}


// ==============================================================
// HIDE THE TRUE MATCH KEY
// _sync_note (Z) stays visible so workers can see sync results;
// only _row_key (Y) is hidden — it's meaningless to a worker and
// its only job is letting the backend match rows safely.
// ==============================================================

function hideRowKeyColumn() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.hideColumns(COL.ROW_KEY);
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