from __future__ import annotations

import uuid
from datetime import datetime, date, time
from pathlib import Path
from typing import Optional

from apps.oms.shared.logger import get_logger

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
