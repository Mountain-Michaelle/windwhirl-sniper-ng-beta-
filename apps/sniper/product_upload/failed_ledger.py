"""
failed_ledger.py

NEW FILE -- additive, nothing else needs to change to adopt it.

A local, persistent record of orders whose upload FAILED -- every kind of
failure, not just the transient ones the retry queue tracks:

  - transient failures that burned through their retry budget
    (MAX_RETRY_ATTEMPTS in retry_cache.py) and were removed from the
    queue as "given up";
  - genuine business-logic rejections (no product match, no state,
    CRM validation error) that never entered the queue at all.

Both used to end in the FailureLogger exports and then simply vanish
between runs -- the next run never looked at them again, and a job could
be marked "completed" (and archived) while failed customers were still
sitting there un-uploaded.

This ledger fixes both halves of that:

  1. Every failed upload is recorded here, persisted to disk with the
     same atomic tmp+os.replace write pattern as RetryCache, so the next
     fresh run can pick the failed customers up FIRST -- before any new
     row is touched (see _drain_failed_ledger in run_upload.py).
  2. A non-empty ledger keeps a job from being marked "completed" and
     archived (checked alongside retry_queue.json at completion time).

Per business rule (Sep 2026): on every fresh run each ledger entry gets
one fresh attempt. If it fails again with the SAME reason as last time,
it is skipped for the rest of that run -- identical input against
identical CRM data can only fail identically. If the reason CHANGED, the
order is worth one more attempt the same run. Permanent data rejections
never even get the browser attempt: they are deterministic, so "same
error" is guaranteed; they stay in the ledger and keep blocking
completion.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from apps.sniper.product_upload.progress_tracker import order_key


@dataclass
class FailedLedgerEntry:
    key: str
    order: dict
    reason: str
    row_number: Optional[int] = None
    attempts: int = 0
    first_failed_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    last_attempt_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))


class FailedLedger:
    """Persistent per-job record of every failed upload. Same shape and
    error tolerance as RetryCache: JSON on disk, atomic replace-on-save,
    silently tolerated corrupt/missing files (an unreadable ledger must
    never take a run down -- it would only cost the retry-first pass)."""

    def __init__(self, path: str = "customers/progress/failed_orders.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, FailedLedgerEntry] = self._load()

    def _load(self) -> dict[str, FailedLedgerEntry]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            return {k: FailedLedgerEntry(**v) for k, v in raw.items()}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _save(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in self._entries.items()}, f,
                      indent=2, default=str)
        os.replace(tmp_path, self.path)

    def record_failure(self, order: dict, reason: str,
                       row_number: Optional[int] = None) -> FailedLedgerEntry:
        """Adds or updates the ledger entry for one failed upload. The
        attempt count only ever grows; the reason and timestamp always
        reflect the LATEST failure, which is what the same-error-skip
        comparison in _drain_failed_ledger reads back."""
        key = order_key(order)
        existing = self._entries.get(key)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = FailedLedgerEntry(
            key=key,
            order=order,
            reason=reason,
            row_number=row_number,
            attempts=(existing.attempts if existing else 0) + 1,
            first_failed_at=existing.first_failed_at if existing else now,
            last_attempt_at=now,
        )
        self._entries[key] = entry
        self._save()
        return entry

    def get(self, key: str) -> Optional[FailedLedgerEntry]:
        return self._entries.get(key)

    def remove(self, order: dict) -> None:
        self.remove_key(order_key(order))

    def remove_key(self, key: str) -> None:
        if key in self._entries:
            del self._entries[key]
            self._save()

    def pending_entries(self) -> list[FailedLedgerEntry]:
        """Snapshot of everything currently recorded as failed --
        leftovers from previous runs plus anything recorded so far this
        run that has not succeeded yet."""
        return list(self._entries.values())

    @property
    def count(self) -> int:
        return len(self._entries)

    def absorb(self, entries) -> None:
        """(--workers sharding) fold another ledger's entries into this
        one, keeping the entry with the HIGHEST attempt count per key on
        conflict -- same conservative max() merge RetryCache.absorb
        uses."""
        for entry in entries:
            existing = self._entries.get(entry.key)
            if existing is None or entry.attempts >= existing.attempts:
                self._entries[entry.key] = entry


def _shard_ledger_path(progress_dir, shard_index: int, shard_count: int) -> Path:
    return Path(progress_dir) / f"failed_orders.shard{shard_index}of{shard_count}.json"


def collect_and_merge_shard_ledgers(progress_dir, main_path) -> int:
    """(--workers sharding) fold every failed_orders.shard*of*.json in
    progress_dir back into the main ledger (max-attempts-wins per key),
    then delete the shard files. Idempotent and safe to call when none
    exist -- returns the number of shard files merged. Runs in the
    parent after its shard children exit, and at startup so a crashed
    sharded run's recorded failures are never lost."""
    progress_dir = Path(progress_dir)
    shard_files = sorted(progress_dir.glob("failed_orders.shard*of*.json"))
    if not shard_files:
        return 0
    main = FailedLedger(path=str(main_path))
    merged = 0
    for shard_file in shard_files:
        shard = FailedLedger(path=str(shard_file))
        main.absorb(shard._entries.values())
        merged += 1
        try:
            shard_file.unlink()
        except OSError:
            pass
    if merged:
        main._save()
    return merged


def partition_ledger_into_shards(progress_dir, main_path, shard_count: int) -> None:
    """(--workers sharding) distribute the main ledger's entries into
    per-shard ledger files (key hash -> shard, same bucketing as the
    retry queue) so each shard worker owns exactly one ledger file. The
    main ledger is emptied into the shards; the parent merges everything
    back when the children exit."""
    main = FailedLedger(path=str(main_path))
    buckets: dict[int, dict[str, FailedLedgerEntry]] = {i: {} for i in range(shard_count)}
    for key, entry in main._entries.items():
        buckets[int(key, 16) % shard_count][key] = entry
    for i in range(shard_count):
        shard = FailedLedger(path=str(_shard_ledger_path(progress_dir, i, shard_count)))
        shard._entries = buckets[i]
        shard._save()
    main._entries = {}
    main._save()

