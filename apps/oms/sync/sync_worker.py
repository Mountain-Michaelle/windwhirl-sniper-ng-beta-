from __future__ import annotations

import asyncio

from apps.oms.sync.sync_models import SyncJob, SyncOperation, SyncStatus
from apps.oms.sync.sync_queue import SynchronizationQueue
from apps.oms.sync.google_provider import GoogleSheetsProvider
from apps.oms.sync.retry_policy import RetryPolicy
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger
from apps.oms.sync.sync_models import SchemaMismatchError
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from apps.oms.infrastructure.persistence.order_repository import OrderRepository
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
        order_repo:     OrderRepository,
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
                
            elif job.operation == SyncOperation.ARCHIVE_ORDER:
                await self._repo.mark_archived(job.order_id)  # soft delete, always    

            
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
        await self._assert_schema_ok()
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
        await self._assert_schema_ok()
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

    async def _assert_schema_ok(self) -> None:
        '''Raises SchemaMismatchError if the sheet's header row has
        drifted from ALL_COLUMNS — e.g. a column was deleted/renamed.'''
        is_valid, missing = await self._provider.validate_headers()
        if not is_valid:
            raise SchemaMismatchError(f"Sheet header mismatch — missing/renamed: {missing}")
    
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
        
        if isinstance(error, SchemaMismatchError):
            job.mark_halted(str(error))
            await self._repo.update_sync_status(job.order_id, "HALTED", error=str(error))
            log.error(f"SyncWorker: HALTED — schema mismatch: {error}")
            return
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
