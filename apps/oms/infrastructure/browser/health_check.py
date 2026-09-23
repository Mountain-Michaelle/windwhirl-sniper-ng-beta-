import asyncio
from typing import Optional

from apps.oms.infrastructure.browser.session_manager import (
    SessionManager,
    SessionState,
)
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class BrowserHealthCheck:
    '''
    Background task that monitors the WhatsApp Web session health.

    Runs on a configurable interval. On each check:
      1. Asks SessionManager if WhatsApp is logged in
      2. If yes → all good, continue
      3. If no  → emit "browser.disconnected" event
                → attempt reconnect via SessionManager
                → emit "browser.connected" event on success

    Usage:
        health = BrowserHealthCheck(session_manager, interval_seconds=60)
        task   = asyncio.create_task(health.run())
        # ... later ...
        health.stop()
        await task
    '''

    # Strings in the page that indicate session has expired
    EXPIRY_SIGNALS = [
        "click here to reload",
        "reload whatsapp",
        "phone not connected",
        "trying to connect",
    ]

    def __init__(
        self,
        session_manager:  SessionManager,
        interval_seconds: int = 60,
    ):
        '''
        Args:
            session_manager:  The active SessionManager to check.
            interval_seconds: How often to check session health.
                              Lower = more responsive but more browser activity.
                              Default: 60 seconds.
        '''
        self._manager   = session_manager
        self._interval  = interval_seconds
        self._running   = False
        self._check_count = 0
        self._fail_count  = 0

    async def run(self) -> None:
        '''
        Start the health check loop.
        Runs until stop() is called or the task is cancelled.

        This is intended to run as a background asyncio task:
            task = asyncio.create_task(health_check.run())
        '''
        self._running = True
        log.info(
            f"Browser health check started "
            f"(interval: {self._interval}s)"
        )

        while self._running:
            await asyncio.sleep(self._interval)

            if not self._running:
                break

            await self._perform_check()

        log.info("Browser health check stopped.")

    def stop(self) -> None:
        '''Signal the health check loop to stop after its current check.'''
        self._running = False

    async def _perform_check(self) -> None:
        '''
        Execute one health check cycle.
        Called automatically by the run() loop.
        '''
        self._check_count += 1
        log.debug(
            f"Health check #{self._check_count} — "
            f"state={self._manager.state.value}"
        )

        # Skip check if session is in a transitional state
        if self._manager.state in (
            SessionState.LAUNCHING,
            SessionState.LOADING,
            SessionState.AWAITING_QR,
            SessionState.STOPPED,
        ):
            log.debug("  Skipping — session in transitional state.")
            return

        # Check if still logged in
        is_ok = await self._manager.is_logged_in()

        # Also check for expiry signals in the page
        if is_ok and self._manager.page:
            is_ok = not await self._check_expiry_signals()

        if is_ok:
            self._fail_count = 0
            log.debug("  Session healthy ✓")
            return

        # Session appears unhealthy
        self._fail_count += 1
        log.warning(
            f"  Session check FAILED "
            f"(consecutive failures: {self._fail_count})"
        )

        # Only reconnect after 2 consecutive failures
        # This avoids false alarms from momentary network blips
        if self._fail_count >= 2:
            await self._trigger_reconnect()

    async def _check_expiry_signals(self) -> bool:
        '''
        Check the page text for WhatsApp session expiry messages.
        Returns True if expiry signals are detected.
        '''
        try:
            body = (
                await self._manager.page.inner_text("body")
            ).lower()

            for signal in self.EXPIRY_SIGNALS:
                if signal in body:
                    log.warning(
                        f"  Session expiry signal detected: {signal!r}"
                    )
                    return True

            return False

        except Exception as e:
            log.debug(f"  Expiry check error: {e}")
            return False

    async def _trigger_reconnect(self) -> None:
        '''
        Attempt to reconnect the browser session.
        Emits browser events before and after reconnect attempt.

        After successful reconnect, emits "browser.reconnected" so
        the main loop can re-open the target group and restart
        the DOM observer with the new page.
        '''
        log.warning(
            "Session confirmed unhealthy. Triggering reconnect..."
        )

        await dispatcher.emit(
            "browser.disconnected",
            reason     ="Health check detected session loss",
            fail_count =self._fail_count,
        )

        try:
            await self._manager.reconnect()
            self._fail_count = 0

            await dispatcher.emit(
                "browser.connected",
                source="health_check_reconnect",
                state =self._manager.state.value,
            )

            # ── THIS IS THE KEY ADDITION ──────────────────────────
            # Emit "browser.reconnected" so the main loop knows
            # to re-open the group and restart the DOM observer.
            # The new page is available at session_manager.page
            await dispatcher.emit(
                "browser.reconnected",
                source   ="health_check",
                new_page =self._manager.page,   # Pass the new page directly
            )

            log.info(
                "Reconnect successful. "
                "Emitted browser.reconnected — observer will restart."
            )

        except Exception as e:
            log.error(
                f"Reconnect failed: {e}\\n"
                f"OMS browser is not operational. Manual restart required.",
                exc_info=True
            )

            await dispatcher.emit(
                "browser.error",
                error  =str(e),
                source ="health_check_reconnect"
            )
    
    
    def stats(self) -> dict:
        '''Return health check statistics for diagnostics.'''
        return {
            "checks_performed":    self._check_count,
            "consecutive_failures": self._fail_count,
            "check_interval_s":    self._interval,
            "is_running":          self._running,
        }