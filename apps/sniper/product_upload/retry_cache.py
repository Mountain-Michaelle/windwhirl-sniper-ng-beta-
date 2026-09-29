"""
retry_cache.py

NEW FILE -- additive, nothing else needs to change to adopt it.

A local, persistent queue for orders that failed for TRANSIENT reasons --
network timeout, a selector not appearing in time because the page hadn't
finished loading, a field readback coming back empty because the DOM
hadn't settled yet -- as opposed to a genuine business-logic rejection
(product not found, state not inferable, duplicate, the CRM's own
validation error). Those genuine rejections still go straight to
FailureLogger/manual_check exactly as before -- this module only intercepts
the class of failure that's likely to succeed if you just try it again.

Persisted to disk (not just kept in memory) so a network blip that takes
down the whole process doesn't lose the retry list -- the next run picks
up exactly where this one left off.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from apps.sniper.product_upload.progress_tracker import order_key

# FIX (persistence-hardening pass, "even more persistency and harsh
# retries so no unbreaking error will slow down my upload"): this used to
# be a bare literal with no way to raise it without editing source. A
# transient failure (network blip, DOM not settled yet) deserves more than
# 3 chances before an order is written off as permanent -- especially
# since each attempt already goes through _attempt_order's own in-process
# lock-mode retry loop first (see run_upload.py), so by the time an entry
# even reaches this cache it has already survived that. Overridable via
# WW_MAX_RETRY_ATTEMPTS for a client that needs it harsher still, without
# a code change; invalid/non-positive values fall back to the default
# rather than silently disabling the give-up threshold.
_DEFAULT_MAX_RETRY_ATTEMPTS = 5


def _resolve_max_retry_attempts() -> int:
    raw = os.environ.get("WW_MAX_RETRY_ATTEMPTS", "")
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    return _DEFAULT_MAX_RETRY_ATTEMPTS


MAX_RETRY_ATTEMPTS = _resolve_max_retry_attempts()

# Reasons that mean "the CRM/data itself said no" -- retrying changes
# nothing, so these skip the queue and go straight to the permanent
# failure log, same as before this feature existed.
PERMANENT_REASON_MARKERS = (
    "no sellable product found",
    "product matching failed",
    "could not infer state from address",
    "bundles",                      # ambiguous multi-product combined price
    "order has no usable value for",  # source data genuinely empty
    "error indicator matched",      # the CRM itself rejected it (duplicate, validation, etc.)
    "flagged as",
    # An Excel price that cannot be parsed is a property of the SOURCE FILE,
    # not of the page: re-reading the same cell returns the same string, and
    # retrying the identical order can only reproduce the identical failure.
    # Before these two markers, an unparseable price was misread as transient
    # and drove a 3x order retry loop (with cache invalidation + live re-reads
    # each round) that stalled the row instead of flagging it for review.
    "could not parse excel price",
    "refusing to open the price dropdown for an empty value",
)


def is_transient_failure(reason: str) -> bool:
    """Default assumption is transient (network timeout, DOM not ready,
    a field readback that just needed another moment) -- explicit
    business-logic rejections are the exception, not the rule, and are
    called out by name in PERMANENT_REASON_MARKERS."""
    lower = (reason or "").lower()
    return not any(marker in lower for marker in PERMANENT_REASON_MARKERS)


@dataclass
class RetryEntry:
    key: str
    order: dict
    attempts: int
    last_reason: str
    row_number: Optional[int] = None
    last_attempt_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))


class RetryCache:
    def __init__(self, path: str = "customers/progress/retry_queue.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, RetryEntry] = self._load()

    def _load(self) -> dict[str, RetryEntry]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            return {k: RetryEntry(**v) for k, v in raw.items()}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _save(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in self._entries.items()}, f, indent=2, default=str)
        os.replace(tmp_path, self.path)

    def record_failure(self, order: dict, reason: str, row_number: Optional[int] = None) -> RetryEntry:
        """Adds/updates a queue entry for a transient failure. Returns the
        entry so the caller can check whether MAX_RETRY_ATTEMPTS has been
        exceeded (see should_give_up)."""
        key = order_key(order)
        existing = self._entries.get(key)
        attempts = (existing.attempts if existing else 0) + 1
        entry = RetryEntry(key=key, order=order, attempts=attempts,
                            last_reason=reason, row_number=row_number)
        self._entries[key] = entry
        self._save()
        return entry

    def should_give_up(self, entry: RetryEntry) -> bool:
        return entry.attempts >= MAX_RETRY_ATTEMPTS

    def remove(self, order: dict) -> None:
        key = order_key(order)
        if key in self._entries:
            del self._entries[key]
            self._save()

    def remove_key(self, key: str) -> None:
        """Companion to remove() for callers that already hold the key.
        Needed when a row was EDITED since the entry was recorded: the
        fresh order's dedup key no longer matches the entry's key, so
        remove() (which hashes the order) can never find it."""
        if key in self._entries:
            del self._entries[key]
            self._save()

    def pending_entries(self) -> list[RetryEntry]:
        """Snapshot of everything currently queued -- includes items left
        over from a previous run that never got retried before the
        process ended, as well as anything queued so far this run."""
        return list(self._entries.values())

    @property
    def count(self) -> int:
        return len(self._entries)

    def absorb(self, entries) -> None:
        """PERF (--workers sharding): fold another queue's entries into
        this one, keeping the entry with the HIGHEST attempt count per
        key on conflict (attempts only ever grow, so max() is the
        conservative merge). Used when merging per-shard queue files
        back into the main persistent queue."""
        for entry in entries:
            existing = self._entries.get(entry.key)
            if existing is None or entry.attempts >= existing.attempts:
                self._entries[entry.key] = entry


def _shard_queue_path(progress_dir, shard_index: int, shard_count: int) -> Path:
    return Path(progress_dir) / f"retry_queue.shard{shard_index}of{shard_count}.json"


def collect_and_merge_shard_queues(progress_dir, main_path) -> int:
    """PERF (--workers sharding) recovery/merge step: fold every
    retry_queue.shard*of*.json found in progress_dir back into the main
    persistent queue (highest-attempt-wins per key), then delete the
    shard files. Idempotent and safe to call when none exist -- returns
    the number of shard files merged. Runs in the parent after its shard
    children exit, and at startup so a crashed sharded run's queued
    retries are never lost."""
    progress_dir = Path(progress_dir)
    shard_files = sorted(progress_dir.glob("retry_queue.shard*of*.json"))
    if not shard_files:
        return 0
    main = RetryCache(path=str(main_path))
    merged = 0
    for shard_file in shard_files:
        shard = RetryCache(path=str(shard_file))
        main.absorb(shard._entries.values())
        merged += 1
        try:
            shard_file.unlink()
        except OSError:
            pass
    if merged:
        main._save()
    return merged


def partition_queue_into_shards(progress_dir, main_path, shard_count: int) -> None:
    """PERF (--workers sharding): distribute the main queue's entries
    into per-shard queue files (key hash -> shard) so each shard worker
    owns exactly one queue file and two workers can never attempt the
    same queued order concurrently. The main queue is emptied into the
    shards; the parent merges everything back when the children exit.
    A crash between partition and merge is recovered by
    collect_and_merge_shard_queues() on the next run."""
    main = RetryCache(path=str(main_path))
    buckets: dict[int, dict[str, RetryEntry]] = {i: {} for i in range(shard_count)}
    for key, entry in main._entries.items():
        buckets[int(key, 16) % shard_count][key] = entry
    for i in range(shard_count):
        shard = RetryCache(path=str(_shard_queue_path(progress_dir, i, shard_count)))
        shard._entries = buckets[i]
        shard._save()
    main._entries = {}
    main._save()