import asyncio
import signal
from typing import Optional

from apps.oms.config.settings import OMSSettings
from apps.oms.infrastructure.browser.profile import BrowserProfile
from apps.oms.infrastructure.browser.session_manager import (
    SessionManager,
    SessionState,
)
from apps.oms.infrastructure.browser.health_check import BrowserHealthCheck
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger
from apps.oms.shared.exceptions import InfrastructureException

log = get_logger(__name__)


class BrowserBootstrap:
    '''
    Assembles and manages the complete browser infrastructure.

    Wires together:
        BrowserProfile      → persistent session directory
        SessionManager      → browser lifecycle, WhatsApp login
        BrowserHealthCheck  → periodic session health monitoring

    Usage:
        settings  = get_settings()
        bootstrap = BrowserBootstrap(settings)

        await bootstrap.start()         # Browser opens, WhatsApp loads
        page = bootstrap.page           # Live page (for Day 3 DOM observer)
        await bootstrap.run_forever()   # Blocks until Ctrl+C or stop()
        await bootstrap.stop()          # Clean shutdown

    Or use as async context manager:
        async with BrowserBootstrap(settings) as bootstrap:
            page = bootstrap.page
            # Day 3 DOM observer will run here
    '''

    def __init__(self, settings: OMSSettings):
        self._settings = settings

        # Assemble infrastructure components
        self._profile = BrowserProfile(
            session_dir=settings.browser.session_dir
        )
        self._session = SessionManager(
            profile=self._profile,
            cfg=settings
        )
        self._health = BrowserHealthCheck(
            session_manager=self._session,
            interval_seconds=60   # Check session health every 60s
        )

        self._health_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    @property
    def session_manager(self) -> SessionManager:
        '''The active SessionManager. Day 3 reads its .page attribute.'''
        return self._session

    @property
    def page(self):
        '''
        The live Playwright page with WhatsApp Web loaded.
        Returns None if browser is not started.

        Day 3 (DOM Observer) receives this page and attaches
        MutationObservers to detect new messages.
        Day 4+ can call other browser interactions through this page.

        NEVER call goto() or navigate on this page after bootstrap.start().
        '''
        return self._session.page

    async def start(self) -> None:
        '''
        Start the browser, load WhatsApp Web, begin health monitoring.

        After this method returns:
          - Browser is open with WhatsApp Web loaded
          - User is logged in (QR scanned if needed)
          - Health check is running in the background
          - self.page is available for Day 3 to use

        Emits: "browser.connected" event on success.
        '''
        log.info(
            f"\n{'=' * 55}\n"
            f"  WINDWHIRL OMS — BROWSER STARTUP\n"
            f"  Group:   {self._settings.whatsapp.group_name or '(not configured)'}\n"
            f"  Session: {self._settings.browser.session_dir}\n"
            f"{'=' * 55}"
        )

        # Validate config before touching the browser
        try:
            self._settings.validate()
        except Exception as e:
            log.warning(
                f"Configuration incomplete: {e}\n"
                "Continuing with partial config for Day 2 testing."
            )

        # Start the browser session
        await self._session.start()

        # Start health monitoring as a background task
        self._health_task = asyncio.create_task(
            self._health.run(),
            name="oms_health_check"
        )

        # Emit connected event — Day 3 listeners will start here
        await dispatcher.emit(
            "browser.connected",
            state=self._session.state.value,
            source="bootstrap"
        )

        log.info(
            f"\n{'=' * 55}\n"
            f"  OMS BROWSER READY\n"
            f"  State:   {self._session.state.value}\n"
            f"  Profile: {self._profile.path}\n"
            f"  Day 3 will attach the message observer to this session.\n"
            f"{'=' * 55}"
        )

    async def stop(self) -> None:
        '''
        Stop health monitoring and close the browser cleanly.
        Preserves the session profile for next startup.
        '''
        log.info("Shutting down OMS browser...")

        # Stop health check loop
        self._health.stop()
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Stop browser session
        await self._session.stop()

        # Signal the run_forever loop to exit
        self._stop_event.set()

        await dispatcher.emit("browser.disconnected", reason="shutdown")
        log.info("OMS browser shutdown complete.")

    async def run_forever(self) -> None:
        '''
        Block until stop() is called or Ctrl+C is pressed.

        In Day 2 (today): just keeps the browser open.
        In Day 3: the DOM observer runs alongside this.
        In production: this is the main application loop.

        Handles Ctrl+C gracefully — calls stop() before exiting.
        '''
        log.info(
            "OMS browser running. Press Ctrl+C to stop.\n"
            "Day 3 will add message monitoring to this session."
        )

        # Register signal handlers for clean shutdown
        loop = asyncio.get_event_loop()

        def _handle_signal():
            log.info("Shutdown signal received.")
            asyncio.create_task(self.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                # Windows does not support add_signal_handler
                # Ctrl+C will be caught by the KeyboardInterrupt below
                pass

        try:
            await self._stop_event.wait()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received.")
            await self.stop()

    # ── Async context manager support ──────────────────────────

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False   # Do not suppress exceptions