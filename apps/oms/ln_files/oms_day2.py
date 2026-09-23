# ==============================================================
# WINDWHIRL OMS — DAY 2: BROWSER INFRASTRUCTURE
# ==============================================================
# This document contains all files for Day 2.
#
# MISSION:
#   Build the browser layer that acts like a staff member who
#   opens WhatsApp Web every morning and leaves it open all day.
#   Passive. Watching. Never refreshing. Never navigating away.
#
# WHAT IS BUILT TODAY:
#   - BrowserProfile     → manages the persistent Chromium profile
#   - SessionManager     → launches browser, handles QR, stays alive
#   - BrowserHealthCheck → detects if session has expired
#   - BrowserBootstrap   → entry point that wires everything together
#
# WHAT IS NOT BUILT TODAY:
#   ❌ Message reading
#   ❌ DOM observation
#   ❌ Parsing
#   ❌ Order detection
#   ❌ Any interaction after login
#
# ARCHITECTURE:
#   All browser code lives in infrastructure/.
#   It implements the ISessionManager interface from Day 1.
#   The domain never imports anything from this folder.
#
# SESSION SEPARATION:
#   The OMS uses its own persistent browser profile completely
#   separate from the review automation.
#   Review automation: .sessions/whatsapp_session/
#   OMS:              .sessions/oms_session/
#   This means two separate WhatsApp Web logins.
#   They never interfere with each other.
#
# FOLDER ADDITIONS TODAY:
#
#   windwhirl/app/oms/infrastructure/
#   ├── __init__.py                    ← update existing
#   └── browser/
#       ├── __init__.py                ← FILE 1
#       ├── profile.py                 ← FILE 2
#       ├── session_manager.py         ← FILE 3
#       ├── health_check.py            ← FILE 4
#       └── bootstrap.py              ← FILE 5
#
# ALSO UPDATE:
#   FILE 6 → app/oms/infrastructure/__init__.py  (update)
#   FILE 7 → app/oms/config/settings.py          (BrowserSettings already correct)
#
# BUILD ORDER:
#   FILE 1 → infrastructure/browser/__init__.py
#   FILE 2 → infrastructure/browser/profile.py
#   FILE 3 → infrastructure/browser/session_manager.py
#   FILE 4 → infrastructure/browser/health_check.py
#   FILE 5 → infrastructure/browser/bootstrap.py
#   FILE 6 → infrastructure/__init__.py  (update)
#   FILE 7 → oms_runner.py  (project root — entry point for testing)
#
# VERIFICATION:
#   python oms_runner.py
#   → Browser opens
#   → WhatsApp Web loads
#   → QR code appears (if first run) or session loads silently
#   → Console shows: "OMS browser ready. Monitoring will begin in Day 3."
#   → Browser stays open until Ctrl+C
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/infrastructure/browser/__init__.py
# ================================================================
# Marks browser/ as a Python package.
# Exposes the public browser API for other infrastructure modules.
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

__all__ = [
    "BrowserProfile",
    "SessionManager",
    "SessionState",
    "BrowserHealthCheck",
    "BrowserBootstrap",
]
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/infrastructure/browser/profile.py
# ================================================================
# PURPOSE:
#   Manages the persistent Chromium browser profile directory.
#   A persistent profile stores:
#     - Cookies (WhatsApp login session)
#     - localStorage (WhatsApp Web state)
#     - Session tokens
#   This is what makes WhatsApp Web stay logged in between runs.
#
# WHY A SEPARATE CLASS:
#   Profile management is its own concern.
#   Session management (what happens after the browser opens) is
#   a different concern. Separating them keeps each class focused.
#
# SEPARATION FROM REVIEW AUTOMATION:
#   Review automation uses: .sessions/whatsapp_session/
#   OMS uses:               .sessions/oms_session/
#   Two completely independent Chromium profiles.
#   Two separate WhatsApp Web logins.
#   They never share cookies or session data.
# ================================================================
# ==============================================================

"""
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.oms.shared.logger import get_logger
from app.oms.shared.exceptions import ConfigurationException

log = get_logger(__name__)


@dataclass
class ProfileInfo:
    '''
    Information about a browser profile directory.
    Returned by BrowserProfile.inspect() for diagnostics.
    '''
    path:       Path
    exists:     bool
    size_mb:    float    # Approximate size of profile on disk
    has_cookies: bool    # Whether the profile appears to have a saved session


class BrowserProfile:
    '''
    Manages the persistent Chromium browser profile for the OMS.

    A Chromium persistent profile is a directory on disk that stores
    browser state: cookies, localStorage, preferences, cache.
    When Playwright launches with user_data_dir pointing to this
    directory, it restores the previous browser state — including
    any WhatsApp Web login session.

    This is how the OMS stays logged into WhatsApp Web between
    restarts without needing to scan the QR code every time.

    The OMS profile is COMPLETELY SEPARATE from:
      - Your personal Chrome browser profile
      - The review automation's WhatsApp session
      - Any other browser profile on the system

    Usage:
        profile = BrowserProfile(session_dir=".sessions/oms_session")
        profile.ensure_exists()
        path = profile.path   # Pass to Playwright's user_data_dir
    '''

    # Minimum profile size in bytes that suggests a saved session exists
    # A fresh empty profile is ~50KB. A profile with WhatsApp session is ~5MB+
    SESSION_SIZE_THRESHOLD_BYTES = 1_000_000  # 1 MB

    def __init__(self, session_dir: str):
        '''
        Args:
            session_dir: Path to the profile directory.
                         Created automatically if it does not exist.
                         Example: ".sessions/oms_session"
        '''
        if not session_dir:
            raise ConfigurationException(
                "Browser session_dir cannot be empty. "
                "Set OMS_BROWSER_SESSION_DIR or configure in settings.py"
            )

        self._path = Path(session_dir).resolve()
        log.debug(f"BrowserProfile initialised: {self._path}")

    @property
    def path(self) -> Path:
        '''Absolute path to the profile directory.'''
        return self._path

    @property
    def path_str(self) -> str:
        '''String version of the profile path for Playwright.'''
        return str(self._path)

    def ensure_exists(self) -> None:
        '''
        Create the profile directory if it does not exist.
        Safe to call multiple times — uses exist_ok=True.
        '''
        self._path.mkdir(parents=True, exist_ok=True)
        log.debug(f"Profile directory ready: {self._path}")

    def exists(self) -> bool:
        '''True if the profile directory exists on disk.'''
        return self._path.exists()

    def appears_to_have_session(self) -> bool:
        '''
        Heuristic check: does this profile likely have a saved WhatsApp session?

        A fresh profile has very little data. A profile with a WhatsApp
        login session will be at least 1MB due to stored cookies and
        localStorage. This is a best-effort check — the only reliable
        way to confirm login is to load WhatsApp Web and check the UI.

        Returns:
            True if the profile appears to contain saved session data.
            False if the profile is empty or very small (likely fresh).
        '''
        if not self._path.exists():
            return False

        try:
            total_size = sum(
                f.stat().st_size
                for f in self._path.rglob("*")
                if f.is_file()
            )
            return total_size > self.SESSION_SIZE_THRESHOLD_BYTES
        except Exception as e:
            log.debug(f"Could not calculate profile size: {e}")
            return False

    def inspect(self) -> ProfileInfo:
        '''
        Return diagnostic information about the profile.
        Used for health checks and startup logging.
        '''
        exists = self._path.exists()
        size_bytes = 0
        has_cookies = False

        if exists:
            try:
                size_bytes = sum(
                    f.stat().st_size
                    for f in self._path.rglob("*")
                    if f.is_file()
                )
                # Check for Chromium's Cookies file as a session indicator
                cookies_path = self._path / "Default" / "Cookies"
                has_cookies  = cookies_path.exists() and cookies_path.stat().st_size > 1024
            except Exception as e:
                log.debug(f"Profile inspection error: {e}")

        return ProfileInfo(
            path        =self._path,
            exists      =exists,
            size_mb     =round(size_bytes / (1024 * 1024), 2),
            has_cookies =has_cookies,
        )

    def clear(self) -> None:
        '''
        Delete the profile directory completely.
        Used when a session is corrupt and needs a fresh start.

        WARNING: This logs out of WhatsApp Web permanently.
        The next launch will require scanning the QR code again.
        '''
        if self._path.exists():
            shutil.rmtree(self._path)
            log.warning(
                f"Profile cleared: {self._path}\n"
                f"WhatsApp Web will require QR scan on next launch."
            )
        else:
            log.debug("Profile does not exist — nothing to clear.")

    def __repr__(self):
        info = self.inspect()
        return (
            f"BrowserProfile("
            f"path={self._path}, "
            f"exists={info.exists}, "
            f"size={info.size_mb}MB, "
            f"has_session={info.has_cookies}"
            f")"
        )
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/infrastructure/browser/session_manager.py
# ================================================================
# PURPOSE:
#   The core browser lifecycle manager for the OMS.
#   Implements ISessionManager from Day 1 domain interfaces.
#
#   Responsible for:
#     - Launching Playwright with the OMS persistent profile
#     - Loading WhatsApp Web (once — never again)
#     - Detecting whether a QR scan is needed
#     - Guiding the user through QR scan if needed
#     - Confirming login by checking the WhatsApp chat list
#     - Keeping the browser alive passively
#
#   NOT responsible for:
#     - Reading messages
#     - Clicking anything
#     - Navigating within WhatsApp
#     - Any business logic
#
# THE "PASSIVE STAFF MEMBER" PRINCIPLE:
#   Once logged in, the browser does nothing.
#   It is open. It is logged in. WhatsApp Web is visible.
#   The DOM observer (Day 3) will watch for incoming messages.
#   The session manager just keeps the lights on.
# ================================================================
# ==============================================================

"""
import asyncio
from enum import Enum
from typing import Optional

from app.oms.domain.interfaces import ISessionManager
from app.oms.infrastructure.browser.profile import BrowserProfile
from app.oms.shared.exceptions import InfrastructureException
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class SessionState(str, Enum):
    '''
    The current state of the browser session.

    STOPPED      → Browser is not running.
    LAUNCHING    → Browser is starting up.
    LOADING      → WhatsApp Web is loading (navigating to web.whatsapp.com).
    AWAITING_QR  → QR code is visible, waiting for user to scan.
    LOGGED_IN    → WhatsApp is loaded and user is authenticated.
    EXPIRED      → Session was active but WhatsApp logged out.
    ERROR        → Unrecoverable error. Restart required.
    '''
    STOPPED     = "STOPPED"
    LAUNCHING   = "LAUNCHING"
    LOADING     = "LOADING"
    AWAITING_QR = "AWAITING_QR"
    LOGGED_IN   = "LOGGED_IN"
    EXPIRED     = "EXPIRED"
    ERROR       = "ERROR"


class SessionManager(ISessionManager):
    '''
    Manages the Playwright browser lifecycle for the OMS.

    One browser. One context. One page. One WhatsApp session.
    Opened once. Never refreshed. Never navigated away.

    The browser is the OMS's passive observer — it opens WhatsApp
    Web and stays there, exactly like a human who opens their
    laptop in the morning and leaves WhatsApp Web running all day.

    Usage:
        manager = SessionManager(profile=profile, cfg=cfg)
        await manager.start()           # Opens browser, logs in
        is_ok = await manager.is_logged_in()  # Health check
        page  = manager.page            # Access the live page (Day 3 uses this)
        await manager.stop()            # Close browser cleanly
    '''

    # WhatsApp Web UI selectors used ONLY for login detection.
    # No other selectors are used in Day 2.
    # Day 3 (DOM Observer) will add message-reading selectors.
    _SEL_CHAT_LIST = 'div[aria-label="Chat list"]'
    _SEL_QR_CODE   = 'canvas[aria-label="Scan me!"], div[data-ref]'
    _SEL_SEARCH    = 'input[aria-label="Search or start a new chat"]'

    # WhatsApp Web URL — navigated to ONCE on startup, never again
    WHATSAPP_URL = "https://web.whatsapp.com"

    # How long to wait for WhatsApp Web to load on startup (ms)
    LOAD_TIMEOUT_MS = 60_000      # 60 seconds

    # How long to wait for QR scan (ms)
    QR_TIMEOUT_MS = 120_000       # 2 minutes

    def __init__(self, profile: BrowserProfile, cfg):
        '''
        Args:
            profile: BrowserProfile managing the persistent session directory.
            cfg:     OMSSettings — provides browser configuration.
        '''
        self._profile   = profile
        self._cfg       = cfg
        self._state     = SessionState.STOPPED

        # Playwright objects — all None until start() is called
        self._playwright = None
        self._context    = None
        self._page       = None

    @property
    def state(self) -> SessionState:
        '''Current session state.'''
        return self._state

    @property
    def page(self):
        '''
        The active Playwright page object.
        Returns None if browser is not started.
        Day 3 (DOM Observer) receives this to watch for messages.
        Day 4+ can use this for other browser interactions.

        IMPORTANT: Never call goto() or navigate on this page
        after start() completes. The browser must stay on
        WhatsApp Web without reloading.
        '''
        return self._page

    @property
    def is_running(self) -> bool:
        '''True if the browser process is alive.'''
        return self._state not in (
            SessionState.STOPPED,
            SessionState.ERROR
        )

    # ── ISessionManager implementation ─────────────────────────

    async def start(self) -> None:
        '''
        Launch the browser, load WhatsApp Web, handle login.

        Flow:
          1. Launch Playwright with persistent profile
          2. Navigate to WhatsApp Web (THE ONLY goto() call)
          3. Wait for either: chat list (logged in) or QR code
          4. If QR: guide user through scan, wait for login
          5. Confirm login by checking chat list is visible
          6. Set state to LOGGED_IN — browser stays here

        Raises:
            InfrastructureException if browser cannot start
            or WhatsApp login fails within the timeout period.
        '''
        if self._state == SessionState.LOGGED_IN:
            log.info("Session already active — skipping start.")
            return

        self._state = SessionState.LAUNCHING
        log.info("Starting OMS browser session...")

        # Inspect profile before launch
        info = self._profile.inspect()
        log.info(
            f"Profile: {self._profile.path}\n"
            f"  exists={info.exists}, "
            f"  size={info.size_mb}MB, "
            f"  cookies={info.has_cookies}"
        )

        if info.has_cookies:
            log.info("Saved session detected — will attempt silent restore.")
        else:
            log.info("No saved session — QR scan will be required.")

        try:
            await self._launch_browser()
            await self._load_whatsapp()
            await self._handle_login()

        except Exception as e:
            self._state = SessionState.ERROR
            raise InfrastructureException(
                f"Browser session failed to start: {e}",
                context={"profile": str(self._profile.path)}
            ) from e

    async def stop(self) -> None:
        '''
        Close the browser cleanly.
        The session profile is preserved — next launch restores it.
        Never call this inside the send/monitor loop.
        Only call on application shutdown.
        '''
        log.info("Stopping OMS browser session...")

        try:
            if self._context:
                await self._context.close()
                self._context = None

            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

            self._page  = None
            self._state = SessionState.STOPPED
            log.info("Browser session stopped cleanly.")

        except Exception as e:
            log.error(f"Error during browser stop: {e}", exc_info=True)
            self._state = SessionState.STOPPED

    async def is_logged_in(self) -> bool:
        '''
        True if WhatsApp Web is loaded and user is authenticated.
        Checks for the presence of the chat list element.

        Called periodically by BrowserHealthCheck to detect
        session expiry (WhatsApp logged out, session expired, etc.)
        '''
        try:
            if not self._page or self._state != SessionState.LOGGED_IN:
                return False

            el = await self._page.query_selector(self._SEL_CHAT_LIST)
            return el is not None

        except Exception:
            return False

    # ── Private browser lifecycle methods ──────────────────────

    async def _launch_browser(self) -> None:
        '''
        Launch Playwright Chromium with the persistent profile.
        Sets self._playwright, self._context, self._page.
        '''
        from playwright.async_api import async_playwright

        self._profile.ensure_exists()

        log.info("Launching Chromium browser...")
        self._playwright = await async_playwright().start()

        browser_cfg = self._cfg.browser

        # launch_persistent_context creates a browser with a saved profile.
        # This is different from launch() + new_context() because the
        # persistent profile preserves cookies and localStorage across runs.
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._profile.path_str,
            headless=browser_cfg.headless,
            viewport={
                "width":  browser_cfg.viewport_w,
                "height": browser_cfg.viewport_h,
            },
            locale=browser_cfg.locale,
            timezone_id=browser_cfg.timezone,
            args=[
                "--no-sandbox",
                # Suppress the automation banner visible to users
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                # Suppress notification permission prompts
                "--disable-notifications",
            ],
        )

        # Use the first existing page or open one
        # Never open more than one page — one session, one page
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

        log.info("Browser launched successfully.")

    async def _load_whatsapp(self) -> None:
        '''
        Navigate to WhatsApp Web. This is called ONCE — on startup.
        After this method returns, the page never navigates again.
        '''
        self._state = SessionState.LOADING
        log.info(f"Loading WhatsApp Web: {self.WHATSAPP_URL}")

        # THE ONLY GOTO CALL IN THE ENTIRE OMS BROWSER INFRASTRUCTURE
        await self._page.goto(
            self.WHATSAPP_URL,
            wait_until="domcontentloaded",
            timeout=self.LOAD_TIMEOUT_MS
        )

        log.info("WhatsApp Web loaded. Checking login state...")

    async def _handle_login(self) -> None:
        '''
        Determine whether login is needed and handle it.

        Waits for either:
          A) Chat list appears → already logged in → continue
          B) QR code appears  → need to scan → guide user

        After QR scan confirmed, verifies chat list is visible.
        '''
        try:
            # Wait for either the chat list or QR code to appear
            await self._page.wait_for_selector(
                f"{self._SEL_CHAT_LIST}, {self._SEL_QR_CODE}",
                timeout=self.LOAD_TIMEOUT_MS
            )
        except Exception as e:
            raise InfrastructureException(
                "WhatsApp Web did not load within the expected time.\n"
                "Check your internet connection.",
                context={"timeout_ms": self.LOAD_TIMEOUT_MS}
            ) from e

        # Check which element appeared
        chat_list = await self._page.query_selector(self._SEL_CHAT_LIST)

        if chat_list:
            # Already logged in — session restored from profile
            self._state = SessionState.LOGGED_IN
            log.info(
                "✅ WhatsApp session restored. OMS is ready.\n"
                "   Browser will remain open passively."
            )
            return

        # QR code appeared — user needs to scan
        await self._handle_qr_scan()

    async def _handle_qr_scan(self) -> None:
        '''
        Display QR scan instructions and wait for the user to scan.
        The browser window must be visible (headless=False) for this.
        '''
        self._state = SessionState.AWAITING_QR

        print("\n" + "=" * 60)
        print("  WHATSAPP OMS — LOGIN REQUIRED")
        print("=" * 60)
        print("  The OMS browser needs to log in to WhatsApp Web.")
        print()
        print("  Steps:")
        print("  1. Open WhatsApp on your phone")
        print("  2. Tap Menu (⋮) → Linked Devices → Link a Device")
        print("  3. Scan the QR code in the browser window")
        print()
        print("  The session will be saved automatically.")
        print("  You will not need to scan again on future starts.")
        print("=" * 60 + "\n")

        log.info(
            "Waiting for QR code scan...\n"
            f"  Timeout: {self.QR_TIMEOUT_MS // 1000} seconds"
        )

        try:
            # Wait for chat list to appear after QR scan
            await self._page.wait_for_selector(
                self._SEL_CHAT_LIST,
                timeout=self.QR_TIMEOUT_MS
            )
        except Exception:
            raise InfrastructureException(
                f"QR code was not scanned within "
                f"{self.QR_TIMEOUT_MS // 1000} seconds.\n"
                "Please restart the OMS and scan the QR code promptly."
            )

        self._state = SessionState.LOGGED_IN
        log.info(
            "✅ QR code scanned successfully.\n"
            "   Session saved to profile. No scan needed next time."
        )

    async def reconnect(self) -> None:
        '''
        Attempt to recover from an expired session.
        Stops the current browser and starts fresh.

        Called by BrowserHealthCheck when session expiry is detected.
        '''
        log.warning("Session expired. Attempting reconnect...")
        self._state = SessionState.EXPIRED

        await self.stop()
        await asyncio.sleep(3)  # Brief pause before restart
        await self.start()

    def __repr__(self):
        return (
            f"SessionManager("
            f"state={self._state.value}, "
            f"profile={self._profile.path.name!r}"
            f")"
        )
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/infrastructure/browser/health_check.py
# ================================================================
# PURPOSE:
#   Periodically checks whether the WhatsApp Web session is still
#   alive. Detects session expiry and triggers reconnection.
#
# WHY HEALTH CHECKS:
#   WhatsApp Web sessions can expire for several reasons:
#     - User logged out from phone
#     - WhatsApp server disconnected the linked device
#     - Network interruption caused session loss
#     - Computer was sleeping and session timed out
#
#   Without a health check, the OMS would silently stop working.
#   With it, the OMS detects the problem and reconnects.
#
# DESIGN:
#   Runs as an asyncio background task.
#   Does not interact with WhatsApp — only reads UI state.
#   If session is lost: calls session_manager.reconnect()
#   Uses configurable check interval (default: 60 seconds).
# ================================================================
# ==============================================================

"""
import asyncio
from typing import Optional

from app.oms.infrastructure.browser.session_manager import (
    SessionManager,
    SessionState,
)
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger

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
        '''
        log.warning(
            "Session confirmed unhealthy. Triggering reconnect..."
        )

        await dispatcher.emit(
            "browser.disconnected",
            reason="Health check detected session loss",
            fail_count=self._fail_count,
        )

        try:
            await self._manager.reconnect()
            self._fail_count = 0

            await dispatcher.emit(
                "browser.connected",
                source="health_check_reconnect"
            )

            log.info("Reconnect successful. Session restored.")

        except Exception as e:
            log.error(
                f"Reconnect failed: {e}\n"
                f"OMS browser is not operational. Manual restart required.",
                exc_info=True
            )

            await dispatcher.emit(
                "browser.error",
                error=str(e),
                source="health_check_reconnect"
            )

    def stats(self) -> dict:
        '''Return health check statistics for diagnostics.'''
        return {
            "checks_performed":    self._check_count,
            "consecutive_failures": self._fail_count,
            "check_interval_s":    self._interval,
            "is_running":          self._running,
        }
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/infrastructure/browser/bootstrap.py
# ================================================================
# PURPOSE:
#   The entry point that wires browser infrastructure together.
#   Creates profile, session manager, and health check.
#   Provides a clean public API for starting/stopping the OMS browser.
#
# WHY A BOOTSTRAP:
#   Application code should not know how to assemble infrastructure.
#   Bootstrap does the wiring — application just calls start()/stop().
#   When Day 3 adds message reading, it receives the bootstrap's
#   session_manager.page and starts watching it — no other changes.
# ================================================================
# ==============================================================

"""
import asyncio
import signal
from typing import Optional

from app.oms.config.settings import OMSSettings
from app.oms.infrastructure.browser.profile import BrowserProfile
from app.oms.infrastructure.browser.session_manager import (
    SessionManager,
    SessionState,
)
from app.oms.infrastructure.browser.health_check import BrowserHealthCheck
from app.oms.events import dispatcher
from app.oms.shared.logger import get_logger
from app.oms.shared.exceptions import InfrastructureException

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
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/infrastructure/__init__.py
# ================================================================
# Update the existing empty infrastructure __init__.py
# to expose the browser infrastructure.
# ================================================================
# ==============================================================

"""
from app.oms.infrastructure.browser import (
    BrowserProfile,
    SessionManager,
    SessionState,
    BrowserHealthCheck,
    BrowserBootstrap,
)

__all__ = [
    "BrowserProfile",
    "SessionManager",
    "SessionState",
    "BrowserHealthCheck",
    "BrowserBootstrap",
]
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/oms_runner.py
# ================================================================
# Day 2 test runner.
# Saved in windwhirl/ root alongside main.py (review automation).
#
# PURPOSE:
#   Launches the OMS browser to verify Day 2 works end-to-end.
#   In Day 3 this becomes the main application entry point.
#
# RUN WITH:
#   cd windwhirl
#   python oms_runner.py
#
# EXPECTED BEHAVIOUR:
#   Browser opens (or loads from saved session)
#   WhatsApp Web appears
#   Console shows "OMS BROWSER READY"
#   Browser stays open passively
#   Ctrl+C shuts it down cleanly
# ================================================================
# ==============================================================

"""
import asyncio
import sys
from pathlib import Path

# Path fix — same pattern as the review automation's main.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.oms.config.settings import get_settings
from app.oms.infrastructure.browser.bootstrap import BrowserBootstrap
from app.oms.shared.logger import get_logger
from app.oms.events import dispatcher

log = get_logger("oms.runner")


# ── Register basic event listeners for Day 2 diagnostics ───────
# These just log events to console so you can see what's happening.
# Day 3 will replace these with real message-handling listeners.

@dispatcher.on("browser.connected")
async def on_connected(**kwargs):
    log.info(f"EVENT browser.connected — {kwargs}")


@dispatcher.on("browser.disconnected")
async def on_disconnected(**kwargs):
    log.info(f"EVENT browser.disconnected — {kwargs}")


@dispatcher.on("browser.error")
async def on_error(**kwargs):
    log.error(f"EVENT browser.error — {kwargs}")


async def main():
    '''
    Day 2 entry point.
    Starts the browser and waits passively.
    Day 3 will add the message observer inside the run_forever loop.
    '''
    log.info("Windwhirl OMS starting...")

    # Load settings
    # For Day 2 testing, group_name and staff_number can be empty
    # The browser will still open — config validation is a warning only
    settings = get_settings()

    # Quick override for testing without setting env vars:
    # settings.whatsapp.group_name  = "Your Group Name Here"
    # settings.whatsapp.staff_number = "2348XXXXXXXXX"
    # settings.browser.headless      = False

    bootstrap = BrowserBootstrap(settings)

    try:
        # Start browser, load WhatsApp Web, begin health monitoring
        await bootstrap.start()

        # Wait here — browser stays open, health check runs in background
        # Day 3 adds: DOM observer runs here watching for messages
        await bootstrap.run_forever()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down.")
    except Exception as e:
        log.error(f"OMS runner error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await bootstrap.stop()

    log.info("Windwhirl OMS stopped.")


if __name__ == "__main__":
    asyncio.run(main())
"""


# ==============================================================
# DAY 2 VERIFICATION
# ==============================================================
#
# From windwhirl/ directory:
#
# Test 1 — imports resolve correctly:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.infrastructure.browser import BrowserBootstrap, SessionManager
#   from app.oms.infrastructure.browser.profile import BrowserProfile
#   from app.oms.infrastructure.browser.health_check import BrowserHealthCheck
#   print('All Day 2 imports OK')
#   "
#
# Test 2 — profile class works:
#   python -c "
#   import sys; sys.path.insert(0, '.')
#   from app.oms.infrastructure.browser.profile import BrowserProfile
#   p = BrowserProfile('.sessions/oms_session')
#   print('Profile:', p)
#   p.ensure_exists()
#   print('Created:', p.path.exists())
#   print('Info:', p.inspect())
#   "
#
# Test 3 — full browser launch (interactive — requires WhatsApp):
#   python oms_runner.py
#
#   Expected console output:
#     [HH:MM:SS] INFO     Windwhirl OMS starting...
#     [HH:MM:SS] INFO     Starting OMS browser session...
#     [HH:MM:SS] INFO     Profile: .sessions/oms_session
#     [HH:MM:SS] INFO     Launching Chromium browser...
#     [HH:MM:SS] INFO     Loading WhatsApp Web: https://web.whatsapp.com
#     (if first run):
#       SCAN QR CODE TO LOG IN
#       (scan with phone)
#     (if session exists):
#       [HH:MM:SS] INFO   ✅ WhatsApp session restored. OMS is ready.
#     [HH:MM:SS] INFO     OMS BROWSER READY
#     [HH:MM:SS] INFO     OMS browser running. Press Ctrl+C to stop.
#     (every 60 seconds):
#     [HH:MM:SS] DEBUG    Health check #1 — state=LOGGED_IN
#     [HH:MM:SS] DEBUG      Session healthy ✓
#
#   Press Ctrl+C to stop cleanly.
#
# ==============================================================
# WHAT DAY 3 BUILDS
# ==============================================================
# Day 3: Message Source + DOM Observer
#   - IDOMObserver implementation using Playwright
#   - Reads messages from the WhatsApp group without navigating
#   - Detects new messages by reading the chat DOM
#   - Returns RawMessage objects to the application layer
#   - No parsing, no order detection, no storage
#
# The DOM observer receives bootstrap.page from Day 2 and
# attaches to it. Day 2 does not change at all.
# Clean separation maintained.
# ==============================================================