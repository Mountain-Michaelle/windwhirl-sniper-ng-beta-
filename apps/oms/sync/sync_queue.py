from __future__ import annotations

import asyncio
from collections import deque
from typing import Optional

from apps.oms.sync.sync_models import SyncJob, SyncOperation, SyncStatus
from apps.oms.shared.logger import get_logger

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
