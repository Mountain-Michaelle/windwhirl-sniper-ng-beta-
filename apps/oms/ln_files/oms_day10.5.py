# ==============================================================
# WINDWHIRL OMS — DAY 10.5: GOOGLE SHEETS SYNC LAYER
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → sync/sync_models.py
#   FILE 2  → sync/retry_policy.py
#   FILE 3  → sync/sync_queue.py
#   FILE 4  → sync/google_provider.py
#   FILE 5  → sync/sync_service.py
#   FILE 6  → sync/sync_worker.py
#   FILE 7  → sync/sync_events.py
#   FILE 8  → sync/__init__.py
#   FILE 9  → infrastructure/persistence/schema.py   (ADD columns only)
#   FILE 10 → infrastructure/persistence/order_repository.py (ADD sync methods)
#   FILE 11 → config/settings.py   (ADD GoogleSettings)
#   FILE 12 → oms_runner.py        (ADD sync wiring)
#
# INSTALL DEPENDENCY FIRST:
#   uv add gspread
#
# ARCHITECTURE:
#   Order saved to DB → "order.persisted" event → SyncService → SyncQueue
#   → SyncWorker (background task) → GoogleSheetsProvider → Google Sheets API
#   → Google Row ID returned → saved back to DB
#
# KEY INVARIANTS:
#   - DB is always source of truth
#   - Google Sheets never blocks the pipeline
#   - OMS never fails because Google is unavailable
#   - Every sync attempt is logged in the orders table
#   - Failed jobs are retained for manual retry / inspection
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/sync/sync_models.py
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
    INSERT_ORDER = "INSERT_ORDER"
    UPDATE_ORDER = "UPDATE_ORDER"
    DELETE_ORDER = "DELETE_ORDER"
    BATCH_INSERT = "BATCH_INSERT"
    BATCH_UPDATE = "BATCH_UPDATE"


class SyncStatus(str, Enum):
    PENDING    = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED  = "COMPLETED"
    FAILED     = "FAILED"
    RETRYING   = "RETRYING"


@dataclass
class SyncJob:
    '''
    One unit of work for the SynchronizationWorker.

    job_id:       Unique identifier for this job.
    order_id:     The database order ID to sync.
    operation:    What to do (INSERT, UPDATE, etc.)
    status:       Current job status.
    retry_count:  How many times this job has been attempted.
    max_retries:  Maximum allowed attempts before marking FAILED.
    priority:     Lower number = higher priority. Default 10.
    created_at:   When this job was queued.
    last_attempt: When the last attempt was made.
    error:        Last error message if any.
    payload:      Optional extra data needed for the operation.
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
        return self.status in (SyncStatus.COMPLETED, SyncStatus.FAILED)

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

    def mark_retry(self, error: str) -> None:
        self.status = SyncStatus.RETRYING
        self.error  = error

    def __repr__(self):
        return (
            f"SyncJob("
            f"id={self.job_id!r}, "
            f"order={self.order_id!r}, "
            f"op={self.operation.value}, "
            f"status={self.status.value}, "
            f"retries={self.retry_count}/{self.max_retries})"
        )
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/sync/retry_policy.py
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import asyncio

from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class RetryPolicy:
    '''
    Exponential backoff retry policy for sync jobs.

    Formula: base_interval * (2 ** retry_count)
    Capped at max_interval to prevent excessive waits.

    Examples (base=5s):
        Attempt 1: wait  5s
        Attempt 2: wait 10s
        Attempt 3: wait 20s
        Attempt 4: wait 40s
        Attempt 5: wait 80s  → FAILED after this

    Usage:
        policy = RetryPolicy(base_interval=5, max_interval=300, max_retries=5)
        if policy.should_retry(job):
            delay = policy.next_delay(job)
            await policy.wait(delay)
    '''

    def __init__(
        self,
        base_interval: float = 5.0,
        max_interval:  float = 300.0,
        max_retries:   int   = 5,
    ):
        self._base    = base_interval
        self._max     = max_interval
        self._max_retries = max_retries

    def should_retry(self, job) -> bool:
        '''True if the job has retries remaining.'''
        return job.retry_count < self._max_retries

    def next_delay(self, job) -> float:
        '''
        Compute the delay before the next retry attempt.
        Exponential backoff capped at max_interval.
        '''
        delay = self._base * (2 ** job.retry_count)
        return min(delay, self._max)

    async def wait(self, seconds: float) -> None:
        '''Wait for the backoff period.'''
        log.debug(f"RetryPolicy: waiting {seconds:.1f}s before next attempt")
        await asyncio.sleep(seconds)

    def is_permanent_failure(self, error: Exception) -> bool:
        '''
        Determine if an error is permanent (no point retrying).
        Permanent failures:
            - Invalid spreadsheet ID
            - Authentication failure (bad credentials)
            - Permission denied on the sheet
        Temporary failures (should retry):
            - Network timeouts
            - Rate limit exceeded (429)
            - Transient Google API errors (500, 503)
        '''
        error_str = str(error).lower()
        permanent_signals = [
            "invalid_grant",
            "unauthorized",
            "spreadsheet not found",
            "permission denied",
            "403",
            "invalid_client",
        ]
        return any(s in error_str for s in permanent_signals)
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/sync/sync_queue.py
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Optional

from app.oms.sync.sync_models import SyncJob, SyncOperation, SyncStatus
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class SynchronizationQueue:
    '''
    In-memory async queue for Google Sheets sync jobs.

    Priority queue: jobs with lower priority number are processed first.
    Thread-safe for use with asyncio tasks.

    Jobs survive within the session. On restart, the SyncService
    re-queues any orders with sync_status=PENDING from the database.

    Usage:
        queue = SynchronizationQueue(max_size=500)
        await queue.enqueue(SyncJob(order_id="ORD-001", ...))
        job = await queue.dequeue()  # Blocks until job available
        queue.size  → current length
    '''

    def __init__(self, max_size: int = 500):
        self._queue    = asyncio.PriorityQueue(maxsize=max_size)
        self._max_size = max_size
        self._enqueued = 0
        self._processed = 0

    async def enqueue(self, job: SyncJob) -> bool:
        '''
        Add a job to the queue.
        Returns False if queue is full — job should be retried later.

        Jobs are sorted by (priority, created_at) so higher priority
        jobs are processed first even if queued later.
        '''
        if self._queue.full():
            log.warning(
                f"SyncQueue: queue full ({self._max_size}) — "
                f"job {job.job_id!r} dropped. Will retry on next cycle."
            )
            return False

        # PriorityQueue sorts by first element of tuple
        sort_key = (job.priority, job.created_at.timestamp())
        await self._queue.put((sort_key, job))
        self._enqueued += 1

        log.debug(
            f"SyncQueue: enqueued {job} "
            f"(queue size: {self._queue.qsize()})"
        )
        return True

    async def dequeue(self) -> SyncJob:
        '''
        Remove and return the next job.
        Blocks until a job is available.
        '''
        _, job = await self._queue.get()
        self._processed += 1
        return job

    def done(self) -> None:
        '''Signal that the last dequeued job is done. Required by asyncio.Queue.'''
        self._queue.task_done()

    async def enqueue_for_retry(self, job: SyncJob) -> None:
        '''
        Re-enqueue a job with lower priority for retry.
        Failed jobs get priority bumped down (higher number = lower priority).
        '''
        job.priority = min(job.priority + 5, 50)
        await self.enqueue(job)

    def drain_pending(self) -> list[SyncJob]:
        '''
        Non-blocking drain of all queued jobs.
        Used during shutdown to get remaining jobs.
        '''
        jobs = []
        while not self._queue.empty():
            try:
                _, job = self._queue.get_nowait()
                jobs.append(job)
            except asyncio.QueueEmpty:
                break
        return jobs

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def is_empty(self) -> bool:
        return self._queue.empty()

    def stats(self) -> dict:
        return {
            "current_size": self.size,
            "max_size":     self._max_size,
            "total_enqueued":  self._enqueued,
            "total_processed": self._processed,
        }
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/sync/google_provider.py
# ================================================================
# PURPOSE:
#   The ONLY component that talks to Google APIs directly.
#   All other OMS components go through this class.
#
# LIBRARY: gspread (install: uv add gspread)
# AUTH: Service account JSON key file
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.oms.shared.logger import get_logger

log = get_logger(__name__)


# Column order in the Google Sheet
# Matches the exact column positions used by all write operations
SHEET_COLUMNS = [
    "database_id",        # A
    "google_row_id",      # B
    "campaign",           # C
    "package",            # D
    "customer_name",      # E
    "phone_number",       # F
    "whatsapp_number",    # G
    "delivery_address",   # H
    "delivery_request",   # I
    "order_date",         # J
    "customer_question",  # K
    "assigned_worker",    # L
    "assignment_status",  # M
    "duplicate_status",   # N
    "is_valid",           # O
    "quality_score",      # P
    "sync_status",        # Q
    "created_at",         # R
    "updated_at",         # S
]

HEADER_ROW = [col.replace("_", " ").title() for col in SHEET_COLUMNS]


class GoogleSheetsProvider:
    '''
    Google Sheets API wrapper. Single responsibility: talk to Google.

    Authentication: Google Service Account (JSON key file).
    No OAuth, no user login. Service account is granted edit access
    to the spreadsheet by the business owner.

    All methods are async-compatible (gspread is sync but calls are
    wrapped to not block the event loop for extended periods).

    Usage:
        provider = GoogleSheetsProvider(cfg)
        await provider.connect()
        row_id = await provider.append_order(order_record)
        await provider.update_order(row_id, order_record)
        await provider.disconnect()
    '''

    def __init__(self, cfg):
        '''
        Args:
            cfg: OMSSettings. Reads from cfg.google.*
        '''
        self._cfg         = cfg
        self._client      = None
        self._spreadsheet = None
        self._worksheet   = None
        self._connected   = False

    async def connect(self) -> bool:
        '''
        Authenticate with Google and open the spreadsheet.
        Returns True on success, False if connection fails.

        Failure here does NOT crash OMS — the sync worker catches it
        and queues a retry.
        '''
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            creds_path = Path(self._cfg.google.credentials_file)
            if not creds_path.exists():
                log.error(
                    f"GoogleSheetsProvider: credentials file not found: "
                    f"{creds_path}\n"
                    "Google Sheets sync will be disabled until credentials "
                    "are provided."
                )
                return False

            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]

            credentials = Credentials.from_service_account_file(
                str(creds_path), scopes=scopes
            )
            self._client      = gspread.authorize(credentials)
            self._spreadsheet = self._client.open_by_key(
                self._cfg.google.spreadsheet_id
            )

            self._worksheet = await self._get_or_create_worksheet(
                self._cfg.google.sheet_name
            )

            self._connected = True
            log.info(
                f"GoogleSheetsProvider: connected to "
                f"'{self._cfg.google.sheet_name}' in "
                f"spreadsheet {self._cfg.google.spreadsheet_id!r}"
            )
            return True

        except Exception as e:
            log.error(
                f"GoogleSheetsProvider: connection failed: {e}\n"
                "Google Sheets sync is unavailable. OMS will continue."
            )
            self._connected = False
            return False

    async def disconnect(self) -> None:
        '''Release Google API resources.'''
        self._client      = None
        self._spreadsheet = None
        self._worksheet   = None
        self._connected   = False
        log.info("GoogleSheetsProvider: disconnected.")

    async def ping(self) -> bool:
        '''True if the Google connection is still alive.'''
        if not self._connected or not self._worksheet:
            return False
        try:
            # Lightweight check: read cell A1
            self._worksheet.cell(1, 1)
            return True
        except Exception:
            self._connected = False
            return False

    async def ensure_headers(self) -> None:
        '''
        Write the header row if the first row is empty.
        Safe to call multiple times — only writes if headers missing.
        '''
        if not self._worksheet:
            return
        try:
            first_row = self._worksheet.row_values(1)
            if not first_row or first_row[0] != "Database Id":
                self._worksheet.insert_row(HEADER_ROW, index=1)
                log.info("GoogleSheetsProvider: headers written to row 1.")
        except Exception as e:
            log.warning(f"GoogleSheetsProvider: could not ensure headers: {e}")

    async def append_order(self, record) -> Optional[int]:
        '''
        Append one order as a new row in the sheet.

        Args:
            record: OrderRecord from the database.

        Returns:
            Google row number (1-based) where the order was written.
            None if append failed.
        '''
        if not self._connected or not self._worksheet:
            return None

        try:
            row_data = self._record_to_row(record)
            self._worksheet.append_row(
                row_data,
                value_input_option="USER_ENTERED",
                insert_data_option="INSERT_ROWS",
            )

            # Find the row we just appended by matching the database_id
            row_number = await self.find_by_database_id(record.order_id)

            log.info(
                f"GoogleSheetsProvider: appended order {record.order_id!r} "
                f"→ row {row_number}"
            )
            return row_number

        except Exception as e:
            log.error(
                f"GoogleSheetsProvider: append failed for "
                f"{record.order_id!r}: {e}"
            )
            raise

    async def update_order(self, google_row_id: int, record) -> bool:
        '''
        Update an existing row by Google row number.

        Args:
            google_row_id: Row number in the sheet (1-based).
            record:        OrderRecord with updated values.

        Returns:
            True on success, False on failure.
        '''
        if not self._connected or not self._worksheet:
            return False

        try:
            row_data = self._record_to_row(record)

            # Write the entire row at once
            col_end = chr(ord('A') + len(SHEET_COLUMNS) - 1)
            range_name = f"A{google_row_id}:{col_end}{google_row_id}"
            self._worksheet.update(range_name, [row_data])

            log.debug(
                f"GoogleSheetsProvider: updated row {google_row_id} "
                f"for order {record.order_id!r}"
            )
            return True

        except Exception as e:
            log.error(
                f"GoogleSheetsProvider: update failed for row "
                f"{google_row_id}: {e}"
            )
            raise

    async def batch_append(self, records: list) -> dict[str, int]:
        '''
        Append multiple orders in a single API call.
        Much more efficient than calling append_order() in a loop.

        Args:
            records: List of OrderRecord objects.

        Returns:
            Dict mapping order_id → google_row_id.
        '''
        if not self._connected or not self._worksheet or not records:
            return {}

        try:
            rows = [self._record_to_row(r) for r in records]
            self._worksheet.append_rows(
                rows,
                value_input_option="USER_ENTERED",
                insert_data_option="INSERT_ROWS",
            )

            # Re-query to get row IDs for all appended records
            result = {}
            for record in records:
                row_id = await self.find_by_database_id(record.order_id)
                if row_id:
                    result[record.order_id] = row_id

            log.info(
                f"GoogleSheetsProvider: batch appended "
                f"{len(records)} order(s)"
            )
            return result

        except Exception as e:
            log.error(f"GoogleSheetsProvider: batch append failed: {e}")
            raise

    async def find_by_database_id(self, order_id: str) -> Optional[int]:
        '''
        Find a row in the sheet by the database order_id (column A).
        Returns the 1-based row number, or None if not found.
        '''
        if not self._worksheet:
            return None
        try:
            cell = self._worksheet.find(order_id, in_column=1)
            return cell.row if cell else None
        except Exception:
            return None

    async def find_by_google_row(self, row_id: int) -> Optional[list]:
        '''
        Return all values in a row by row number.
        '''
        if not self._worksheet:
            return None
        try:
            return self._worksheet.row_values(row_id)
        except Exception:
            return None

    async def create_sheet_if_missing(self, sheet_name: str) -> None:
        '''Add a worksheet tab if it doesn't exist.'''
        if not self._spreadsheet:
            return
        try:
            existing = [ws.title for ws in self._spreadsheet.worksheets()]
            if sheet_name not in existing:
                self._spreadsheet.add_worksheet(
                    title=sheet_name, rows=1000, cols=len(SHEET_COLUMNS)
                )
                log.info(f"GoogleSheetsProvider: created sheet '{sheet_name}'")
        except Exception as e:
            log.warning(f"GoogleSheetsProvider: could not create sheet: {e}")

    async def _get_or_create_worksheet(self, sheet_name: str):
        '''Get existing worksheet or create it, then ensure headers.'''
        await self.create_sheet_if_missing(sheet_name)
        ws = self._spreadsheet.worksheet(sheet_name)
        # Ensure headers are written
        first_row = ws.row_values(1)
        if not first_row or first_row[0].lower().replace(" ", "_") != "database_id":
            ws.insert_row(HEADER_ROW, index=1)
            log.info(f"GoogleSheetsProvider: headers written to '{sheet_name}'")
        return ws

    def _record_to_row(self, record) -> list:
        '''
        Convert an OrderRecord to a list of cell values.
        Column order must match SHEET_COLUMNS exactly.
        '''
        def fmt_dt(dt) -> str:
            if dt is None:
                return ""
            return dt.strftime("%d/%m/%Y %H:%M") if hasattr(dt, 'strftime') else str(dt)

        def safe(val, default="") -> str:
            if val is None:
                return default
            return str(val)

        return [
            safe(record.order_id),
            safe(getattr(record, 'google_row_id', "")),
            safe(record.campaign),
            safe(record.package_name),
            safe(record.customer_name),
            safe(record.phone_number),
            safe(record.whatsapp_number),
            safe(record.delivery_address),
            safe(record.delivery_request),
            safe(record.order_date_raw),
            safe(record.customer_question),
            safe(record.worker_number),
            safe(record.assignment_status),
            safe(record.duplicate_status),
            "Yes" if record.is_valid else "No",
            f"{record.quality_score:.0%}" if record.quality_score is not None else "",
            safe(getattr(record, 'sync_status', 'PENDING')),
            fmt_dt(record.created_at),
            fmt_dt(record.updated_at),
        ]

    @property
    def is_connected(self) -> bool:
        return self._connected
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/sync/sync_service.py
# ================================================================
# PURPOSE:
#   Receives events from the main OMS pipeline and converts them
#   into SyncJob objects for the queue.
#   The only bridge between the OMS pipeline and the sync layer.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from app.oms.sync.sync_models import SyncJob, SyncOperation
from app.oms.sync.sync_queue import SynchronizationQueue
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class SyncService:
    '''
    Converts OMS domain events into synchronization jobs.
    Enqueues them into the SynchronizationQueue.

    Subscribes to:
        "order.persisted"   → INSERT_ORDER job
        "order.assigned"    → UPDATE_ORDER job
        "order.duplicate"   → UPDATE_ORDER job (duplicate status updated)

    Never calls Google directly. Never blocks the pipeline.
    All Google interaction is deferred to SyncWorker.

    Usage:
        service = SyncService(queue)
        service.register_listeners()
        # From now on, every order.persisted event auto-queues a sync job
    '''

    def __init__(self, queue: SynchronizationQueue, max_retries: int = 5):
        self._queue       = queue
        self._max_retries = max_retries
        self._enabled     = True

    def register_listeners(self) -> None:
        '''Register all event listeners. Call once at startup.'''

        @dispatcher.on("order.persisted")
        async def on_order_persisted(order_id: str, **kwargs):
            if not self._enabled:
                return
            await self._enqueue(order_id, SyncOperation.INSERT_ORDER)

        @dispatcher.on("assignment.resolved")
        async def on_assignment_resolved(order_id: str, **kwargs):
            if not self._enabled:
                return
            await self._enqueue(
                order_id, SyncOperation.UPDATE_ORDER, priority=8
            )

        @dispatcher.on("duplicate.confirmed")
        async def on_duplicate(order_id_a: str, **kwargs):
            if not self._enabled:
                return
            await self._enqueue(
                order_id_a, SyncOperation.UPDATE_ORDER, priority=9
            )

        @dispatcher.on("duplicate.likely")
        async def on_likely_dup(order_id_a: str, **kwargs):
            if not self._enabled:
                return
            await self._enqueue(
                order_id_a, SyncOperation.UPDATE_ORDER, priority=9
            )

        log.info("SyncService: event listeners registered.")

    async def _enqueue(
        self,
        order_id:  str,
        operation: SyncOperation,
        priority:  int = 10,
    ) -> None:
        '''Create and enqueue a SyncJob.'''
        job = SyncJob(
            order_id   =order_id,
            operation  =operation,
            max_retries=self._max_retries,
            priority   =priority,
        )
        enqueued = await self._queue.enqueue(job)
        if enqueued:
            log.debug(
                f"SyncService: queued {operation.value} for {order_id!r}"
            )
        else:
            log.warning(
                f"SyncService: queue full — {operation.value} for "
                f"{order_id!r} was NOT enqueued"
            )

    async def re_queue_pending(self, order_repo) -> int:
        '''
        On startup: re-queue any orders with sync_status=PENDING.
        These are orders that were saved to DB but not yet synced
        (e.g. OMS was restarted before sync completed).

        Returns the number of jobs re-queued.
        '''
        try:
            pending_orders = await order_repo.get_unsynced()
            count = 0
            for record in pending_orders:
                await self._enqueue(record.order_id, SyncOperation.INSERT_ORDER)
                count += 1

            if count:
                log.info(
                    f"SyncService: re-queued {count} unsynced order(s) "
                    "from previous session."
                )
            return count
        except Exception as e:
            log.warning(f"SyncService: could not re-queue pending: {e}")
            return 0

    def disable(self) -> None:
        '''Disable sync (e.g. during testing or when Google is unavailable).'''
        self._enabled = False
        log.info("SyncService: disabled.")

    def enable(self) -> None:
        self._enabled = True
        log.info("SyncService: enabled.")
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/sync/sync_worker.py
# ================================================================
# PURPOSE:
#   The background asyncio task that drains the SynchronizationQueue.
#   Processes one job at a time. Handles retries with backoff.
#   Saves google_row_id back to the database after successful append.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import asyncio

from app.oms.sync.sync_models import SyncJob, SyncOperation, SyncStatus
from app.oms.sync.sync_queue import SynchronizationQueue
from app.oms.sync.google_provider import GoogleSheetsProvider
from app.oms.sync.retry_policy import RetryPolicy
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class SynchronizationWorker:
    '''
    Background worker that processes the SynchronizationQueue.

    Runs as an asyncio task (asyncio.create_task(worker.run())).
    Dequeues one job at a time, processes it via GoogleSheetsProvider,
    handles errors and retries, saves google_row_id back to the DB.

    Shutdown: call worker.stop() → current job completes → task exits.

    Usage:
        worker = SynchronizationWorker(queue, provider, order_repo, retry_policy)
        task   = asyncio.create_task(worker.run(), name="sync_worker")
        # ...
        worker.stop()
        await task
    '''

    def __init__(
        self,
        queue:        SynchronizationQueue,
        provider:     GoogleSheetsProvider,
        order_repo,
        retry_policy: RetryPolicy,
    ):
        self._queue    = queue
        self._provider = provider
        self._repo     = order_repo
        self._policy   = retry_policy
        self._running  = False
        self._jobs_ok  = 0
        self._jobs_fail= 0

    async def run(self) -> None:
        '''
        Main worker loop. Runs until stop() is called.
        Blocks on queue.dequeue() when idle — zero CPU waste.
        '''
        self._running = True
        log.info("SyncWorker: started. Waiting for sync jobs...")

        while self._running:
            try:
                # Blocks until a job is available
                job = await asyncio.wait_for(
                    self._queue.dequeue(),
                    timeout=5.0   # Check _running every 5s
                )
                await self._process(job)
                self._queue.done()

            except asyncio.TimeoutError:
                continue  # No job available — check _running and loop

            except asyncio.CancelledError:
                break

            except Exception as e:
                log.error(f"SyncWorker: unexpected error: {e}", exc_info=True)
                await asyncio.sleep(1)

        log.info(
            f"SyncWorker: stopped. "
            f"Completed={self._jobs_ok}, Failed={self._jobs_fail}"
        )

    def stop(self) -> None:
        '''Signal the worker to stop after completing the current job.'''
        self._running = False
        log.info("SyncWorker: stop signal received.")

    async def _process(self, job: SyncJob) -> None:
        '''
        Execute one sync job. Handles success, retry, and permanent failure.
        '''
        job.mark_attempt()
        log.debug(f"SyncWorker: processing {job}")

        await dispatcher.emit(
            "sync.started",
            job_id  =job.job_id,
            order_id=job.order_id,
            op      =job.operation.value,
        )

        try:
            # Ensure Google connection is live before attempting
            if not self._provider.is_connected:
                reconnected = await self._provider.connect()
                if not reconnected:
                    raise ConnectionError("Google Sheets not available")

            # Execute the appropriate operation
            if job.operation == SyncOperation.INSERT_ORDER:
                await self._insert(job)

            elif job.operation == SyncOperation.UPDATE_ORDER:
                await self._update(job)

            elif job.operation == SyncOperation.BATCH_INSERT:
                await self._batch_insert(job)

            job.mark_completed()
            self._jobs_ok += 1

            # Update sync status in DB
            await self._repo.update_sync_status(
                job.order_id, "SYNCED", error=""
            )

            await dispatcher.emit(
                "sync.completed",
                job_id  =job.job_id,
                order_id=job.order_id,
            )
            log.info(
                f"SyncWorker: ✅ completed {job.operation.value} "
                f"for order {job.order_id!r}"
            )

        except Exception as e:
            await self._handle_failure(job, e)

    async def _insert(self, job: SyncJob) -> None:
        '''Insert order to Google Sheets and save the row ID.'''
        record = await self._repo.get_by_id(job.order_id)
        if not record:
            log.warning(
                f"SyncWorker: order {job.order_id!r} not found in DB — "
                "skipping insert"
            )
            return

        row_id = await self._provider.append_order(record)
        if row_id:
            await self._repo.update_google_row_id(job.order_id, row_id)
            log.info(
                f"SyncWorker: order {job.order_id!r} → "
                f"Google row {row_id}"
            )

    async def _update(self, job: SyncJob) -> None:
        '''Update existing Google Sheets row.'''
        record = await self._repo.get_by_id(job.order_id)
        if not record:
            return

        row_id = getattr(record, 'google_row_id', None)
        if not row_id:
            # Row not yet in Sheets — insert instead
            log.debug(
                f"SyncWorker: no google_row_id for {job.order_id!r} "
                "— inserting instead of updating"
            )
            await self._insert(job)
            return

        await self._provider.update_order(row_id, record)

    async def _batch_insert(self, job: SyncJob) -> None:
        '''Batch insert all order_ids from job.payload.'''
        order_ids = job.payload.get("order_ids", [])
        records   = []
        for oid in order_ids:
            r = await self._repo.get_by_id(oid)
            if r:
                records.append(r)

        if records:
            row_map = await self._provider.batch_append(records)
            for order_id, row_id in row_map.items():
                await self._repo.update_google_row_id(order_id, row_id)

    async def _handle_failure(self, job: SyncJob, error: Exception) -> None:
        '''
        Handle a failed sync attempt.
        Retries if policy allows, marks FAILED if exhausted.
        '''
        error_str = str(error)

        # Permanent failure — no point retrying
        if self._policy.is_permanent_failure(error):
            job.mark_failed(error_str)
            self._jobs_fail += 1
            log.error(
                f"SyncWorker: ❌ permanent failure for "
                f"{job.order_id!r}: {error_str}"
            )
            await self._repo.update_sync_status(
                job.order_id, "FAILED", error=error_str
            )
            await dispatcher.emit(
                "sync.failed",
                job_id   =job.job_id,
                order_id =job.order_id,
                error    =error_str,
                permanent=True,
            )
            return

        # Transient failure — retry if we have attempts remaining
        if self._policy.should_retry(job):
            delay = self._policy.next_delay(job)
            job.mark_retry(error_str)
            self._jobs_fail += 1  # Count as failure for this attempt

            log.warning(
                f"SyncWorker: retry {job.retry_count}/{job.max_retries} "
                f"for {job.order_id!r} in {delay:.0f}s: {error_str}"
            )

            await dispatcher.emit(
                "sync.retry",
                job_id     =job.job_id,
                order_id   =job.order_id,
                retry_count=job.retry_count,
                delay_s    =delay,
            )

            await self._repo.update_sync_status(
                job.order_id, "RETRYING", error=error_str
            )

            # Wait then re-enqueue
            await self._policy.wait(delay)
            await self._queue.enqueue_for_retry(job)

        else:
            # Retries exhausted
            job.mark_failed(error_str)
            self._jobs_fail += 1
            log.error(
                f"SyncWorker: ❌ max retries exhausted for "
                f"{job.order_id!r}: {error_str}"
            )
            await self._repo.update_sync_status(
                job.order_id, "FAILED", error=error_str
            )
            await dispatcher.emit(
                "sync.failed",
                job_id   =job.job_id,
                order_id =job.order_id,
                error    =error_str,
                permanent=False,
            )

    def stats(self) -> dict:
        return {
            "running":    self._running,
            "jobs_ok":    self._jobs_ok,
            "jobs_failed":self._jobs_fail,
            "queue_size": self._queue.size,
        }
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/sync/sync_events.py
# ================================================================
# PURPOSE:
#   Documents all sync-related events emitted by the system.
#   Not executable — serves as a registry for event names.
# ================================================================
# ==============================================================

"""
# Sync event name constants
# Import and use these instead of raw strings to prevent typos

SYNC_CREATED   = "sync.created"
SYNC_STARTED   = "sync.started"
SYNC_COMPLETED = "sync.completed"
SYNC_FAILED    = "sync.failed"
SYNC_RETRY     = "sync.retry"

# Order persisted event (emitted by repository after DB save)
ORDER_PERSISTED = "order.persisted"
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/sync/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.sync.sync_models import SyncJob, SyncOperation, SyncStatus
from app.oms.sync.sync_queue import SynchronizationQueue
from app.oms.sync.retry_policy import RetryPolicy
from app.oms.sync.google_provider import GoogleSheetsProvider
from app.oms.sync.sync_service import SyncService
from app.oms.sync.sync_worker import SynchronizationWorker

__all__ = [
    "SyncJob", "SyncOperation", "SyncStatus",
    "SynchronizationQueue",
    "RetryPolicy",
    "GoogleSheetsProvider",
    "SyncService",
    "SynchronizationWorker",
]
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/app/oms/infrastructure/persistence/schema.py
# ================================================================
# ADD ONLY — four new columns on OrderRecord.
# Do NOT rewrite the schema — just add these fields.
#
# FIND the OrderRecord class and ADD these columns after
# the existing "updated_at" column:
# ================================================================
# ==============================================================

"""
# ADD these four columns to the OrderRecord class in schema.py
# Place them after the existing "updated_at" column:

    # Google Sheets sync fields (added Day 10.5)
    google_row_id   = Column(Integer, nullable=True)   # Row number in sheet
    sync_status     = Column(String,  default="PENDING", nullable=False)
    last_sync_time  = Column(DateTime, nullable=True)
    last_sync_error = Column(Text,    nullable=True)

# ALSO ADD this index to __table_args__:
    Index("ix_orders_sync_status", "sync_status"),
"""

# DATABASE MIGRATION:
# These columns are added with ALTER TABLE automatically by SQLAlchemy
# on next startup IF using SQLite (SQLite supports ADD COLUMN).
# For production PostgreSQL, use Alembic migrations.
#
# For SQLite (development), add this to .init():
#
#   def _apply_migrations(self) -> None:
#       '''Add new columns to existing tables if missing.'''
#       from sqDatabaselalchemy import text, inspect
#       with self._engine.connect() as conn:
#           inspector = inspect(self._engine)
#           existing  = [col['name'] for col in
#                        inspector.get_columns('orders')]
#           new_cols  = {
#               'google_row_id':  'ALTER TABLE orders ADD COLUMN google_row_id INTEGER',
#               'sync_status':    "ALTER TABLE orders ADD COLUMN sync_status TEXT DEFAULT 'PENDING'",
#               'last_sync_time': 'ALTER TABLE orders ADD COLUMN last_sync_time DATETIME',
#               'last_sync_error':'ALTER TABLE orders ADD COLUMN last_sync_error TEXT',
#           }
#           for col_name, sql in new_cols.items():
#               if col_name not in existing:
#                   conn.execute(text(sql))
#                   conn.commit()
#                   log.info(f"Migration: added column {col_name!r} to orders")


# ==============================================================
# ================================================================
#  FILE 10
#  PATH: windwhirl/app/oms/infrastructure/persistence/order_repository.py
# ================================================================
# ADD these methods to the existing OrderRepository class.
# Do NOT rewrite the file — just append these methods.
# ================================================================
# ==============================================================

"""
# ADD to OrderRepository class:

    async def update_google_row_id(
        self,
        order_id:     str,
        google_row_id: int,
    ) -> None:
        '''Save the Google Sheets row number after successful append.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return
            record.google_row_id  = google_row_id
            record.sync_status    = "SYNCED"
            record.last_sync_time = datetime.now()
            record.last_sync_error = None
            session.commit()
            self._log.debug(
                f"OrderRepository: order {order_id!r} → "
                f"google_row_id={google_row_id}"
            )

    async def update_sync_status(
        self,
        order_id: str,
        status:   str,
        error:    str = "",
    ) -> None:
        '''Update sync_status and optionally the last_sync_error.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return
            record.sync_status     = status
            record.last_sync_time  = datetime.now()
            record.last_sync_error = error or None
            session.commit()

    async def get_unsynced(self, limit: int = 100) -> list:
        '''
        Return orders where sync_status is PENDING or RETRYING.
        Used on startup to re-queue incomplete sync jobs.
        '''
        with self._sf() as session:
            return (
                session.query(OrderRecord)
                       .filter(OrderRecord.sync_status.in_(["PENDING", "RETRYING"]))
                       .order_by(OrderRecord.created_at.asc())
                       .limit(limit)
                       .all()
            )

    async def emit_persisted(self, order_id: str) -> None:
        '''
        Emit the "order.persisted" event after saving.
        Called at the end of save_validated_order().
        '''
        from app.oms.events import dispatcher
        await dispatcher.emit("order.persisted", order_id=order_id)
"""

# NOTE: Add emit_persisted() call at end of save_validated_order():
#
#   async def save_validated_order(self, validated_order) -> str:
#       ...
#       session.commit()
#       await self.emit_persisted(parsed.order_id)   # ← ADD THIS
#       return parsed.order_id


# ==============================================================
# ================================================================
#  FILE 11
#  PATH: windwhirl/app/oms/config/settings.py
# ================================================================
# ADD GoogleSettings dataclass and add to OMSSettings.
# ================================================================
# ==============================================================

"""
# ADD after SheetsSettings (or replace it):

@dataclass
class GoogleSettings:
    '''
    Google Sheets synchronization configuration.

    enabled:           Set to True to activate sync. False to disable entirely.
    credentials_file:  Path to the Google Service Account JSON key file.
                       Download from Google Cloud Console → IAM → Service Accounts.
                       Share the spreadsheet with the service account email.
    spreadsheet_id:    The long ID from the spreadsheet URL:
                       https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit
    sheet_name:        Tab name inside the spreadsheet. Default: "Orders"
    retry_limit:       Maximum sync attempts before marking FAILED. Default: 5.
    retry_interval:    Base seconds between retries (exponential). Default: 5.
    batch_size:        Max orders per batch append call. Default: 50.
    queue_max_size:    Max jobs in the sync queue. Default: 500.
    '''
    enabled:          bool  = False        # Off by default until credentials provided
    credentials_file: str   = "config/google_credentials.json"
    spreadsheet_id:   str   = ""
    sheet_name:       str   = "Orders"
    retry_limit:      int   = 5
    retry_interval:   float = 5.0
    batch_size:       int   = 50
    queue_max_size:   int   = 500

# ADD to OMSSettings dataclass (replace sheets: SheetsSettings):
    google: GoogleSettings = field(default_factory=GoogleSettings)

# ADD to _load_from_env() env_map:
    "OMS_GOOGLE_ENABLED":           ("google.enabled",          bool),
    "OMS_GOOGLE_CREDENTIALS_FILE":  ("google.credentials_file", str),
    "OMS_GOOGLE_SPREADSHEET_ID":    ("google.spreadsheet_id",   str),
    "OMS_GOOGLE_SHEET_NAME":        ("google.sheet_name",       str),
    "OMS_GOOGLE_RETRY_LIMIT":       ("google.retry_limit",      int),
    "OMS_GOOGLE_RETRY_INTERVAL":    ("google.retry_interval",   float),
    "OMS_GOOGLE_BATCH_SIZE":        ("google.batch_size",       int),
    "OMS_GOOGLE_QUEUE_MAX_SIZE":    ("google.queue_max_size",   int),
"""


# ==============================================================
# ================================================================
#  FILE 12
#  PATH: windwhirl/oms_runner.py  (ADD sync wiring)
# ================================================================
# ADD these blocks to the existing oms_runner.py.
# Place them in the positions indicated.
# ================================================================
# ==============================================================

"""
# 1. ADD imports at the top:
from app.oms.sync import (
    SynchronizationQueue, RetryPolicy,
    GoogleSheetsProvider, SyncService, SynchronizationWorker,
)

# 2. ADD build_sync() function after build_persistence():
def build_sync(settings, order_repo):
    '''Wire the complete Google Sheets sync layer.'''
    google_cfg = settings.google

    queue    = SynchronizationQueue(max_size=google_cfg.queue_max_size)
    policy   = RetryPolicy(
        base_interval=google_cfg.retry_interval,
        max_interval =300.0,
        max_retries  =google_cfg.retry_limit,
    )
    provider = GoogleSheetsProvider(settings)
    service  = SyncService(queue, max_retries=google_cfg.retry_limit)
    worker   = SynchronizationWorker(queue, provider, order_repo, policy)

    return queue, provider, service, worker


# 3. ADD sync event listeners:
@dispatcher.on("sync.completed")
async def on_sync_completed(**kwargs):
    log.info(
        f"Sync completed: order {kwargs.get('order_id')!r} "
        f"→ Google Sheets"
    )

@dispatcher.on("sync.failed")
async def on_sync_failed(**kwargs):
    log.error(
        f"Sync FAILED: order {kwargs.get('order_id')!r} | "
        f"error: {kwargs.get('error')!r} | "
        f"permanent: {kwargs.get('permanent')}"
    )

@dispatcher.on("sync.retry")
async def on_sync_retry(**kwargs):
    log.warning(
        f"Sync retry #{kwargs.get('retry_count')} for "
        f"order {kwargs.get('order_id')!r} "
        f"in {kwargs.get('delay_s', 0):.0f}s"
    )


# 4. In main(), after build_persistence():
#
#   sync_task = None
#   if settings.google.enabled:
#       queue, provider, service, worker = build_sync(settings, order_repo)

#       # Connect to Google Sheets
#       connected = await provider.connect()
#       if connected:
#           await provider.ensure_headers()

#       # Register event listeners
#       service.register_listeners()

#       # Re-queue any unsynced orders from previous session
#       await service.re_queue_pending(order_repo)

#       # Start background worker
#       sync_task = asyncio.create_task(worker.run(), name="sync_worker")
#       log.info("Google Sheets sync layer active.")
#   else:
#       log.info(
#           "Google Sheets sync disabled "
#           "(set OMS_GOOGLE_ENABLED=true to enable)."
#       )

#
# 5. In main() finally block (graceful shutdown):
#
#   # Stop sync worker
#   if sync_task and not sync_task.done():
#       worker.stop()
#       try:
#           await asyncio.wait_for(sync_task, timeout=30.0)
#       except asyncio.TimeoutError:
#           log.warning("Sync worker did not stop cleanly — cancelling.")
#           sync_task.cancel()
#
#   # Disconnect Google
#   if settings.google.enabled:
#       await provider.disconnect()
"""


# ==============================================================
# SETUP GUIDE — Google Service Account
# ==============================================================
#
# Follow these steps once to set up Google Sheets access:
#
# STEP 1: Create a Google Cloud Project
#   → console.cloud.google.com
#   → New Project → "windwhirl-oms"
#
# STEP 2: Enable Google Sheets API
#   → APIs & Services → Library
#   → Search "Google Sheets API" → Enable
#   → Also enable "Google Drive API"
#
# STEP 3: Create Service Account
#   → APIs & Services → Credentials → Create Credentials
#   → Service Account → Name: "oms-sync"
#   → Create and Continue → Done
#
# STEP 4: Download JSON Key
#   → Click the service account → Keys tab
#   → Add Key → Create new key → JSON → Download
#   → Save to: windwhirl/config/google_credentials.json
#   → NEVER commit this file to git
#   → Add to .gitignore: config/google_credentials.json
#
# STEP 5: Share Spreadsheet
#   → Open your Google Sheet
#   → Share → Add the service account email
#     (looks like: oms-sync@windwhirl-oms.iam.gserviceaccount.com)
#   → Give "Editor" access → Done
#
# STEP 6: Configure OMS
#   Set in settings.py or environment variables:
#     OMS_GOOGLE_ENABLED=true
#     OMS_GOOGLE_CREDENTIALS_FILE=config/google_credentials.json
#     OMS_GOOGLE_SPREADSHEET_ID=<your spreadsheet ID from URL>
#     OMS_GOOGLE_SHEET_NAME=Orders
#
# STEP 7: Verify
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   import asyncio
#   from app.oms.config.settings import get_settings
#   from app.oms.sync.google_provider import GoogleSheetsProvider
#
#   async def run():
#       settings = get_settings()
#       provider = GoogleSheetsProvider(settings)
#       ok = await provider.connect()
#       print('Connected:', ok)
#       if ok:
#           alive = await provider.ping()
#           print('Ping:', alive)
#           await provider.ensure_headers()
#           print('Headers verified.')
#       await provider.disconnect()
#
#   asyncio.run(run())
#   "
#
# ==============================================================
# DAY 10.5 VERIFICATION
# ==============================================================
#
# Test 1 — Imports (no Google credentials needed):
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.sync import (
#       SyncJob, SyncOperation, SyncStatus,
#       SynchronizationQueue, RetryPolicy,
#       SyncService, SynchronizationWorker,
#   )
#   print('All Day 10.5 imports OK')
#   "
#
# Test 2 — Queue logic (no Google needed):
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.sync.sync_queue import SynchronizationQueue
#   from app.oms.sync.sync_models import SyncJob, SyncOperation
#
#   async def run():
#       q   = SynchronizationQueue(max_size=10)
#       job = SyncJob(order_id='ORD-001', operation=SyncOperation.INSERT_ORDER)
#       ok  = await q.enqueue(job)
#       assert ok
#       out = await q.dequeue()
#       assert out.order_id == 'ORD-001'
#       print('Queue OK:', out)
#       print('Stats:', q.stats())
#
#   asyncio.run(run())
#   "
#
# Test 3 — Retry policy:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.sync.retry_policy import RetryPolicy
#   from app.oms.sync.sync_models import SyncJob, SyncOperation, SyncStatus
#
#   job    = SyncJob('ORD-001', SyncOperation.INSERT_ORDER)
#   policy = RetryPolicy(base_interval=5, max_interval=300, max_retries=5)
#
#   for i in range(6):
#       delay = policy.next_delay(job)
#       retry = policy.should_retry(job)
#       print(f'Attempt {i}: delay={delay:.0f}s, can_retry={retry}')
#       job.retry_count += 1
#   "
#
# Test 4 — Full connection test (requires credentials):
#   python -c "
#   import sys, asyncio; sys.path.insert(0, '.')
#   from app.oms.config.settings import get_settings
#   from app.oms.sync.google_provider import GoogleSheetsProvider
#
#   async def run():
#       cfg      = get_settings()
#       provider = GoogleSheetsProvider(cfg)
#       ok       = await provider.connect()
#       print('Connected:', ok)
#       if ok:
#           print('Ping:', await provider.ping())
#           await provider.ensure_headers()
#       await provider.disconnect()
#
#   asyncio.run(run())
#   "
#
# ==============================================================
# WHAT PHASE TWO BUILDS
# ==============================================================
# Phase Two begins with FastAPI REST API:
#   GET  /orders          → list with filters
#   GET  /orders/{id}     → single order detail
#   POST /orders/{id}/assign → manual assignment
#   GET  /workers/{number}/orders → orders by worker
#   GET  /reports/daily   → trigger Excel export
#   GET  /health          → system health check
#   WS   /live            → WebSocket for real-time order updates
#
# The existing event bus and repository layer are the backend.
# FastAPI just exposes them over HTTP.
# No business logic changes required.
# ==============================================================