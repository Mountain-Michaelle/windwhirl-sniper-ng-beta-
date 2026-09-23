# ==============================================================
# PATH SETUP — MUST BE FIRST
# ==============================================================
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# ==============================================================

import asyncio
import os

from apps.oms.config.settings import get_settings
from apps.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from apps.oms.infrastructure.browser.raw_message import RawMessage
from apps.oms.infrastructure.browser.message_cache import MessageCache
from apps.oms.infrastructure.browser.checkpoint_store import CheckpointStore
from apps.oms.infrastructure.browser.recovery_manager import RecoveryManager
from apps.oms.infrastructure.browser.dom_observer import DOMObserver
from apps.oms.application.classifier import MessageClassifier, MessageClass
from apps.oms.application.parser import OrderParser
from apps.oms.application.validator import OrderValidator
from apps.oms.application.assignment_engine import SingleStaffAssignmentEngine
from apps.oms.application.pipeline import MessagePipeline
from apps.oms.domain.entities import Order, Staff
from apps.oms.shared.logger import get_logger
from apps.oms.events import dispatcher
from apps.oms.infrastructure.persistence import (
    Database, OrderRepository, AssignmentRepository,
    DuplicateRepository, DbDuplicateStore, ExcelExporter,
)
from apps.oms.application.models.validated_order import ValidatedOrder
from apps.oms.application.models.duplicate_group import DuplicateGroup
from apps.oms.application.duplicate.duplicate_detection_engine import DuplicateDetectionEngine
from apps.oms.application.assignment_state_engine import AssignmentStateEngine
from apps.oms.application.assignment_resolution_engine import AssignmentResolutionEngine

# ── Sync layer (Day 10.5 outbound + Day 10.6 inbound) ─────────────
# Previously imported nowhere in this file — save_validated_order()
# emits "order.persisted" but with no SyncService listening, and no
# worker draining the queue, nothing ever reached Google Sheets. The
# inbound (Sheet → DB) side was entirely absent as well.
from apps.oms.sync import (
    SynchronizationQueue, RetryPolicy,
    GoogleSheetsProvider, SyncService, SynchronizationWorker,
)
from apps.oms.sync.inbound_sync_service import InboundSyncTrigger, InboundSyncProcessor
from apps.oms.sync.reconciliation import ReconciliationService

log = get_logger("oms.runner")

# ── Module-level state ────────────────────────────────────────────
# These are module-level so the "browser.reconnected" handler can
# cancel and replace the observer task without a closure mess.
_observer_task:    asyncio.Task = None
_settings          = None
_bootstrap         = None
_pipeline          = None
_order_repo        = None
_duplicate_engine  = None

# ── Sync layer state ───────────────────────────────────────────────
_sync_queue        = None   # SynchronizationQueue  (outbound, DB → Sheet)
_sync_provider      = None   # GoogleSheetsProvider  (shared by both directions)
_sync_service        = None   # SyncService           (order.persisted → job)
_sync_worker           = None   # SynchronizationWorker (drains _sync_queue)
_sync_worker_task        = None   # asyncio.Task for the outbound worker

_inbound_trigger           = None   # InboundSyncTrigger    (WHEN to pull sheet edits)
_inbound_processor           = None   # InboundSyncProcessor  (HOW to apply them safely)
_reconciler                    = None   # ReconciliationService
_inbound_task                     = None   # asyncio.Task for the inbound trigger poll loop


# ── Staff directory and pipeline builders ─────────────────────────

def build_staff_directory(settings) -> dict:
    staff = Staff(
        number      =settings.whatsapp.staff_number,
        group_name  =settings.whatsapp.group_name,
        display_name=getattr(settings.whatsapp, "staff_display_name", "staff"),
    )
    return {staff.display_name.lower(): staff}


def build_pipeline(settings) -> MessagePipeline:
    staff = Staff(
        number      =settings.whatsapp.staff_number,
        group_name  =settings.whatsapp.group_name,
        display_name=getattr(settings.whatsapp, "staff_display_name", "staff"),
    )
    return MessagePipeline(
        classifier       =MessageClassifier(),
        parser           =OrderParser(staff_number=staff.number),
        validator        =OrderValidator(),
        assignment_engine=SingleStaffAssignmentEngine(staff),
        state_engine     =AssignmentStateEngine(build_staff_directory(settings)),
        staff            =staff,
    )


def build_persistence(settings):
    db              = Database(settings.storage.database_url)
    db.init()
    sf              = db.session_factory
    order_repo      = OrderRepository(sf)
    assignment_repo = AssignmentRepository(sf)
    duplicate_repo  = DuplicateRepository(sf)
    db_store        = DbDuplicateStore(
        order_repo     =order_repo,
        duplicate_repo =duplicate_repo,
        window_hours   =48.0,
    )
    dup_engine      = DuplicateDetectionEngine(window_hours=48.0)
    dup_engine._store = db_store
    excel           = ExcelExporter(order_repo)
    log.info(f"Persistence initialized: {settings.storage.database_url}")
    return db, order_repo, assignment_repo, duplicate_repo, dup_engine, excel


# ── Sync layer builders ─────────────────────────────────────────────

def build_sync(settings, order_repo):
    '''
    Wire the outbound (DB → Sheet) half of the sync layer.
    Returns the queue/provider/service/worker so main() can start
    the worker task and register listeners after connecting.
    '''
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


def build_inbound_sync(provider, order_repo):
    '''
    Wire the inbound (Sheet → DB) half of the sync layer — the safe
    sync-back that resolves rows by hidden row_key, rejects tampered
    IDs, soft-deletes only on explicit DELETE, and enforces the
    Scheduled-requires-date rule on every run.
    '''
    processor  = InboundSyncProcessor(provider, order_repo)
    trigger    = InboundSyncTrigger(provider)
    reconciler = ReconciliationService(provider, order_repo)
    return processor, trigger, reconciler


# ── Sync event listeners ────────────────────────────────────────────

@dispatcher.on("sync.completed")
async def on_sync_completed(**kwargs):
    log.info(f"Sync completed: order {kwargs.get('order_id')!r} → Google Sheets")


@dispatcher.on("sync.failed")
async def on_sync_failed(**kwargs):
    log.error(
        f"Sync FAILED: order {kwargs.get('order_id')!r} | "
        f"error: {kwargs.get('error')!r} | permanent: {kwargs.get('permanent')}"
    )


@dispatcher.on("sync.retry")
async def on_sync_retry(**kwargs):
    log.warning(
        f"Sync retry #{kwargs.get('retry_count')} for order "
        f"{kwargs.get('order_id')!r} in {kwargs.get('delay_s', 0):.0f}s"
    )


# ── Observer lifecycle helper ─────────────────────────────────────

async def _start_observer(page, group_name: str) -> asyncio.Task:
    '''
    Open the target group (if not already there), run recovery,
    inject MutationObserver, and return the running observer task.

    Called:
      - On initial startup
      - After every browser.reconnected event

    Returns the new asyncio.Task for the DOMObserver.
    '''
    global _settings

    # Open the target group on the current page
    opened = await _bootstrap.session_manager.open_target_group(group_name)
    if not opened:
        log.warning(
            f"Could not open group {group_name!r} — "
            "observer will not start until group is found."
        )
        return None

    checkpoint_store = CheckpointStore(
        group_name  =group_name,
        data_dir    ="data",
        max_history =_settings.observer.checkpoint_history_size,
    )
    cache = MessageCache(max_size=_settings.observer.message_cache_size)

    # Recovery runs once — catches messages missed while offline/reconnecting
    recovery = RecoveryManager(
        page             =page,
        checkpoint_store =checkpoint_store,
        cache            =cache,
        cfg              =_settings,
    )
    await recovery.run()

    # Start fresh DOM observer with the current (new) page
    observer = DOMObserver(
        page             =page,
        cache            =cache,
        checkpoint_store =checkpoint_store,
        cfg              =_settings,
    )
    task = asyncio.create_task(observer.run(), name="oms_dom_observer")
    log.info(
        f"DOM Observer (re)started on group {group_name!r}. "
        f"MutationObserver active."
    )
    return task


# ── Reconnect handler ─────────────────────────────────────────────

@dispatcher.on("browser.reconnected")
async def on_browser_reconnected(new_page=None, **kwargs):
    '''
    Called by BrowserHealthCheck after a successful reconnect.

    Steps:
      1. Cancel the old (dead) observer task
      2. Get the new page from the session manager
      3. Re-open the target group
      4. Restart the DOM Observer with the new page
    '''
    global _observer_task, _bootstrap, _settings

    log.info("Handling browser reconnect — restarting observer...")

    # Cancel the old dead observer task
    if _observer_task and not _observer_task.done():
        _observer_task.cancel()
        try:
            await _observer_task
        except asyncio.CancelledError:
            pass
        log.info("Old observer task cancelled.")

    # Get the fresh page from the session manager
    page = _bootstrap.session_manager.page
    if not page:
        log.error(
            "Reconnect handler: no page available from session manager. "
            "Observer cannot restart."
        )
        return

    group_name = _settings.whatsapp.group_name
    if not group_name:
        log.warning("No group_name configured — observer not restarted.")
        return

    # Small pause — let the new page fully settle before navigating
    await asyncio.sleep(3)

    # Restart observer on the new page
    _observer_task = await _start_observer(page, group_name)

    if _observer_task:
        log.info(
            "Observer successfully restarted after reconnect. "
            "Monitoring resumed."
        )
    else:
        log.error(
            "Observer failed to restart after reconnect. "
            "Check group name and browser state."
        )


# ── Standard event listeners ──────────────────────────────────────

@dispatcher.on("browser.connected")
async def on_browser_connected(**kwargs):
    log.info(f"Browser connected — {kwargs.get('state')}")


@dispatcher.on("browser.disconnected")
async def on_browser_disconnected(**kwargs):
    log.warning(
        f"Browser disconnected — {kwargs.get('reason', '?')} | "
        f"Reconnect will be attempted automatically."
    )


@dispatcher.on("recovery.completed")
async def on_recovery_completed(**kwargs):
    log.info(
        f"Recovery complete — "
        f"{kwargs.get('recovered_count', 0)} message(s) replayed"
    )


@dispatcher.on("observer.started")
async def on_observer_started(**kwargs):
    log.info(
        f"Observer active — watching: {kwargs.get('group')!r}\\n"
        "Waiting for new messages..."
    )


@dispatcher.on("message.classified")
async def on_classified(**kwargs):
    log.debug(
        f"Classified: {kwargs.get('cls')} "
        f"({kwargs.get('confidence', 0):.2f})"
    )


@dispatcher.on("order.detected")
async def on_order_detected(order, **kwargs):
    log.info(
        f"\\n{'=' * 50}\\n"
        f"  NEW ORDER DETECTED\\n"
        f"  ID:       {order.order_id}\\n"
        f"  Customer: {order.customer_name}\\n"
        f"  Status:   {order.status.value}\\n"
        f"{'=' * 50}"
    )


@dispatcher.on("order.validated")
async def on_order_validated(validated_order: ValidatedOrder, **kwargs):
    global _order_repo
    try:
        # This call now emits "order.persisted" internally, which is
        # what SyncService listens for to queue the outbound sync job.
        await _order_repo.save_validated_order(validated_order)
    except Exception as e:
        log.error(f"Persistence: failed to save order: {e}", exc_info=True)


@dispatcher.on("assignment.resolved")
async def on_assignment_resolved_persist(**kwargs):
    global _order_repo
    order_id      = kwargs.get("order_id")
    worker_number = kwargs.get("worker_number")
    try:
        await _order_repo.update_assignment(order_id, worker_number)
        log.info(f"Persistence: assignment saved for order {order_id!r}")
    except Exception as e:
        log.error(f"Persistence: assignment save failed: {e}", exc_info=True)


@dispatcher.on("duplicate.confirmed")
async def on_duplicate_confirmed(**kwargs):
    global _order_repo
    try:
        await _order_repo.update_duplicate_status(
            kwargs.get("order_id_a"), "CONFIRMED_DUPLICATE", kwargs.get("group_id", "")
        )
    except Exception as e:
        log.error(f"Persistence: duplicate update failed: {e}", exc_info=True)


@dispatcher.on("order.validated")
async def check_duplicate(validated_order: ValidatedOrder, **kwargs):
    global _duplicate_engine
    try:
        await _duplicate_engine.check(validated_order)
    except Exception as e:
        log.error(f"Duplicate check failed: {e}", exc_info=True)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    global _observer_task, _settings, _bootstrap, _pipeline
    global _order_repo, _duplicate_engine
    global _sync_queue, _sync_provider, _sync_service, _sync_worker, _sync_worker_task
    global _inbound_trigger, _inbound_processor, _reconciler, _inbound_task

    log.info("Windwhirl OMS starting...")

    _settings = get_settings()

    # Persistence
    db, _order_repo, assignment_repo, duplicate_repo, _duplicate_engine, exporter = (
        build_persistence(_settings)
    )

    # ── Sync layer (Google Sheets) ──────────────────────────────────
    # Both directions share one provider/connection. If Google is
    # disabled or unreachable, OMS keeps running WhatsApp intake and
    # the DB normally — sync is additive, never a hard dependency.
    if getattr(_settings, "google", None) and _settings.google.enabled:
        _sync_queue, _sync_provider, _sync_service, _sync_worker = build_sync(
            _settings, _order_repo
        )

        connected = await _sync_provider.connect()
        if connected:
            await _sync_provider.ensure_headers()
            await _sync_provider.ensure_field_protection()

            # Outbound: order.persisted / assignment.resolved / duplicate.*
            # → queued as SyncJobs
            _sync_service.register_listeners()

            # Recover any orders that didn't finish syncing before the
            # last restart (sync_status still PENDING/RETRYING)
            await _sync_service.re_queue_pending(_order_repo)

            _sync_worker_task = asyncio.create_task(
                _sync_worker.run(), name="oms_sync_worker"
            )
            log.info("Outbound sync (DB → Sheet) active.")

            # Inbound: worker edits in the sheet → safely applied to DB
            _inbound_processor, _inbound_trigger, _reconciler = build_inbound_sync(
                _sync_provider, _order_repo
            )

            async def _on_inbound_trigger(source):
                summary = await _inbound_processor.run_once(source)
                if summary.halted:
                    log.error(f"Inbound sync halted: {summary.halt_reason}")
                else:
                    log.info(
                        f"Inbound sync ({source.value}): "
                        f"synced={summary.synced} archived={summary.archived} "
                        f"flagged={summary.flagged} errors={summary.errored}"
                    )
                await _reconciler.run()
                return summary

            _inbound_task = asyncio.create_task(
                _inbound_trigger.poll_loop(_on_inbound_trigger), name="oms_inbound_sync"
            )
            log.info("Inbound sync (Sheet → DB) active.")
        else:
            log.warning(
                "Google Sheets connection failed — sync layer disabled for "
                "this run. OMS will continue without it."
            )
    else:
        log.info(
            "Google Sheets sync disabled "
            "(set OMS_GOOGLE_ENABLED=true / settings.google.enabled to enable)."
        )

    # Pipeline
    _pipeline = build_pipeline(_settings)

    # Browser
    _bootstrap = BrowserBootstrap(_settings)

    # Message received → pipeline
    @dispatcher.on("message.received")
    async def handle_message(message: RawMessage, **kwargs):
        await _pipeline.process(message)

    @dispatcher.on("message.recovered")
    async def handle_recovered(message: RawMessage, **kwargs):
        await _pipeline.process(message)

    try:
        # Start browser (QR or session restore)
        await _bootstrap.start()

        group_name = _settings.whatsapp.group_name
        if not group_name:
            log.warning("whatsapp.group_name not configured.")
            await _bootstrap.run_forever()
            return

        # Initial observer start
        page = _bootstrap.page
        _observer_task = await _start_observer(page, group_name)

        # Block here — health check runs in background and handles reconnects
        # via the "browser.reconnected" event handler above
        await _bootstrap.run_forever()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down.")
    except Exception as e:
        log.error(f"OMS runner error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if _observer_task and not _observer_task.done():
            _observer_task.cancel()
            try:
                await _observer_task
            except asyncio.CancelledError:
                pass

        # Stop inbound sync trigger polling first (it calls the processor,
        # which calls the provider — stop the "outer" loop before tearing
        # down the connection it depends on).
        if _inbound_task and not _inbound_task.done():
            _inbound_trigger.stop()
            try:
                await asyncio.wait_for(_inbound_task, timeout=15.0)
            except asyncio.TimeoutError:
                log.warning("Inbound sync task did not stop cleanly — cancelling.")
                _inbound_task.cancel()

        # Stop the outbound sync worker
        if _sync_worker_task and not _sync_worker_task.done():
            _sync_worker.stop()
            try:
                await asyncio.wait_for(_sync_worker_task, timeout=30.0)
            except asyncio.TimeoutError:
                log.warning("Sync worker did not stop cleanly — cancelling.")
                _sync_worker_task.cancel()

        # Disconnect Google last, once nothing else is using it
        if _sync_provider is not None:
            await _sync_provider.disconnect()

        if _pipeline:
            log.info(f"Pipeline stats: {_pipeline.stats()}")

        await _bootstrap.stop()
        log.info("Windwhirl OMS stopped.")


if __name__ == "__main__":
    asyncio.run(main())