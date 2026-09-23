from __future__ import annotations

from apps.oms.sync.sync_models import SyncJob, SyncOperation
from apps.oms.sync.sync_queue import SynchronizationQueue
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

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
