import asyncio
from datetime import datetime
from typing import Optional

from apps.oms.infrastructure.browser.raw_message import (
    RawMessage,
    MessageDirection,
)
from apps.oms.infrastructure.browser.message_cache import MessageCache
from apps.oms.infrastructure.browser.checkpoint_store import (
    CheckpointStore,
    Checkpoint,
)
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

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