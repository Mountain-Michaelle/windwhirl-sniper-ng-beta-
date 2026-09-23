import asyncio
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

# ------------------------------------------------------------------
# Primary selector for the message container.
#
# FIXED: was "#main div[data-testid='conversation-panel-messages']" —
# that testid no longer exists in current WhatsApp Web (confirmed
# absent from a live DOM dump). Every startup/reconnect was burning
# a full 30s timeout waiting on a selector that can never match.
#
# "#main" itself is the long-standing, stable root of the open chat
# panel — it's an actual element id, not one of Meta's rotating
# atomic CSS classes, so it's a much safer anchor to wait on.
# ------------------------------------------------------------------
PRIMARY_CONTAINER_SELECTOR = "#main"

# Timeout for waiting on the container element to appear.
CONTAINER_WAIT_TIMEOUT = 30_000  # ms

# How many poll cycles between proactive health checks.
# POLL_INTERVAL is 2.0s, so 15 cycles ≈ every 30s.
HEALTH_CHECK_EVERY_N_POLLS = 15

# ------------------------------------------------------------------
# JavaScript MutationObserver injected into the page.
# This watches the WhatsApp chat container for new message nodes.
# When a new message is inserted, it extracts the text, sender,
# timestamp, and direction, then pushes to a global JS queue.
# Python reads this queue via page.evaluate() on a timer.
# ------------------------------------------------------------------
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

    // Find the chat message container.
    //
    // FIXED: "#main" is now checked FIRST and is the primary target.
    // The old order let narrower, single-message elements (like
    // [data-testid="msg-container"], which belongs to ONE message
    // bubble, not the list) match before #main — an observer attached
    // there never sees new messages inserted elsewhere in the DOM.
    // #main reliably wraps the entire scrollable conversation, so
    // watching it with subtree:true catches every new message row
    // no matter how WhatsApp nests things internally.
    const findContainer = () => {
        return (
            document.querySelector('#main') ||
            // Fallbacks only used if #main itself isn't found yet —
            // should be rare, since #main is present as soon as any
            // chat is open.
            document.querySelector('div[data-testid="conversation-panel-messages"]') ||
            document.querySelector('div[role="application"]') ||
            document.querySelector('#app')
        );
    };

    const container = findContainer();
    if (!container) {
        window.__omsObserverActive = false;
        return { status: 'container_not_found' };
    }

    // Extract message data from a DOM node.
    //
    // FIXED (row matching): messages are identified by
    // data-testid="conv-msg-<id>" — confirmed present on every
    // message row in a live DOM capture. This is a semantic,
    // functional attribute (WhatsApp needs it to address individual
    // messages), unlike the atomic/obfuscated "xNNNNNNN" classes,
    // so it's a much more durable anchor.
    //
    // FIXED (direction): the old check looked for ".message-in" /
    // ".message-out" classes that do not exist anywhere in current
    // WhatsApp Web markup — every message was silently dropped here,
    // regardless of who sent it. Direction is now inferred from the
    // delivery/read-receipt tick inside [data-testid="msg-meta"]:
    // you only ever get a Sent/Delivered/Read tick on messages YOU
    // sent. No tick present = incoming, from someone else.
    const extractMessage = (node) => {
        try {
            let msgNode = node;

            // The added node is often a plain wrapper <div> containing
            // the actual message element — find the real message node
            // by its data-testid, whether it's the node itself or a
            // descendant.
            if (!msgNode.getAttribute ||
                !(msgNode.getAttribute('data-testid') || '').startsWith('conv-msg-')) {
                const found = msgNode.querySelector &&
                    msgNode.querySelector('[data-testid^="conv-msg-"]');
                if (!found) return null;
                msgNode = found;
            }

            // Get text content
            const textEl = msgNode.querySelector(
                '[data-testid="selectable-text"], .copyable-text span[dir]'
            );
            const rawText = textEl ? textEl.innerText.trim() : '';
            if (!rawText) return null;

            // Get timestamp and sender from the data-pre-plain-text
            // attribute, e.g. "[2:36 PM, 7/13/2026] Michael Nabeau: "
            const tsEl    = msgNode.querySelector('[data-pre-plain-text]');
            const tsAttr  = tsEl ? tsEl.getAttribute('data-pre-plain-text') : '';
            const tsMatch = tsAttr.match(/\\[([^,]+),/);
            const snMatch = tsAttr.match(/\\] ([^:]+):/);

            // Direction: presence of a status tick (Sent/Delivered/Read)
            // inside msg-meta means this message was sent BY the
            // logged-in account — i.e. outgoing.
            const metaEl = msgNode.querySelector('[data-testid="msg-meta"]');
            const hasStatusTick = !!(metaEl && metaEl.querySelector('[data-icon], [aria-label*="Sent"], [aria-label*="Delivered"], [aria-label*="Read"]'));
            const isOutgoing = hasStatusTick;

            window.__omsMsgCounter++;

            return {
                rawText:      rawText,
                timestamp:    tsMatch ? tsMatch[1].trim() : '',
                sender:       snMatch ? snMatch[1].trim() : (isOutgoing ? 'You' : 'Unknown'),
                direction:    isOutgoing ? 'OUTGOING' : 'INCOMING',
                capturedMs:   Date.now(),
                queueIdx:     window.__omsMsgCounter,
            };
        } catch(e) {
            return null;    // Skip malformed nodes silently
        }
    };

    // Create the MutationObserver
    const observer = new MutationObserver((mutations) => {
        // Track testids already handled in this batch of mutations —
        // prevents the same message being queued twice when both the
        // wrapper node AND one of its children match independently.
        const seenInBatch = new Set();

        const tryExtract = (node) => {
            const msg = extractMessage(node);
            if (!msg) return;
            const key = msg.timestamp + '|' + msg.sender + '|' + msg.rawText.slice(0, 40);
            if (seenInBatch.has(key)) return;
            seenInBatch.add(key);
            window.__omsMessageQueue.push(msg);
        };

        mutations.forEach((mutation) => {
            mutation.addedNodes.forEach((node) => {
                if (node.nodeType !== Node.ELEMENT_NODE) return;

                tryExtract(node);

                if (node.children) {
                    Array.from(node.children).forEach((child) => tryExtract(child));
                }
            });
        });
    });

    // Observe the container for child additions.
    // subtree: true — watch all descendants, not just direct children.
    // childList: true — react to added/removed nodes.
    observer.observe(container, {
        childList: true,
        subtree:   true,
    });

    // Store reference to allow cleanup later
    window.__omsObserver = observer;

    return { status: 'active', containerTag: container.tagName, containerId: container.id || '' };
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
    Python polls the JS message queue every POLL_INTERVAL seconds, and
    every HEALTH_CHECK_EVERY_N_POLLS cycles it also verifies the observer
    is still alive, re-injecting if WhatsApp silently tore it down.

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
        self._polls_since_health_check = 0

    # ----------------------------------------------------------------
    #  Public API
    # ----------------------------------------------------------------

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

        # Main poll loop — drains JS queue every POLL_INTERVAL seconds,
        # and periodically confirms the observer is still actually alive.
        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
                await self._drain_queue()

                self._polls_since_health_check += 1
                if self._polls_since_health_check >= HEALTH_CHECK_EVERY_N_POLLS:
                    self._polls_since_health_check = 0
                    await self._ensure_observer_alive()

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

    def stats(self) -> dict:
        '''Return observer statistics for diagnostics.'''
        return {
            "messages_processed": self._msg_count,
            "cache_stats":        self._cache.stats(),
            "poll_interval":      self.POLL_INTERVAL,
            "running":            self._running,
        }

    # ----------------------------------------------------------------
    #  Private – injection
    # ----------------------------------------------------------------

    async def _inject_observer(self) -> bool:
        '''
        Inject the JavaScript MutationObserver into the page.

        Waits for PRIMARY_CONTAINER_SELECTOR ("#main") to appear before
        attempting injection, eliminating the "container not found"
        timing race. Falls through to a short retry loop as a safety
        net for transient page glitches.
        '''
        try:
            await self._page.wait_for_selector(
                PRIMARY_CONTAINER_SELECTOR,
                state="attached",
                timeout=CONTAINER_WAIT_TIMEOUT,
            )
            log.debug(
                f"Primary container '{PRIMARY_CONTAINER_SELECTOR}' "
                "is present in the DOM."
            )
        except Exception as e:
            log.warning(
                f"Timed out waiting for primary container: {e}\n"
                "Will still attempt injection with fallback selectors."
            )

        for attempt in range(1, self.MAX_INJECT_ATTEMPTS + 1):
            try:
                result = await self._page.evaluate(_MUTATION_OBSERVER_JS)
                status = result.get("status", "unknown")

                if status == "active":
                    log.info(
                        f"MutationObserver injected "
                        f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS}). "
                        f"Container: <{result.get('containerTag', '?')} "
                        f"id={result.get('containerId', '')!r}>"
                    )
                    return True

                elif status == "already_active":
                    log.debug("MutationObserver already active — skipping injection.")
                    return True

                elif status == "container_not_found":
                    log.debug(
                        f"Chat container not ready "
                        f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS}) "
                        f"— retrying in {self.INJECT_RETRY_DELAY}s..."
                    )
                    if attempt < self.MAX_INJECT_ATTEMPTS:
                        await asyncio.sleep(self.INJECT_RETRY_DELAY)
                    continue

                else:
                    log.warning(
                        f"Unexpected injection status: {status!r} "
                        f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS})"
                    )
                    if attempt < self.MAX_INJECT_ATTEMPTS:
                        await asyncio.sleep(self.INJECT_RETRY_DELAY)
                    continue

            except Exception as e:
                log.debug(
                    f"Injection error "
                    f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS}): {e}"
                )
                if attempt < self.MAX_INJECT_ATTEMPTS:
                    await asyncio.sleep(self.INJECT_RETRY_DELAY)

        log.error(
            f"Failed to inject MutationObserver after "
            f"{self.MAX_INJECT_ATTEMPTS} attempts "
            f"({self.MAX_INJECT_ATTEMPTS * self.INJECT_RETRY_DELAY:.0f}s total).\n"
            "Check that the group chat is visible and fully loaded in the browser."
        )
        return False

    # ----------------------------------------------------------------
    #  Private – queue processing
    # ----------------------------------------------------------------

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

    # ----------------------------------------------------------------
    #  Private – health & cleanup
    # ----------------------------------------------------------------

    async def _check_observer_health(self) -> bool:
        '''
        Verify the injected MutationObserver is still active.
        '''
        try:
            result = await self._page.evaluate(_HEALTH_CHECK_JS)
            return result.get("active", False)
        except Exception:
            return False

    async def _ensure_observer_alive(self) -> None:
        '''
        FIXED: previously _check_observer_health() existed but nothing
        ever called it, so a silently-cleared observer (e.g. WhatsApp
        replacing the chat panel's DOM on a re-render) would never
        recover — drain_queue() just kept returning [] forever with no
        error. This is now called every HEALTH_CHECK_EVERY_N_POLLS
        cycles from the main loop and re-injects if the observer died.
        '''
        alive = await self._check_observer_health()
        if not alive:
            log.warning(
                "MutationObserver health check failed — observer is no "
                "longer active. Re-injecting..."
            )
            await self._inject_observer()
        else:
            log.debug("MutationObserver health check OK — still active.")

    async def _cleanup(self) -> None:
        '''Stop the JavaScript MutationObserver cleanly.'''
        try:
            await self._page.evaluate(_STOP_OBSERVER_JS)
            log.debug("MutationObserver disconnected.")
        except Exception as e:
            log.debug(f"Observer cleanup error (non-critical): {e}")

    # ----------------------------------------------------------------
    #  Class constants (kept as instance-friendly for clarity)
    # ----------------------------------------------------------------
    MAX_INJECT_ATTEMPTS = 10
    INJECT_RETRY_DELAY  = 2.0