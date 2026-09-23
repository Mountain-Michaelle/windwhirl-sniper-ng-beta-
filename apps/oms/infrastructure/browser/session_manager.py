import asyncio
from enum import Enum
from typing import Optional

from playwright_stealth import Stealth  # <-- NEW
from apps.oms.domain.interfaces import ISessionManager
from apps.oms.infrastructure.browser.profile import BrowserProfile
from apps.oms.shared.exceptions import InfrastructureException
from apps.oms.shared.logger import get_logger
from apps.oms.mixins import GroupNavigationMixin    

stealth = Stealth()
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


class SessionManager(ISessionManager, GroupNavigationMixin):
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

        Includes stealth measures to avoid WhatsApp detection:
          - Realistic user‑agent (Chrome 120, Windows 10)
          - Geolocation set to Lagos, Nigeria
          - Languages, timezone already set from config
          - `--disable-blink-features=AutomationControlled`
          - `stealth_async(page)` patch after page creation
          - Init script to hide `navigator.webdriver`
        '''
        from playwright.async_api import async_playwright

        self._profile.ensure_exists()

        log.info("Launching Chromium browser...")
        self._playwright = await async_playwright().start()

        browser_cfg = self._cfg.browser

        # --- Build a realistic context for WhatsApp ---
        # Use a modern Chrome user‑agent (Windows)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        # Geolocation for Lagos, Nigeria (to match timezone Africa/Lagos)
        geolocation = {"latitude": 6.5244, "longitude": 3.3792}

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
            locale=browser_cfg.locale,          # 'en-US'
            timezone_id=browser_cfg.timezone,   # 'Africa/Lagos'
            user_agent=user_agent,              # <-- NEW
            permissions=["geolocation"],        # <-- NEW
            geolocation=geolocation,            # <-- NEW
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            color_scheme="light",
            args=[
                "--no-sandbox",
                # Disable the "Chrome is being controlled by automated test software" banner
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                # Suppress notification permission prompts
                "--disable-notifications",
                # Additional anti‑detection flags
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
            ],
        )

        # Use the first existing page or open one
        # Never open more than one page — one session, one page
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

        # ========================
        # STEALTH: apply patches
        # ========================
        # 1. playwright-stealth – patches many fingerprint leaks
        await stealth.apply_stealth_async(self._page)
        log.debug("Stealth patches applied via playwright-stealth")

        # 2. Extra init script to hide webdriver (belt and braces)
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        log.debug("navigator.webdriver removed via init script")

        log.info("Browser launched successfully with stealth measures.")

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