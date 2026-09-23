# ==============================================================
# WINDWHIRL OMS — DAY 3: PASSIVE MESSAGE OBSERVER
# ==============================================================
# FILES IN THIS DOCUMENT:
#
#   FILE 1  → infrastructure/browser/raw_message.py
#   FILE 2  → infrastructure/browser/message_cache.py
#   FILE 3  → infrastructure/browser/checkpoint_store.py
#   FILE 4  → infrastructure/browser/recovery_manager.py
#   FILE 5  → infrastructure/browser/dom_observer.py
#   FILE 6  → infrastructure/
# 
# 
# /__init__.py     (update)
#   FILE 7  → infrastructure/__init__.py             (update)
#   FILE 8  → config/settings.py                     (add observer settings)
#   FILE 9  → session_manager.py                     (add group navigation)
#   FILE 10 → oms_runner.py                          (update to run observer)
#
# ARCHITECTURE:
#   Browser → SessionManager → (navigate to group) →
#   RecoveryManager → (startup catch-up) →
#   DOMObserver → (live watching) →
#   RawMessage → EventDispatcher → (Day 4 classifier picks up here)
#
# CORE DESIGN DECISION — MutationObserver via JS injection:
#   Instead of Playwright polling (checking every N seconds),
#   we inject a real JavaScript MutationObserver into the page.
#   When WhatsApp inserts a new message node into the DOM,
#   the JS fires immediately and queues it for Python to read.
#   Python reads the queue every 2 seconds via page.evaluate().
#   This is event-driven on the JS side — no message is missed
#   between polls because the JS buffers everything.
#
#   Polling interval: 2 seconds (only to drain the JS queue)
#   Detection latency: ~0ms (JS fires on DOM mutation)
#   CPU impact: minimal (no busy-wait, no selector scanning)
#
# RESPONSIBILITY BOUNDARY:
#   Everything in Day 3 stops at RawMessage + events.
#   No order detection. No parsing. No classification.
#   Day 4 (classifier) consumes "message.received" events.
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/infrastructure/browser/raw_message.py
# ================================================================
# PURPOSE:
#   Defines RawMessage — the output of the observer layer.
#   Pure data. No business logic. No interpretation.
#
#   Every message the observer sees becomes a RawMessage.
#   Day 4's classifier decides what TYPE of message it is.
#   Day 5's parser extracts structured data from order messages.
#   Today we only capture and fingerprint.
#
# NOTE ON DOMAIN vs INFRASTRUCTURE:
#   Day 1 defined a domain RawMessage in domain/entities.py.
#   This infrastructure RawMessage is the SAME concept but
#   carries additional infrastructure-specific fields:
#     fingerprint, dom_reference, captured_at, direction.
#   Day 4 will convert infrastructure RawMessage → domain RawMessage
#   when passing to the application layer.
#   Keeping them separate respects the domain/infra boundary.
# ================================================================
# ==============================================================

"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MessageDirection(str, Enum):
    '''
    Whether the message was sent by someone else (INCOMING)
    or by the monitored account itself (OUTGOING).

    The OMS primarily cares about INCOMING messages — orders
    sent by customers or assignments sent by coordinators.
    OUTGOING messages are captured for completeness and for
    the recovery manager's checkpoint matching.
    '''
    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"
    UNKNOWN  = "UNKNOWN"


@dataclass
class RawMessage:
    '''
    A single WhatsApp message as captured from the DOM.
    Contains no interpretation — only raw captured data.

    Fields:
        internal_id:   OMS-assigned sequential ID for this session.
                       Not persistent — resets each run.
        fingerprint:   Deterministic hash for deduplication.
                       Based on sender + timestamp + normalized text.
                       Persistent across runs — used by checkpoint store.
        sender:        Sender's display name or phone number as shown
                       in WhatsApp Web. May be a name if saved in contacts.
        sender_number: Phone number if extractable from DOM, else "".
        raw_text:      Complete message text including whitespace.
                       Exactly as it appears in WhatsApp Web.
        timestamp:     Message timestamp as parsed from WhatsApp Web UI.
                       May be a time string ("14:32") or date ("Yesterday").
        direction:     INCOMING or OUTGOING (relative to monitored account).
        captured_at:   When the OMS captured this message (system time).
        dom_reference: CSS path or identifier of the DOM node.
                       Used for debugging selector issues.
        group_name:    Which WhatsApp group this message came from.
        is_recovered:  True if this message was found during startup
                       recovery, False if detected live.
    '''
    internal_id:   int
    fingerprint:   str
    sender:        str
    raw_text:      str
    timestamp:     str
    direction:     MessageDirection
    group_name:    str
    captured_at:   datetime           = field(default_factory=datetime.now)
    sender_number: str                = ""
    dom_reference: str                = ""
    is_recovered:  bool               = False

    @staticmethod
    def compute_fingerprint(
        sender:    str,
        timestamp: str,
        raw_text:  str
    ) -> str:
        '''
        Compute a deterministic fingerprint for a message.
        Used for deduplication — two messages with the same
        fingerprint are treated as the same message.

        INPUT NORMALIZATION:
          sender:    stripped and lowercased
          timestamp: stripped
          raw_text:  whitespace collapsed, stripped, lowercased

        OUTPUT:
          First 16 characters of SHA-256 hex digest.
          16 chars = 64-bit collision space — sufficient for a
          WhatsApp group message volume (thousands per day).

        WHY NOT USE WHATSAPP'S INTERNAL ID:
          WhatsApp Web's internal message IDs are not consistently
          accessible via the DOM across WhatsApp Web versions.
          A deterministic fingerprint based on content is more
          reliable than depending on WhatsApp's internal structure.
        '''
        # Normalize inputs before hashing
        normalized_sender = sender.strip().lower()
        normalized_time   = timestamp.strip()
        # Collapse multiple whitespace into single space
        normalized_text   = re.sub(r'\s+', ' ', raw_text).strip().lower()

        payload = f"{normalized_sender}|{normalized_time}|{normalized_text}"
        digest  = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        # Return first 16 hex chars (64 bits) — enough entropy for this use case
        return digest[:16]

    def preview(self, max_chars: int = 60) -> str:
        '''Short preview of message text for logging.'''
        text = self.raw_text.strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    def is_incoming(self) -> bool:
        '''True if this message was sent by someone else.'''
        return self.direction == MessageDirection.INCOMING

    def __repr__(self):
        return (
            f"RawMessage("
            f"id={self.internal_id}, "
            f"fp={self.fingerprint!r}, "
            f"sender={self.sender!r}, "
            f"direction={self.direction.value}, "
            f"recovered={self.is_recovered}, "
            f"text={self.preview(40)!r}"
            f")"
        )
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/infrastructure/browser/message_cache.py
# ================================================================
# PURPOSE:
#   Lightweight in-memory cache of recently seen message fingerprints.
#   Prevents the same message from being processed twice.
#
# WHY AN IN-MEMORY CACHE (not database):
#   The duplicate check happens in the hot path — every message
#   that arrives goes through this cache. It must be O(1).
#   A database lookup per message would be too slow and would
#   create unnecessary I/O during live observation.
#
#   The cache is bounded in size (max_size). When full, the
#   oldest fingerprints are evicted. This is safe because:
#     - The database (Day 5) handles long-term deduplication
#     - The cache only needs to cover the current session window
#     - Evicted fingerprints represent old messages unlikely to reappear
#
# THREAD SAFETY:
#   asyncio is single-threaded — no locks needed.
# ================================================================
# ==============================================================

"""
from collections import OrderedDict
from datetime import datetime

from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class MessageCache:
    '''
    Bounded LRU cache of message fingerprints.

    Tracks which messages have already been seen this session
    to prevent duplicate processing.

    Uses an OrderedDict as an LRU cache:
      - Insertion order is maintained
      - When max_size is reached, the oldest entry is evicted
      - Lookup is O(1)

    Usage:
        cache = MessageCache(max_size=1000)
        if not cache.has_seen(fingerprint):
            cache.mark_seen(fingerprint)
            process_message(message)
    '''

    def __init__(self, max_size: int = 1000):
        '''
        Args:
            max_size: Maximum number of fingerprints to keep.
                      When exceeded, the oldest is evicted.
                      Default 1000 covers several hours of typical
                      WhatsApp group activity.
        '''
        self._cache:    OrderedDict[str, datetime] = OrderedDict()
        self._max_size: int                        = max_size
        self._hits:     int                        = 0   # Duplicate detections
        self._misses:   int                        = 0   # New messages

    def has_seen(self, fingerprint: str) -> bool:
        '''
        True if this fingerprint has been seen before.
        Moves the entry to the end (most recently used) on hit.

        Args:
            fingerprint: The message fingerprint to check.

        Returns:
            True  → already seen, skip this message
            False → new message, process it
        '''
        if fingerprint in self._cache:
            # Move to end (most recently used) — LRU policy
            self._cache.move_to_end(fingerprint)
            self._hits += 1
            return True

        self._misses += 1
        return False

    def mark_seen(self, fingerprint: str) -> None:
        '''
        Record a fingerprint as seen.
        Evicts the oldest entry if cache is full.

        Args:
            fingerprint: The fingerprint to record.
        '''
        if fingerprint in self._cache:
            # Already present — just move to end
            self._cache.move_to_end(fingerprint)
            return

        # Evict oldest if at capacity
        if len(self._cache) >= self._max_size:
            oldest_key, _ = self._cache.popitem(last=False)
            log.debug(f"Cache eviction: {oldest_key!r}")

        self._cache[fingerprint] = datetime.now()

    def clear(self) -> None:
        '''Clear all cached fingerprints. Used when recovery resets.'''
        count = len(self._cache)
        self._cache.clear()
        log.debug(f"Message cache cleared ({count} entries removed)")

    def seed(self, fingerprints: list[str]) -> None:
        '''
        Pre-populate the cache with known fingerprints.
        Called during startup to pre-load checkpoint fingerprints
        so recovery messages don't get double-processed.

        Args:
            fingerprints: List of fingerprints to mark as already seen.
        '''
        for fp in fingerprints:
            self.mark_seen(fp)
        log.debug(f"Cache seeded with {len(fingerprints)} fingerprints")

    @property
    def size(self) -> int:
        '''Current number of entries in the cache.'''
        return len(self._cache)

    def stats(self) -> dict:
        '''Return cache statistics for diagnostics.'''
        return {
            "size":     self.size,
            "max_size": self._max_size,
            "hits":     self._hits,
            "misses":   self._misses,
            "hit_rate": (
                f"{self._hits / (self._hits + self._misses) * 100:.1f}%"
                if (self._hits + self._misses) > 0
                else "0%"
            ),
        }
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/infrastructure/browser/checkpoint_store.py
# ================================================================
# PURPOSE:
#   Persists recovery checkpoints to disk.
#   A checkpoint is a record of the last successfully processed
#   message — used by RecoveryManager to find where to resume
#   after the OMS was offline.
#
# WHY MULTIPLE CHECKPOINTS (not just one):
#   If the OMS stores only the last fingerprint and that exact
#   message was deleted or is not visible after scrolling, recovery
#   fails entirely. Storing the last N fingerprints means recovery
#   succeeds if ANY of them are still visible in the chat.
#
# STORAGE:
#   Simple JSON file in the data/ directory.
#   Atomic write (write to temp, rename) prevents corruption
#   if the OMS crashes mid-write.
#   No database dependency — keeps Day 3 infrastructure-light.
# ================================================================
# ==============================================================

"""
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.oms.shared.logger import get_logger

log = get_logger(__name__)


@dataclass
class Checkpoint:
    '''
    A saved recovery point — the last successfully processed message.

    fingerprint:  Message fingerprint (for matching in DOM during recovery).
    timestamp_str: Timestamp string as shown in WhatsApp ("14:32", "Yesterday").
    saved_at:     When this checkpoint was saved (ISO format string).
    message_preview: Short preview of the message text (for debugging).
    '''
    fingerprint:      str
    timestamp_str:    str
    saved_at:         str
    message_preview:  str = ""

    @staticmethod
    def now_str() -> str:
        return datetime.now().isoformat()


class CheckpointStore:
    '''
    Persists and retrieves recovery checkpoints.

    Stores the last N fingerprints seen before the OMS went offline.
    RecoveryManager uses these to find where to resume in the chat.

    Usage:
        store = CheckpointStore(group_name="Nabeau Orders", max_history=5)
        store.save(checkpoint)
        history = store.load_history()   # List[Checkpoint], most recent first
    '''

    DEFAULT_MAX_HISTORY = 5   # Store last 5 checkpoints

    def __init__(
        self,
        group_name:  str,
        data_dir:    str = "data",
        max_history: int = DEFAULT_MAX_HISTORY,
    ):
        '''
        Args:
            group_name:  The WhatsApp group name — used in the filename
                         so each group has its own checkpoint file.
            data_dir:    Directory to store checkpoint files.
            max_history: How many checkpoints to retain.
        '''
        self._max_history = max_history
        self._data_dir    = Path(data_dir)

        # Build a safe filename from the group name
        safe_name    = "".join(c if c.isalnum() else "_" for c in group_name)
        self._path   = self._data_dir / f"oms_checkpoint_{safe_name}.json"

        log.debug(f"CheckpointStore: {self._path}")

    def save(self, checkpoint: Checkpoint) -> None:
        '''
        Save a checkpoint to disk.
        Prepends to history (most recent first).
        Trims history to max_history entries.
        Atomic write — safe against crashes mid-write.

        Args:
            checkpoint: The checkpoint to save.
        '''
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Load existing history
        history = self.load_history()

        # Prepend new checkpoint (most recent first)
        history.insert(0, checkpoint)

        # Trim to max history size
        history = history[:self._max_history]

        # Convert to serializable format
        data = {
            "version":  1,
            "updated":  Checkpoint.now_str(),
            "history":  [asdict(cp) for cp in history],
        }

        # Atomic write: write to temp file, then rename
        # This prevents corrupt checkpoint files if OMS crashes mid-write
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix=".tmp")

        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # Atomic rename — replaces the old file in one OS operation
            os.replace(tmp_path, self._path)
            log.debug(
                f"Checkpoint saved: fp={checkpoint.fingerprint!r}, "
                f"ts={checkpoint.timestamp_str!r}"
            )

        except Exception as e:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            log.error(f"Failed to save checkpoint: {e}", exc_info=True)

    def load_history(self) -> list[Checkpoint]:
        '''
        Load checkpoint history from disk.
        Returns empty list if no checkpoint file exists.
        Most recent checkpoint is first in the list.

        Returns:
            List of Checkpoint objects, most recent first.
        '''
        if not self._path.exists():
            log.debug("No checkpoint file found — starting fresh.")
            return []

        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)

            history = [
                Checkpoint(**cp)
                for cp in data.get("history", [])
            ]

            log.debug(
                f"Loaded {len(history)} checkpoint(s) from {self._path}"
            )
            return history

        except Exception as e:
            log.warning(
                f"Could not load checkpoint file: {e}\n"
                f"Starting recovery from scratch."
            )
            return []

    def latest(self) -> Optional[Checkpoint]:
        '''
        The most recent checkpoint, or None if no history exists.
        '''
        history = self.load_history()
        return history[0] if history else None

    def all_fingerprints(self) -> list[str]:
        '''
        All fingerprints from checkpoint history.
        Used by MessageCache.seed() to pre-populate the cache
        before recovery begins, preventing double-processing.
        '''
        return [cp.fingerprint for cp in self.load_history()]

    def offline_duration_hours(self) -> Optional[float]:
        '''
        Calculate how long the OMS was offline based on the latest checkpoint.
        Returns None if no checkpoint exists (first ever run).

        Used by RecoveryManager to decide whether to attempt recovery.
        '''
        latest = self.latest()
        if not latest:
            return None

        try:
            saved_at = datetime.fromisoformat(latest.saved_at)
            delta    = datetime.now() - saved_at
            return delta.total_seconds() / 3600  # Convert to hours
        except Exception as e:
            log.warning(f"Could not parse checkpoint timestamp: {e}")
            return None

    def clear(self) -> None:
        '''Delete the checkpoint file. Used for fresh starts.'''
        if self._path.exists():
            self._path.unlink()
            log.info(f"Checkpoint cleared: {self._path}")

    def __repr__(self):
        latest = self.latest()
        return (
            f"CheckpointStore("
            f"path={self._path.name!r}, "
            f"latest={latest.fingerprint[:8] if latest else None!r}"
            f")"
        )
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/infrastructure/browser/recovery_manager.py
# ================================================================
# PURPOSE:
#   Catches up on messages missed while the OMS was offline.
#   Runs ONCE at startup, before the live observer begins.
#   Never runs while the live observer is active.
#
# RECOVERY ALGORITHM:
#   1. Load checkpoint history from CheckpointStore
#   2. Calculate offline duration
#   3. If offline > RECOVERY_MAX_AGE_HOURS → skip recovery
#   4. Read currently visible messages in the group chat
#   5. Search for any checkpoint fingerprint in visible messages
#   6. If found → replay only messages newer than the checkpoint
#   7. If not found → scroll up and repeat
#   8. Stop when: checkpoint found OR max scrolls reached
#
# DESIGN RULES FROM DAY 3 PROMPT:
#   - Recovery runs ONLY during startup
#   - Recovery NEVER runs while live observer is active
#   - Stop after RECOVERY_MAX_SCROLLS regardless of result
#   - Never crash — log warning and start live observer
# ================================================================
# ==============================================================

"""
import asyncio
from datetime import datetime
from typing import Optional

from app.oms.infrastructure.browser.raw_message import (
    RawMessage,
    MessageDirection,
)
from app.oms.infrastructure.browser.message_cache import MessageCache
from app.oms.infrastructure.browser.checkpoint_store import (
    CheckpointStore,
    Checkpoint,
)
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class RecoveryManager:
    '''
    Startup-only component that replays messages missed while offline.

    Reads from the currently open WhatsApp group chat (no navigation).
    Emits "message.recovered" events for each missed message found.
    Updates the checkpoint after each successful recovery.

    After recovery completes (or is skipped), the Live DOM Observer
    takes over. These two components NEVER run simultaneously.

    Usage:
        recovery = RecoveryManager(page, checkpoint_store, cache, cfg)
        recovered = await recovery.run()
        # recovered is a list of RawMessage — may be empty
        # After this returns, start DOMObserver
    '''

    # WhatsApp Web CSS selectors for reading messages
    # These read the DOM passively — no clicking, no navigation
    SEL_MESSAGES       = 'div[role="row"]'                   # Each message row
    SEL_INCOMING_TEXT  = '.message-in .copyable-text'         # Incoming text
    SEL_OUTGOING_TEXT  = '.message-out .copyable-text'        # Outgoing text
    SEL_TIMESTAMP      = '[data-pre-plain-text]'              # Timestamp attribute
    SEL_SENDER_NAME    = '.copyable-text span[aria-label]'    # Sender name
    SEL_MSG_CONTAINER  = '#main div[role="application"]'      # Chat container

    def __init__(
        self,
        page,
        checkpoint_store: CheckpointStore,
        cache:            MessageCache,
        cfg,
    ):
        '''
        Args:
            page:             Playwright page with WhatsApp group open.
            checkpoint_store: Loaded checkpoint history.
            cache:            MessageCache (will be seeded with checkpoint fps).
            cfg:              OMSSettings.
        '''
        self._page       = page
        self._store      = checkpoint_store
        self._cache      = cache
        self._cfg        = cfg
        self._id_counter = 0   # For generating internal_id

        # Recovery limits from config
        obs_cfg = getattr(cfg, 'observer', None)
        self._max_age_hours  = getattr(obs_cfg, 'recovery_max_age_hours',  24)
        self._max_messages   = getattr(obs_cfg, 'recovery_max_messages',   500)
        self._max_scrolls    = getattr(obs_cfg, 'recovery_max_scrolls',    20)
        self._group_name     = cfg.whatsapp.group_name

    async def run(self) -> list[RawMessage]:
        '''
        Execute startup recovery. Returns list of recovered messages.

        Flow:
          1. Check if recovery is needed
          2. Seed cache with checkpoint fingerprints
          3. Search visible messages for checkpoints
          4. Scroll up and search again if not found
          5. Replay missed messages
          6. Return recovered messages (empty if nothing to recover)
        '''
        await dispatcher.emit("recovery.started", group=self._group_name)

        # ── Step 1: Decide whether to attempt recovery ──────────
        checkpoint_history = self._store.load_history()
        offline_hours      = self._store.offline_duration_hours()

        if not checkpoint_history:
            log.info(
                "No previous checkpoint found — first run.\n"
                "Recovery skipped. Live Observer will start fresh."
            )
            await dispatcher.emit(
                "recovery.skipped",
                reason="no_checkpoint",
                group=self._group_name
            )
            return []

        if offline_hours is None or offline_hours > self._max_age_hours:
            log.info(
                f"Offline duration ({offline_hours:.1f}h if known) exceeds "
                f"RECOVERY_MAX_AGE_HOURS ({self._max_age_hours}h).\n"
                "Recovery skipped. Live Observer starts fresh."
            )
            await dispatcher.emit(
                "recovery.skipped",
                reason="offline_too_long",
                offline_hours=offline_hours,
                group=self._group_name
            )
            return []

        log.info(
            f"Recovery starting — was offline for {offline_hours:.1f}h\n"
            f"Checkpoint history: {len(checkpoint_history)} fingerprint(s)"
        )

        # ── Step 2: Seed cache with known fingerprints ──────────
        # Pre-populate the cache with checkpoint fingerprints so that
        # if we encounter them during recovery, they're skipped
        # (we don't want to re-process already-seen messages)
        known_fps = self._store.all_fingerprints()
        self._cache.seed(known_fps)

        # ── Step 3-4: Search for checkpoint in DOM ──────────────
        checkpoint_fps = set(known_fps)
        recovered      = []

        try:
            recovered = await self._search_and_recover(checkpoint_fps)
        except Exception as e:
            log.warning(
                f"Recovery encountered an error: {e}\n"
                "Starting Live Observer without recovery.",
                exc_info=True
            )
            await dispatcher.emit(
                "recovery.completed",
                recovered_count=0,
                status="error",
                group=self._group_name
            )
            return []

        log.info(
            f"Recovery complete — {len(recovered)} message(s) replayed."
        )
        await dispatcher.emit(
            "recovery.completed",
            recovered_count=len(recovered),
            status="success",
            group=self._group_name
        )

        return recovered

    async def _search_and_recover(
        self,
        checkpoint_fps: set[str]
    ) -> list[RawMessage]:
        '''
        Search the chat for a checkpoint fingerprint.
        Scrolls up if not found in visible messages.
        Returns messages newer than the found checkpoint.
        '''
        scroll_count   = 0
        all_messages   = []

        while scroll_count <= self._max_scrolls:
            # Read all currently visible messages
            visible = await self._read_visible_messages()

            # Search for any checkpoint fingerprint
            checkpoint_idx = self._find_checkpoint(visible, checkpoint_fps)

            if checkpoint_idx is not None:
                # Found a checkpoint — replay everything after it
                log.info(
                    f"Checkpoint found at position {checkpoint_idx} "
                    f"(after {scroll_count} scroll(s))"
                )

                messages_to_replay = visible[checkpoint_idx + 1:]
                recovered          = []

                for msg in messages_to_replay:
                    if len(recovered) >= self._max_messages:
                        log.warning(
                            f"Recovery hit RECOVERY_MAX_MESSAGES "
                            f"({self._max_messages}) — stopping."
                        )
                        break

                    if not self._cache.has_seen(msg.fingerprint):
                        msg.is_recovered = True
                        self._cache.mark_seen(msg.fingerprint)
                        recovered.append(msg)

                        await dispatcher.emit(
                            "message.recovered",
                            message=msg,
                            group=self._group_name
                        )

                        # Update checkpoint after each successful recovery
                        self._store.save(Checkpoint(
                            fingerprint     =msg.fingerprint,
                            timestamp_str   =msg.timestamp,
                            saved_at        =Checkpoint.now_str(),
                            message_preview =msg.preview(),
                        ))

                return recovered

            # Checkpoint not in visible area — scroll up and try again
            if scroll_count >= self._max_scrolls:
                log.warning(
                    f"Checkpoint not found after {self._max_scrolls} scroll(s).\n"
                    "Recovery stopped. Live Observer will start fresh."
                )
                break

            log.debug(
                f"Checkpoint not visible (scroll {scroll_count + 1}"
                f"/{self._max_scrolls}) — scrolling up..."
            )
            await self._scroll_up()
            scroll_count += 1

            # Brief pause after scroll — let WhatsApp Web load older messages
            await asyncio.sleep(1.5)

        return []   # No checkpoint found — return empty

    async def _read_visible_messages(self) -> list[RawMessage]:
        '''
        Read all currently visible messages from the open chat.
        Reads the DOM passively — no clicks, no navigation.
        Returns a list of RawMessage objects, oldest first.
        '''
        try:
            # Extract message data via JavaScript — one DOM query
            # is more efficient than multiple Playwright calls
            messages_data = await self._page.evaluate("""
                () => {
                    const results = [];
                    // Find all message rows in the chat
                    const rows = document.querySelectorAll('div[role="row"]');

                    rows.forEach((row, idx) => {
                        try {
                            // Determine direction from class
                            const isIncoming = row.querySelector('.message-in') !== null;
                            const isOutgoing = row.querySelector('.message-out') !== null;
                            if (!isIncoming && !isOutgoing) return;

                            // Get message text
                            const textEl = row.querySelector(
                                '.copyable-text span[class*="selectable"],' +
                                '.copyable-text span[dir],' +
                                '.message-in .copyable-text,' +
                                '.message-out .copyable-text'
                            );
                            const rawText = textEl ? textEl.innerText.trim() : '';
                            if (!rawText) return;  // Skip empty messages

                            // Get timestamp from data attribute
                            const tsEl = row.querySelector('[data-pre-plain-text]');
                            const tsAttr = tsEl
                                ? tsEl.getAttribute('data-pre-plain-text')
                                : '';
                            // data-pre-plain-text format: "[HH:MM, DD/MM/YYYY] Name: "
                            const tsMatch = tsAttr.match(/\\[([^,]+),/);
                            const timestamp = tsMatch ? tsMatch[1].trim() : '';

                            // Get sender name
                            const senderMatch = tsAttr.match(/\\] ([^:]+):/);
                            const sender = senderMatch
                                ? senderMatch[1].trim()
                                : (isOutgoing ? 'You' : 'Unknown');

                            results.push({
                                rawText:   rawText,
                                timestamp: timestamp,
                                sender:    sender,
                                direction: isIncoming ? 'INCOMING' : 'OUTGOING',
                                domIdx:    idx,
                            });
                        } catch(e) {
                            // Skip malformed message nodes silently
                        }
                    });

                    return results;
                }
            """)

        except Exception as e:
            log.warning(f"Could not read visible messages: {e}")
            return []

        if not messages_data:
            return []

        result = []
        for data in messages_data:
            self._id_counter += 1
            fp = RawMessage.compute_fingerprint(
                sender    =data["sender"],
                timestamp =data["timestamp"],
                raw_text  =data["rawText"],
            )
            msg = RawMessage(
                internal_id  =self._id_counter,
                fingerprint  =fp,
                sender       =data["sender"],
                raw_text     =data["rawText"],
                timestamp    =data["timestamp"],
                direction    =MessageDirection(data["direction"]),
                group_name   =self._group_name,
                dom_reference=f"row[{data['domIdx']}]",
            )
            result.append(msg)

        return result

    def _find_checkpoint(
        self,
        messages: list[RawMessage],
        checkpoint_fps: set[str]
    ) -> Optional[int]:
        '''
        Find the index of a checkpoint fingerprint in the message list.
        Returns the index of the MOST RECENT checkpoint found,
        or None if no checkpoint fingerprint is in the list.
        '''
        # Search from most recent (end) to oldest (start)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].fingerprint in checkpoint_fps:
                return i
        return None

    async def _scroll_up(self) -> None:
        '''
        Scroll the chat window upward to reveal older messages.
        Uses JavaScript to scroll the chat container element.
        Does not navigate or click anything.
        '''
        try:
            await self._page.evaluate("""
                () => {
                    // Find the scrollable chat container
                    const container =
                        document.querySelector('#main div[role="application"]') ||
                        document.querySelector('#main .copyable-area') ||
                        document.querySelector('#main');

                    if (container) {
                        // Scroll up by a portion of the container height
                        // (not the full height — partial scroll reveals messages
                        //  in manageable batches)
                        container.scrollTop -= Math.floor(container.clientHeight * 0.7);
                    }
                }
            """)
        except Exception as e:
            log.debug(f"Scroll up error (non-critical): {e}")
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/infrastructure/browser/dom_observer.py
# ================================================================
# PURPOSE:
#   Passively watches for new messages in the open WhatsApp group.
#   Starts AFTER recovery completes.
#   Runs continuously until stopped.
#
# DETECTION METHOD — JavaScript MutationObserver:
#   A real JavaScript MutationObserver is injected into the page.
#   It watches the chat message container for DOM mutations.
#   When WhatsApp inserts a new message node, the JS fires
#   immediately and appends the message data to a queue.
#   Python polls this queue every 2 seconds to drain it.
#
#   This is NOT polling the DOM for selectors every 2 seconds.
#   The JS detects the mutation instantly (0ms latency).
#   Python drains the queue every 2 seconds (minimal CPU).
#   Messages are never missed between Python polls because
#   the JS buffers everything.
#
# PASSIVE PRINCIPLE:
#   The observer never clicks, types, scrolls, or navigates.
#   It only reads. The browser state never changes because of it.
#   WhatsApp Web behaves exactly as if a human is watching.
# ================================================================
# ==============================================================

"""
import asyncio
from typing import Optional

from app.oms.infrastructure.browser.raw_message import (
    RawMessage,
    MessageDirection,
)
from app.oms.infrastructure.browser.message_cache import MessageCache
from app.oms.infrastructure.browser.checkpoint_store import (
    CheckpointStore,
    Checkpoint,
)
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

log = get_logger(__name__)

# JavaScript MutationObserver injected into the page.
# This watches the WhatsApp chat container for new message nodes.
# When a new message is inserted, it extracts the text, sender,
# timestamp, and direction, then pushes to a global JS queue.
# Python reads this queue via page.evaluate() on a timer.
_MUTATION_OBSERVER_JS = """
() => {
    // Guard: don't inject multiple observers if called again
    if (window.__omsObserverActive) {
        return { status: 'already_active' };
    }

    // Message queue — Python drains this periodically
    window.__omsMessageQueue = window.__omsMessageQueue || [];
    window.__omsObserverActive = true;
    window.__omsMsgCounter = window.__omsMsgCounter || 0;

    // Find the chat message container
    const findContainer = () => {
        return (
            document.querySelector('#main div[role="application"]') ||
            document.querySelector('#main .copyable-area') ||
            document.querySelector('#main')
        );
    };

    const container = findContainer();
    if (!container) {
        window.__omsObserverActive = false;
        return { status: 'container_not_found' };
    }

    // Extract message data from a DOM node
    const extractMessage = (node) => {
        try {
            // Only process actual message rows
            if (!node.getAttribute || node.getAttribute('role') !== 'row') {
                // Check children for a row
                const row = node.querySelector && node.querySelector('div[role="row"]');
                if (!row) return null;
                node = row;
            }

            const isIncoming = node.querySelector('.message-in') !== null;
            const isOutgoing = node.querySelector('.message-out') !== null;
            if (!isIncoming && !isOutgoing) return null;

            // Get text content
            const textEl = node.querySelector(
                '.copyable-text span[class*="selectable"],' +
                '.copyable-text span[dir],' +
                '.message-in .copyable-text,' +
                '.message-out .copyable-text'
            );
            const rawText = textEl ? textEl.innerText.trim() : '';
            if (!rawText) return null;

            // Get timestamp and sender from data attribute
            const tsEl    = node.querySelector('[data-pre-plain-text]');
            const tsAttr  = tsEl ? tsEl.getAttribute('data-pre-plain-text') : '';
            const tsMatch = tsAttr.match(/\\[([^,]+),/);
            const snMatch = tsAttr.match(/\\] ([^:]+):/);

            window.__omsMsgCounter++;

            return {
                rawText:      rawText,
                timestamp:    tsMatch ? tsMatch[1].trim() : '',
                sender:       snMatch ? snMatch[1].trim() : (isOutgoing ? 'You' : 'Unknown'),
                direction:    isIncoming ? 'INCOMING' : 'OUTGOING',
                capturedMs:   Date.now(),
                queueIdx:     window.__omsMsgCounter,
            };
        } catch(e) {
            return null;    // Skip malformed nodes silently
        }
    };

    // Create the MutationObserver
    const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            mutation.addedNodes.forEach((node) => {
                // Only process element nodes (not text/comment nodes)
                if (node.nodeType !== Node.ELEMENT_NODE) return;

                const msg = extractMessage(node);
                if (msg) {
                    window.__omsMessageQueue.push(msg);
                }

                // Also check immediate children (WhatsApp sometimes
                // inserts a wrapper div containing the message row)
                if (node.children) {
                    Array.from(node.children).forEach((child) => {
                        const childMsg = extractMessage(child);
                        if (childMsg) {
                            window.__omsMessageQueue.push(childMsg);
                        }
                    });
                }
            });
        });
    });

    // Observe the container for child additions
    // subtree: true — watch all descendants, not just direct children
    // childList: true — react to added/removed nodes
    observer.observe(container, {
        childList: true,
        subtree:   true,
    });

    // Store reference to allow cleanup later
    window.__omsObserver = observer;

    return { status: 'active', containerTag: container.tagName };
}
"""

# JavaScript to drain the message queue (called by Python on a timer)
_DRAIN_QUEUE_JS = """
() => {
    if (!window.__omsMessageQueue) return [];
    const batch = window.__omsMessageQueue.splice(0);  // Take all, clear
    return batch;
}
"""

# JavaScript to check if the observer is still active
_HEALTH_CHECK_JS = """
() => {
    return {
        active:    !!window.__omsObserverActive,
        queueSize: (window.__omsMessageQueue || []).length,
        msgCount:  window.__omsMsgCounter || 0,
    };
}
"""

# JavaScript to stop the observer cleanly
_STOP_OBSERVER_JS = """
() => {
    if (window.__omsObserver) {
        window.__omsObserver.disconnect();
        window.__omsObserver = null;
    }
    window.__omsObserverActive = false;
    return { status: 'stopped' };
}
"""


class DOMObserver:
    '''
    Live observer that detects new WhatsApp messages via MutationObserver.

    Injects a JavaScript MutationObserver into the WhatsApp Web page.
    The JS fires immediately when a new message node is added to the DOM.
    Python polls the JS message queue every POLL_INTERVAL seconds.

    This design means:
      - Detection latency: ~0ms (JS reacts to DOM mutation instantly)
      - Python overhead: POLL_INTERVAL seconds (only to drain the queue)
      - No selector scanning in the Python poll loop
      - No missed messages (JS buffers everything between Python polls)

    Usage:
        observer = DOMObserver(page, cache, checkpoint_store, cfg)
        task = asyncio.create_task(observer.run())
        # ... later ...
        await observer.stop()
        await task
    '''

    POLL_INTERVAL = 2.0   # Seconds between JS queue drains

    def __init__(
        self,
        page,
        cache:            MessageCache,
        checkpoint_store: CheckpointStore,
        cfg,
    ):
        self._page       = page
        self._cache      = cache
        self._store      = checkpoint_store
        self._cfg        = cfg
        self._running    = False
        self._id_counter = 0
        self._msg_count  = 0   # Messages processed this session
        self._group_name = cfg.whatsapp.group_name

    async def run(self) -> None:
        '''
        Start the live observation loop.
        Injects the MutationObserver, then polls the queue.
        Runs until stop() is called or the task is cancelled.
        '''
        self._running = True
        log.info("Live DOM Observer starting...")

        # Inject the MutationObserver into the page
        injected = await self._inject_observer()
        if not injected:
            log.error(
                "Failed to inject MutationObserver. "
                "Live observation cannot start."
            )
            self._running = False
            return

        await dispatcher.emit(
            "observer.started",
            group=self._group_name,
            poll_interval=self.POLL_INTERVAL
        )

        log.info(
            f"DOM Observer active. Watching: {self._group_name!r}\n"
            f"Poll interval: {self.POLL_INTERVAL}s | "
            f"Detection: instant (MutationObserver)"
        )

        # Main poll loop — drains JS queue every POLL_INTERVAL seconds
        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
                await self._drain_queue()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(
                    f"Observer poll error: {e}\n"
                    "Attempting to re-inject observer..."
                )
                # Try to re-inject the observer if something went wrong
                try:
                    await self._inject_observer()
                except Exception as reinject_err:
                    log.error(f"Re-injection failed: {reinject_err}")
                    await asyncio.sleep(5)  # Brief backoff before retry

        await self._cleanup()
        await dispatcher.emit("observer.stopped", group=self._group_name)
        log.info("DOM Observer stopped.")

    async def stop(self) -> None:
        '''Signal the observer loop to stop cleanly.'''
        self._running = False

    async def _inject_observer(self) -> bool:
        '''
        Inject the JavaScript MutationObserver into the page.
        Returns True if injection succeeded.
        '''
        try:
            result = await self._page.evaluate(_MUTATION_OBSERVER_JS)
            status = result.get("status", "unknown")

            if status == "active":
                log.info(
                    f"MutationObserver injected. "
                    f"Container: {result.get('containerTag', '?')}"
                )
                return True

            elif status == "already_active":
                log.debug("MutationObserver already active — skipping injection.")
                return True

            elif status == "container_not_found":
                log.warning(
                    "WhatsApp chat container not found.\n"
                    "Is the group chat open in the browser?"
                )
                return False

            else:
                log.warning(f"Unexpected injection status: {status!r}")
                return False

        except Exception as e:
            log.error(f"Observer injection error: {e}", exc_info=True)
            return False

    async def _drain_queue(self) -> None:
        '''
        Drain the JavaScript message queue.
        Processes each queued message and emits events.
        Called every POLL_INTERVAL seconds by the main loop.
        '''
        try:
            batch = await self._page.evaluate(_DRAIN_QUEUE_JS)
        except Exception as e:
            log.debug(f"Queue drain error: {e}")
            return

        if not batch:
            return

        log.debug(f"Draining {len(batch)} message(s) from JS queue...")

        for data in batch:
            try:
                await self._process_queued_message(data)
            except Exception as e:
                log.warning(
                    f"Failed to process queued message: {e}\n"
                    f"Data: {data}"
                )
                # Continue processing remaining messages — one failure
                # must never stop the observer

    async def _process_queued_message(self, data: dict) -> None:
        '''
        Convert a raw JS data dict to a RawMessage and emit an event.
        Handles deduplication via MessageCache.
        Updates checkpoint after processing.
        '''
        # Build fingerprint for deduplication
        fingerprint = RawMessage.compute_fingerprint(
            sender    =data.get("sender", ""),
            timestamp =data.get("timestamp", ""),
            raw_text  =data.get("rawText", ""),
        )

        # Check for duplicate
        if self._cache.has_seen(fingerprint):
            log.debug(f"Duplicate skipped: {fingerprint[:8]!r}")
            return

        # Mark as seen before processing — prevents double-emit
        # even if something fails downstream
        self._cache.mark_seen(fingerprint)

        self._id_counter += 1
        self._msg_count  += 1

        msg = RawMessage(
            internal_id  =self._id_counter,
            fingerprint  =fingerprint,
            sender       =data.get("sender", "Unknown"),
            raw_text     =data.get("rawText", ""),
            timestamp    =data.get("timestamp", ""),
            direction    =MessageDirection(data.get("direction", "INCOMING")),
            group_name   =self._group_name,
            is_recovered =False,
        )

        log.info(
            f"  📨 New message #{self._msg_count}: "
            f"{msg.direction.value} | "
            f"{msg.sender!r} | "
            f"{msg.preview()!r}"
        )

        # Emit the live message event
        # Day 4's classifier subscribes to "message.received"
        await dispatcher.emit(
            "message.received",
            message=msg,
            group=self._group_name
        )

        # Update checkpoint after every live message
        # This ensures recovery can find where we left off
        self._store.save(Checkpoint(
            fingerprint    =fingerprint,
            timestamp_str  =msg.timestamp,
            saved_at       =Checkpoint.now_str(),
            message_preview=msg.preview(),
        ))

    async def _check_observer_health(self) -> bool:
        '''
        Verify the injected MutationObserver is still active.
        Called periodically to detect if the observer was cleared
        (e.g., WhatsApp Web reloaded parts of the DOM).
        '''
        try:
            result = await self._page.evaluate(_HEALTH_CHECK_JS)
            return result.get("active", False)
        except Exception:
            return False

    async def _cleanup(self) -> None:
        '''Stop the JavaScript MutationObserver cleanly.'''
        try:
            await self._page.evaluate(_STOP_OBSERVER_JS)
            log.debug("MutationObserver disconnected.")
        except Exception as e:
            log.debug(f"Observer cleanup error (non-critical): {e}")

    def stats(self) -> dict:
        '''Return observer statistics for diagnostics.'''
        return {
            "messages_processed": self._msg_count,
            "cache_stats":        self._cache.stats(),
            "poll_interval":      self.POLL_INTERVAL,
            "running":            self._running,
        }
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/infrastructure/browser/__init__.py
# ================================================================
# Update to expose Day 3 components.
# ================================================================
# ==============================================================

"""
from app.oms.infrastructure.browser.profile import BrowserProfile
from app.oms.infrastructure.browser.session_manager import (
    SessionManager,
    SessionState,
)
from app.oms.infrastructure.browser.health_check import BrowserHealthCheck
from app.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from app.oms.infrastructure.browser.raw_message import (
    RawMessage,
    MessageDirection,
)
from app.oms.infrastructure.browser.message_cache import MessageCache
from app.oms.infrastructure.browser.checkpoint_store import (
    CheckpointStore,
    Checkpoint,
)
from app.oms.infrastructure.browser.recovery_manager import RecoveryManager
from app.oms.infrastructure.browser.dom_observer import DOMObserver

__all__ = [
    "BrowserProfile",
    "SessionManager",
    "SessionState",
    "BrowserHealthCheck",
    "BrowserBootstrap",
    "RawMessage",
    "MessageDirection",
    "MessageCache",
    "CheckpointStore",
    "Checkpoint",
    "RecoveryManager",
    "DOMObserver",
]
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/config/settings.py
# ================================================================
# ADD ObserverSettings dataclass and add to OMSSettings.
# Find the existing settings.py and ADD these blocks.
# Do not replace the whole file — only add what is shown.
# ================================================================
# ADD this dataclass AFTER the RetrySettings dataclass:
# ==============================================================

"""
@dataclass
class ObserverSettings:
    '''
    Settings for the passive message observer and recovery manager.

    poll_interval_seconds: How often Python drains the JS message queue.
                           Lower = more responsive. Higher = less overhead.
                           Default: 2 seconds.

    recovery_max_age_hours: Maximum offline duration to attempt recovery.
                            If the OMS was offline longer than this, recovery
                            is skipped and the live observer starts fresh.
                            Default: 24 hours.

    recovery_max_messages:  Maximum number of missed messages to replay.
                            Prevents unbounded replay after a long outage.
                            Default: 500.

    recovery_max_scrolls:   Maximum scroll operations during recovery search.
                            Prevents infinite scrolling if checkpoint not found.
                            Default: 20.

    message_cache_size:     Maximum fingerprints in the in-memory cache.
                            Default: 1000.

    checkpoint_history_size: How many checkpoint fingerprints to persist.
                             More = higher recovery reliability.
                             Default: 5.
    '''
    poll_interval_seconds:    float = 2.0
    recovery_max_age_hours:   int   = 24
    recovery_max_messages:    int   = 500
    recovery_max_scrolls:     int   = 20
    message_cache_size:       int   = 1000
    checkpoint_history_size:  int   = 5
"""

# ADD this field to the OMSSettings dataclass (after retry field):
"""
    observer: ObserverSettings = field(default_factory=ObserverSettings)
"""

# ADD these to the _load_from_env() env_map dict:
"""
    "OMS_OBSERVER_POLL_INTERVAL":      ("observer.poll_interval_seconds",   float),
    "OMS_OBSERVER_MAX_AGE_HOURS":      ("observer.recovery_max_age_hours",  int),
    "OMS_OBSERVER_MAX_MESSAGES":       ("observer.recovery_max_messages",   int),
    "OMS_OBSERVER_MAX_SCROLLS":        ("observer.recovery_max_scrolls",    int),
    "OMS_OBSERVER_CACHE_SIZE":         ("observer.message_cache_size",      int),
    "OMS_OBSERVER_CHECKPOINT_HISTORY": ("observer.checkpoint_history_size", int),
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/infrastructure/browser/session_manager.py
# ================================================================
# ADD group navigation method.
# After login is confirmed, navigate to the target WhatsApp group.
# This is the ONLY navigation after the initial WhatsApp Web load.
# After the group is opened, the browser never navigates again.
#
# ADD this method to SessionManager class (after _handle_qr_scan):
# ================================================================
# ==============================================================

"""
    async def open_target_group(self, group_name: str) -> bool:
        '''
        Navigate to the configured WhatsApp group.

        This is called ONCE after login. After this method returns,
        the chat is open and the browser never navigates again.
        The DOMObserver watches this open chat for new messages.

        Strategy:
          1. Click the WhatsApp search input
          2. Type the group name character by character
          3. Wait for the group to appear in results
          4. Click the group result
          5. Verify the group chat is open

        Returns:
            True if group was found and opened successfully.
            False if group could not be found.
        '''
        import asyncio, random

        SEARCH_SEL = 'input[aria-label="Search or start a new chat"]'

        log.info(f"Opening target group: {group_name!r}")

        # ── Click search bar ─────────────────────────────────────
        try:
            search_el = await self._page.wait_for_selector(
                SEARCH_SEL,
                timeout=8_000
            )
            await search_el.click()
            await asyncio.sleep(random.uniform(0.3, 0.7))
        except Exception as e:
            log.error(f"Could not find search bar: {e}")
            return False

        # ── Clear and type group name ────────────────────────────
        await self._page.keyboard.press("Control+a")
        await asyncio.sleep(0.1)
        await self._page.keyboard.press("Delete")
        await asyncio.sleep(0.2)

        for char in group_name:
            await self._page.keyboard.type(
                char,
                delay=random.uniform(60, 140)
            )

        log.debug(f"Typed group name: {group_name!r}")
        await asyncio.sleep(random.uniform(1.2, 2.0))

        # ── Click the group result ───────────────────────────────
        result_sels = [
            f'span[title="{group_name}"]',
            f'span[title*="{group_name[:20]}"]',
            'div[data-testid="cell-frame-container"]:first-child',
            'div[role="listitem"]:first-child',
        ]

        clicked = False
        for sel in result_sels:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    clicked = True
                    log.debug(f"Clicked group result: {sel}")
                    break
            except Exception:
                continue

        if not clicked:
            await self._page.keyboard.press("Enter")
            log.debug("Used Enter to select top result")

        # ── Verify group chat opened ─────────────────────────────
        await asyncio.sleep(random.uniform(1.5, 2.5))

        try:
            # Check that the group header shows the expected name
            header_sels = [
                f'header span[title="{group_name}"]',
                f'span[title="{group_name}"]',
                'div[data-testid="conversation-header"]',
                'header',
            ]
            for sel in header_sels:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    log.info(f"Group chat opened: {group_name!r}")
                    return True
        except Exception:
            pass

        # Fallback — check if we're no longer on the home screen
        search_still_focused = await self._page.query_selector(SEARCH_SEL)
        if search_still_focused:
            log.warning(f"Could not confirm group opened: {group_name!r}")
            return False

        log.info(f"Group chat likely open: {group_name!r}")
        return True
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/oms_runner.py
# ================================================================
# FULL REPLACE — wires together Day 2 + Day 3 components.
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.oms.config.settings import get_settings
from app.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from app.oms.infrastructure.browser.raw_message import RawMessage
from app.oms.infrastructure.browser.message_cache import MessageCache
from app.oms.infrastructure.browser.checkpoint_store import CheckpointStore
from app.oms.infrastructure.browser.recovery_manager import RecoveryManager
from app.oms.infrastructure.browser.dom_observer import DOMObserver
from app.oms.shared.logger import get_logger
from app.oms.events import dispatcher

log = get_logger("oms.runner")


# ── Event listeners ─────────────────────────────────────────────

@dispatcher.on("browser.connected")
async def on_browser_connected(**kwargs):
    log.info(f"Browser connected — {kwargs.get('state')}")


@dispatcher.on("recovery.started")
async def on_recovery_started(**kwargs):
    log.info(f"Recovery started for group: {kwargs.get('group')!r}")


@dispatcher.on("recovery.skipped")
async def on_recovery_skipped(**kwargs):
    log.info(f"Recovery skipped: {kwargs.get('reason')}")


@dispatcher.on("recovery.completed")
async def on_recovery_completed(**kwargs):
    log.info(
        f"Recovery complete — "
        f"{kwargs.get('recovered_count', 0)} message(s) replayed"
    )


@dispatcher.on("observer.started")
async def on_observer_started(**kwargs):
    log.info(
        f"Live observer active — watching: {kwargs.get('group')!r}\n"
        f"Waiting for new messages..."
    )


@dispatcher.on("message.received")
async def on_message_received(message: RawMessage, **kwargs):
    '''
    Called for every new live message detected in the group.
    Day 4's classifier will replace this with actual processing.
    '''
    log.info(
        f"MESSAGE RECEIVED:\n"
        f"  Sender:    {message.sender}\n"
        f"  Direction: {message.direction.value}\n"
        f"  Time:      {message.timestamp}\n"
        f"  Text:      {message.preview()}\n"
        f"  Fingerprint: {message.fingerprint}"
    )


@dispatcher.on("message.recovered")
async def on_message_recovered(message: RawMessage, **kwargs):
    log.info(
        f"RECOVERED MESSAGE:\n"
        f"  Sender: {message.sender}\n"
        f"  Time:   {message.timestamp}\n"
        f"  Text:   {message.preview()}"
    )


@dispatcher.on("observer.stopped")
async def on_observer_stopped(**kwargs):
    log.info("Observer stopped.")


async def main():
    log.info("Windwhirl OMS starting — Day 3...")

    settings = get_settings()

    # For testing: set these directly if env vars not configured
    # settings.whatsapp.group_name  = "Your Group Name Here"
    # settings.whatsapp.staff_number = "2348XXXXXXXXX"

    bootstrap = BrowserBootstrap(settings)
    observer_task = None

    try:
        # ── Start browser and log in ──────────────────────────────
        await bootstrap.start()

        # ── Open the target WhatsApp group ────────────────────────
        if settings.whatsapp.group_name:
            opened = await bootstrap.session_manager.open_target_group(
                settings.whatsapp.group_name
            )
            if not opened:
                log.warning(
                    f"Could not open group: {settings.whatsapp.group_name!r}\n"
                    "Check the group name in settings."
                )
        else:
            log.warning(
                "whatsapp.group_name not configured.\n"
                "Set OMS_WHATSAPP_GROUP_NAME or edit settings.py.\n"
                "Observer will not start without a group."
            )
            await bootstrap.run_forever()
            return

        page = bootstrap.page

        # ── Initialise shared components ──────────────────────────
        checkpoint_store = CheckpointStore(
            group_name  =settings.whatsapp.group_name,
            data_dir    ="data",
            max_history =settings.observer.checkpoint_history_size,
        )
        cache = MessageCache(
            max_size=settings.observer.message_cache_size
        )

        # ── Run Recovery Manager ──────────────────────────────────
        # Recovery runs ONCE at startup before live observer begins
        recovery = RecoveryManager(
            page             =page,
            checkpoint_store =checkpoint_store,
            cache            =cache,
            cfg              =settings,
        )
        await recovery.run()

        # ── Start Live DOM Observer ───────────────────────────────
        # Recovery is done — hand off to live observation
        observer = DOMObserver(
            page             =page,
            cache            =cache,
            checkpoint_store =checkpoint_store,
            cfg              =settings,
        )
        observer_task = asyncio.create_task(
            observer.run(),
            name="oms_dom_observer"
        )

        # ── Wait forever (browser + observer running) ─────────────
        await bootstrap.run_forever()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down.")
    except Exception as e:
        log.error(f"OMS runner error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Stop observer first, then browser
        if observer_task and not observer_task.done():
            observer_task.cancel()
            try:
                await observer_task
            except asyncio.CancelledError:
                pass
        await bootstrap.stop()
        log.info("Windwhirl OMS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
"""


# ==============================================================
# DAY 3 VERIFICATION
# ==============================================================
#
# Test 1 — Imports resolve:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.infrastructure.browser.raw_message import RawMessage
#   from app.oms.infrastructure.browser.message_cache import MessageCache
#   from app.oms.infrastructure.browser.checkpoint_store import CheckpointStore
#   from app.oms.infrastructure.browser.recovery_manager import RecoveryManager
#   from app.oms.infrastructure.browser.dom_observer import DOMObserver
#   print('All Day 3 imports OK')
#   "
#
# Test 2 — Fingerprint is deterministic:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.infrastructure.browser.raw_message import RawMessage
#   fp1 = RawMessage.compute_fingerprint('Blessing', '14:32', 'I want 2 Sadoer sets')
#   fp2 = RawMessage.compute_fingerprint('Blessing', '14:32', 'I want 2 Sadoer sets')
#   fp3 = RawMessage.compute_fingerprint('Blessing', '14:32', 'Different text')
#   assert fp1 == fp2, 'Same input must produce same fingerprint'
#   assert fp1 != fp3, 'Different text must produce different fingerprint'
#   print('Fingerprint OK:', fp1)
#   "
#
# Test 3 — Cache LRU eviction:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.infrastructure.browser.message_cache import MessageCache
#   cache = MessageCache(max_size=3)
#   cache.mark_seen('fp1')
#   cache.mark_seen('fp2')
#   cache.mark_seen('fp3')
#   assert cache.size == 3
#   cache.mark_seen('fp4')   # Should evict fp1
#   assert cache.size == 3
#   assert not cache.has_seen('fp1'), 'fp1 should be evicted'
#   assert cache.has_seen('fp4'), 'fp4 should be in cache'
#   print('Cache LRU eviction OK. Stats:', cache.stats())
#   "
#
# Test 4 — Checkpoint store read/write:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.infrastructure.browser.checkpoint_store import CheckpointStore, Checkpoint
#   store = CheckpointStore('test_group', data_dir='data', max_history=3)
#   store.save(Checkpoint('fp_abc123', '14:32', Checkpoint.now_str(), 'Test msg'))
#   store.save(Checkpoint('fp_def456', '14:35', Checkpoint.now_str(), 'Test msg 2'))
#   history = store.load_history()
#   assert len(history) == 2
#   assert history[0].fingerprint == 'fp_def456'   # Most recent first
#   assert history[1].fingerprint == 'fp_abc123'
#   print('Checkpoint store OK. Latest:', store.latest())
#   store.clear()
#   "
#
# Test 5 — Full integration (interactive — requires running browser from Day 2):
#   python oms_runner.py
#   Configure group name in settings first.
#   Expected: browser opens, group is found, observer starts,
#   new messages in the group appear in the console.
#
# ==============================================================
# WHAT DAY 4 BUILDS
# ==============================================================
# Day 4: Message Classification Engine
#   - Consumes "message.received" events from today's observer
#   - Classifies each RawMessage into: ORDER | ASSIGNMENT | SYSTEM | UNKNOWN
#   - Emits "message.classified" events with classification result
#   - Does NOT parse order details
#   - Does NOT assign workers
#   - Only routes messages into the correct pipeline
#
# Day 4 registers a handler:
#   @dispatcher.on("message.received")
#   async def classify(message: RawMessage, **kwargs):
#       classification = classifier.classify(message)
#       await dispatcher.emit("message.classified", ...)
#
# No changes to Day 3 files. Clean event-driven handoff.
# ==================================================