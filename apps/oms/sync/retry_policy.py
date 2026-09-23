from __future__ import annotations

import asyncio

from apps.oms.shared.logger import get_logger

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
