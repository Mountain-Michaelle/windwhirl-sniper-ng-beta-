"""Bounded, fair scheduling for per-client upload subprocesses.

The upload engine intentionally keeps one Playwright browser per worker and
one order per page. This module only decides when the batch parent may start
the next client subprocess. It never shares a page, browser, session, or
business state between clients.

Scheduling rules:

* ``max_parallel`` limits the total browser slots consumed by active jobs.
  A job with ``--workers N`` consumes N slots unless it is a dry run.
* At most one job per client group runs at a time, because each client owns
  shared authentication and catalog resources protected by ``auth.lock``.
* Pending jobs are started round-robin across client groups, preserving
  input order within each group. A large client therefore cannot occupy
  every slot before another ready client gets a turn.
* Child processes are reaped non-blocking, so one slow client does not
  prevent the parent from monitoring or replacing other workers.
* ``KeyboardInterrupt`` and startup failures terminate active children
  before propagating.

The caller still owns all job-specific locks, status transitions, retry
state, and process exit-code reporting.
"""

from __future__ import annotations

import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class ScheduledJob:
    """One child process to run under the global scheduler."""

    key: str
    group: str
    command: Sequence[str]
    slots: int = 1
    label: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("ScheduledJob.key must not be empty")
        if not self.group:
            raise ValueError("ScheduledJob.group must not be empty")
        if not self.command:
            raise ValueError("ScheduledJob.command must not be empty")
        if self.slots < 1:
            raise ValueError("ScheduledJob.slots must be at least 1")


@dataclass(frozen=True)
class JobResult:
    job: ScheduledJob
    returncode: int


class ClientProcessScheduler:
    """Launch and monitor client subprocesses under a global slot limit."""

    def __init__(
        self,
        max_parallel: int | None,
        *,
        poll_interval_s: float = 0.1,
        popen: Callable[..., object] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        on_start: Callable[[ScheduledJob], None] | None = None,
        on_finish: Callable[[JobResult], None] | None = None,
    ) -> None:
        if max_parallel is not None and max_parallel < 0:
            raise ValueError("max_parallel must be non-negative")
        if poll_interval_s < 0:
            raise ValueError("poll_interval_s must be non-negative")
        self.max_parallel = max_parallel
        self.poll_interval_s = poll_interval_s
        self._popen = popen
        self._sleep = sleep
        self._on_start = on_start
        self._on_finish = on_finish

    @property
    def unlimited(self) -> bool:
        return not self.max_parallel

    def _fits(self, active_slots: int, job_slots: int) -> bool:
        return self.unlimited or active_slots + job_slots <= self.max_parallel

    @staticmethod
    def _terminate(process: object) -> None:
        terminate = getattr(process, "terminate", None)
        if terminate is None:
            return
        try:
            terminate()
        except Exception:
            pass

    @staticmethod
    def _poll(process: object) -> int | None:
        poll = getattr(process, "poll", None)
        if poll is None:
            raise TypeError("scheduled process must provide poll()")
        return poll()

    @staticmethod
    def _has_pending(pending: dict[str, deque[ScheduledJob]]) -> bool:
        return any(queue for queue in pending.values())

    def _shutdown(self, active: dict[str, tuple[ScheduledJob, object]]) -> None:
        for _key, (_job, process) in list(active.items()):
            self._terminate(process)
        active.clear()

    def _reap(
        self,
        active: dict[str, tuple[ScheduledJob, object]],
        results: dict[str, JobResult],
        active_slots: int,
    ) -> tuple[bool, int]:
        progress = False
        for key in list(active):
            job, process = active[key]
            returncode = self._poll(process)
            if returncode is None:
                continue
            del active[key]
            active_slots -= job.slots
            result = JobResult(job=job, returncode=returncode)
            results[key] = result
            if self._on_finish is not None:
                self._on_finish(result)
            progress = True
        return progress, active_slots

    def _launch_ready(
        self,
        pending: dict[str, deque[ScheduledJob]],
        group_order: list[str],
        cursor: int,
        active: dict[str, tuple[ScheduledJob, object]],
        active_slots: int,
    ) -> tuple[bool, int, int]:
        """Start ready jobs round-robin without starving waiting clients."""
        progress = False
        group_count = len(group_order)
        active_groups = {job.group for job, _process in active.values()}

        # Fill as many slots as possible, but always resume scanning at the
        # group after the one that just launched. A skipped busy/full group
        # does not advance the cursor, so the next free slot goes to the
        # client that has waited longest.
        while True:
            chosen_index = None
            chosen_job = None
            for offset in range(group_count):
                index = (cursor + offset) % group_count
                group = group_order[index]
                queue = pending.get(group)
                if not queue or group in active_groups:
                    continue
                job = queue[0]
                if not self._fits(active_slots, job.slots):
                    continue
                chosen_index = index
                chosen_job = job
                break
            if chosen_job is None or chosen_index is None:
                break

            queue = pending[group_order[chosen_index]]
            queue.popleft()
            process = self._popen(list(chosen_job.command))
            active[chosen_job.key] = (chosen_job, process)
            active_slots += chosen_job.slots
            active_groups.add(chosen_job.group)
            cursor = (chosen_index + 1) % group_count
            if self._on_start is not None:
                self._on_start(chosen_job)
            progress = True

        return progress, active_slots, cursor

    def run(self, jobs: Iterable[ScheduledJob]) -> list[JobResult]:
        """Run all jobs and return results in the original input order."""
        job_list = list(jobs)
        if not job_list:
            return []

        seen_keys: set[str] = set()
        pending: dict[str, deque[ScheduledJob]] = {}
        group_order: list[str] = []
        for job in job_list:
            if job.key in seen_keys:
                raise ValueError(f"duplicate scheduled job key: {job.key}")
            seen_keys.add(job.key)
            if not self.unlimited and job.slots > self.max_parallel:
                raise ValueError(
                    f"job {job.key!r} needs {job.slots} browser slots, "
                    f"greater than max_parallel={self.max_parallel}"
                )
            if job.group not in pending:
                pending[job.group] = deque()
                group_order.append(job.group)
            pending[job.group].append(job)

        active: dict[str, tuple[ScheduledJob, object]] = {}
        results: dict[str, JobResult] = {}
        active_slots = 0
        cursor = 0

        try:
            while active or self._has_pending(pending):
                progress, active_slots = self._reap(
                    active, results, active_slots
                )
                launched, active_slots, cursor = self._launch_ready(
                    pending, group_order, cursor, active, active_slots
                )
                if not progress and not launched:
                    self._sleep(self.poll_interval_s)
        except BaseException:
            self._shutdown(active)
            raise

        missing = [job.key for job in job_list if job.key not in results]
        if missing:
            raise RuntimeError(f"scheduler finished without results for: {missing}")
        return [results[job.key] for job in job_list]


__all__ = ["ClientProcessScheduler", "JobResult", "ScheduledJob"]

