# ==============================================================
# WHATSAPP REVIEW AUTOMATION — DAY 3 BUILD
# ==============================================================
# FILES IN THIS DOCUMENT:
#   FILE 11 → src/playwright_sender.py
#   FILE 12 → src/scheduler.py
#   FILE 13 → src/reporter.py
#   FILE 14 → main.py
#
# PREREQUISITE:
#   All Day 1 and Day 2 tests must be passing before building these.
#   Specifically: customers.xlsx must be in data/ and FILE 6 test
#   must show "Customers found: 96" before continuing.
#
# WHAT YOU ARE BUILDING TODAY:
#   FILE 11 — The actual WhatsApp Web automation. Controls a real
#             Chrome browser. All 8 stealth layers live here.
#             This is the most complex file in the whole system.
#
#   FILE 12 — The orchestration layer. Runs the full sending day.
#             Fetches customers, calls the sender, applies human-like
#             delays, updates the DB, triggers end-of-day tasks.
#
#   FILE 13 — Generates the daily summary report (sent/failed/invalid
#             counts, template A vs B breakdown) and emails it to you.
#
#   FILE 14 — The CLI entry point. The only file you ever run directly.
#             Wires everything together into clean commands:
#             --setup, --preview, --dry-run, --run, --report, --reset-failed
#
# AFTER TODAY YOU CAN RUN THE FULL SYSTEM:
#   python main.py --setup               → import Excel, scan QR
#   python main.py --preview             → see customer count + schedule
#   python main.py --dry-run             → read exact messages before sending
#   python main.py --run --now --count 3 → send 3 test messages immediately
#   python main.py --run                 → full scheduled day (50 messages)
#
# FUTURE-AWARE DESIGN DECISIONS IN TODAY'S FILES:
#
#   PlaywrightSender (FILE 11):
#     Implements WhatsAppSender interface from Day 2 FILE 10.
#     When you build FastAPI, the sender runs as an app-level singleton:
#       app.state.sender = PlaywrightSender(cfg)
#       await app.state.sender.connect()  ← in FastAPI startup event
#     Then routes call: await request.app.state.sender.send_text(...)
#     No changes to PlaywrightSender needed.
#
#   Scheduler (FILE 12):
#     Pure async service class — no CLI dependency.
#     FastAPI can call scheduler.run_now(count) from a POST endpoint:
#       @app.post("/send-batch")
#       async def send_batch(count: int):
#           await scheduler.run_now(count)
#     The scheduled 6-session day runs as a background task in FastAPI.
#
#   Reporter (FILE 13):
#     Pure service — no framework dependency.
#     FastAPI can expose: GET /reports/today → returns the report dict.
#     Next.js dashboard fetches this and renders it as a UI.
#
#   main.py (FILE 14):
#     CLI only. When FastAPI is added, a separate api.py is created.
#     main.py stays as the local runner. Both share the same service classes.
#     Nothing in main.py needs to change when FastAPI is added.
#
# FOLDER STRUCTURE AFTER TODAY (complete system):
#
#   whatsapp_automation/
#   ├── main.py                   ← FILE 14 — run this
#   ├── requirements.txt          ← Day 1
#   ├── .gitignore                ← Day 1
#   ├── data/
#   │   ├── customers.xlsx        ← your Excel file
#   │   └── automation.db         ← auto-created SQLite database
#   ├── templates/
#   │   ├── message_a.j2          ← Day 2
#   │   └── message_b.j2          ← Day 2
#   ├── .sessions/                ← auto-created, WhatsApp login saved here
#   ├── logs/                     ← auto-created, rotating log files
#   ├── reports/                  ← auto-created, daily text reports
#   ├── screenshots/              ← auto-created, proof per sent message
#   └── src/
#       ├── __init__.py           ← Day 1
#       ├── config.py             ← Day 1
#       ├── database.py           ← Day 1
#       ├── data_reader.py        ← Day 2
#       ├── message_builder.py    ← Day 2
#       ├── whatsapp_sender.py    ← Day 2
#       ├── playwright_sender.py  ← FILE 11 (today)
#       ├── scheduler.py          ← FILE 12 (today)
#       └── reporter.py           ← FILE 13 (today)
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 11
#  PATH:  whatsapp_automation/src/playwright_sender.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   Controls a real Chromium browser to send WhatsApp messages
#   via WhatsApp Web. Implements the WhatsAppSender interface
#   from Day 2 FILE 10 with all 8 stealth layers.
#
# THE 8 STEALTH LAYERS — WHY EACH EXISTS:
#
#   STEALTH 1: page.type() with random delay per character
#     WhatsApp Web is a JavaScript app that listens for keyboard events.
#     page.fill() inserts text silently with NO keyboard events.
#     That silence is immediately detectable.
#     page.type() fires a real keydown/keypress/keyup event per character
#     with a random delay — identical to a human typing.
#
#   STEALTH 2: Pre-typing pause (3–8 seconds)
#     After the chat loads, we wait before touching the input box.
#     This simulates: user arrives at the chat, reads it, then types.
#     A bot that starts typing 0.1s after page load looks like a bot.
#
#   STEALTH 3: Human mouse movement
#     Before clicking the input box, we move the mouse to a random
#     screen position first, pause briefly, then move to the target.
#     Humans never teleport the cursor directly to a UI element.
#
#   STEALTH 4: No ?text= URL parameter
#     WhatsApp Web accepts: ?phone=234XXX&text=Hello+World
#     But humans never use that — they navigate to the chat and type.
#     We use: ?phone=234XXX only, then type manually.
#
#   STEALTH 5: headless=False, real viewport
#     WhatsApp Web behaves differently in headless mode — it detects
#     it and may show a "Use WhatsApp on your phone" prompt instead.
#     We always run with a visible 1280×800 browser window.
#
#   STEALTH 6: Emoji via JavaScript injection
#     page.type() mangles multi-byte emoji characters on some systems.
#     We split the message into text segments and emoji segments.
#     Text → typed with page.type(). Emoji → injected via JS eval.
#
#   STEALTH 7: Occasional random scroll
#     Before typing, we sometimes scroll the chat up then back down.
#     This simulates the human habit of glancing at previous messages
#     before replying.
#
#   STEALTH 8: Tab rotation every 8–12 messages
#     After N messages, navigate back to WhatsApp Web home and pause.
#     This resets any cumulative session behaviour patterns that
#     WhatsApp's browser-side analytics might be tracking.
#
# SESSION PERSISTENCE:
#   Browser session saved to .sessions/whatsapp_session/ on first QR scan.
#   Playwright's persistent context stores cookies, localStorage, session data.
#   All future runs load this silently — no QR scan needed.
#
# FUTURE / FASTAPI NOTE:
#   When adding FastAPI, create the sender once at app startup:
#     @app.on_event("startup")
#     async def startup():
#         app.state.sender = PlaywrightSender(cfg)
#         await app.state.sender.connect()
#
#   Then inject it into routes via dependency:
#     async def get_sender(request: Request) -> WhatsAppSender:
#         return request.app.state.sender
#
#     @app.post("/send")
#     async def send_one(phone: str, message: str,
#                        sender: WhatsAppSender = Depends(get_sender)):
#         result = await sender.send_text(phone, message, order_id="manual")
#         return result
#
#   Shutdown cleanly:
#     @app.on_event("shutdown")
#     async def shutdown():
#         await app.state.sender.disconnect()
#
# TEST AFTER SAVING (requires WhatsApp session from --setup):
#   python main.py --setup        ← run this first (scans QR)
#   python main.py --run --now --count 1  ← sends one message
# ================================================================
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/playwright_sender.py

import asyncio
import logging
import random
import re
from datetime import datetime
from pathlib import Path

from src.whatsapp_sender import WhatsAppSender, SendResult

logger = logging.getLogger(__name__)


class PlaywrightSender(WhatsAppSender):
    """
    WhatsApp Web automation via Playwright async API.
    Implements all 8 stealth behaviours defined in the class docstring above.

    Extends WhatsAppSender (abstract interface from src/whatsapp_sender.py).
    The Scheduler and CLI only talk to WhatsAppSender — they never
    import PlaywrightSender directly. This keeps the swap path clean.

    First run:  browser opens → QR code shown → scan with phone → session saved
    Later runs: session loads from .sessions/ → no QR needed
    """

    # ── WhatsApp Web CSS selectors ─────────────────────────────
    # These target specific UI elements in WhatsApp Web's HTML.
    # If WhatsApp updates their UI and selectors break, update here only.
    SEL = {
        # The chat list on the left — confirms we're logged in
        "chat_list":     'div[aria-label="Chat list"]',

        # QR code shown on first login
        "qr_code":       'canvas[aria-label="Scan me!"], div[data-ref]',

        # The text input box at the bottom of a chat
        "msg_input":     'div[aria-label="Type a message"]',

        # The tick icon that appears after a message is delivered
        # data-icon="msg-check"    = single grey tick (sent to server)
        # data-icon="msg-dblcheck" = double grey tick (delivered to phone)
        # Either confirms success — we don't need to wait for blue ticks
        "sent_tick":     'span[data-icon="msg-check"], '
                         'span[data-icon="msg-dblcheck"]',

        # Paperclip / attachment button
        "attach_btn":    'span[data-icon="attach-menu-plus"]',

        # Caption input when sending an image
        "caption_input": 'div[aria-label="Add a caption"]',
    }

    # ── Text that indicates a number is not on WhatsApp ────────
    # WhatsApp Web shows one of these strings in a modal when the
    # phone number navigated to is not a registered WhatsApp account.
    INVALID_TEXTS = [
        "phone number shared via url is invalid",
        "not on whatsapp",
        "invalid phone number",
    ]

    def __init__(self, cfg):
        """
        Args:
            cfg: AppConfig instance — provides delay settings and paths.
        """
        self._cfg       = cfg
        self._log       = logging.getLogger(self.__class__.__name__)

        # Paths
        self._sess_path = Path(".sessions") / "whatsapp_session"
        self._ss_path   = Path("screenshots")

        # Playwright objects — initialized in connect()
        self._pw      = None   # Playwright instance
        self._ctx     = None   # Browser context (persistent, holds session)
        self._page    = None   # Active browser tab

        # STEALTH 8: Tab rotation tracking
        # After _rotate_after messages, navigate home to reset session patterns
        self._msgs_on_tab  = 0
        self._rotate_after = random.randint(8, 12)

    # ── CONNECTION ─────────────────────────────────────────────

    async def connect(self) -> bool:
        """
        Launch browser with persistent session.

        On first run: QR code appears, user scans with phone.
                      Session is saved to .sessions/ automatically.
        All later runs: session loads silently, no QR needed.

        STEALTH 5: headless=False — WhatsApp Web detects headless.
                   Viewport 1280×800 — matches a real laptop screen.

        Returns:
            True if connected and chat list is visible.

        Raises:
            ConnectionError if connection could not be established.
        """
        # Playwright is imported here (deferred import) so that
        # modules that don't need the browser (--preview, --dry-run)
        # don't need Playwright installed just to import this file.
        from playwright.async_api import async_playwright

        self._log.info("Launching browser...")
        self._sess_path.mkdir(parents=True, exist_ok=True)
        self._ss_path.mkdir(exist_ok=True)

        self._pw = await async_playwright().start()

        # launch_persistent_context = browser + profile in one call.
        # user_data_dir stores cookies, localStorage, session tokens.
        # This is what makes the WhatsApp login persist between runs.
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._sess_path),
            headless=False,                            # STEALTH 5
            viewport={"width": 1280, "height": 800},   # STEALTH 5
            locale="en-US",
            timezone_id="Africa/Lagos",                # Match Nigeria timezone
            args=[
                "--no-sandbox",
                # Hides the "Chrome is controlled by automated software" banner
                # and removes the navigator.webdriver=true JS property
                # that WhatsApp Web could detect
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        # Use the first existing tab or open a new one
        self._page = (
            self._ctx.pages[0]
            if self._ctx.pages
            else await self._ctx.new_page()
        )

        self._log.info("Navigating to WhatsApp Web...")
        await self._page.goto(
            "https://web.whatsapp.com",
            wait_until="domcontentloaded"
        )

        try:
            # Wait for either the chat list (logged in) or QR code (first run)
            await self._page.wait_for_selector(
                f"{self.SEL['chat_list']}, {self.SEL['qr_code']}",
                timeout=30_000   # 30 seconds
            )

            # Check which element appeared
            chat_list = await self._page.query_selector(self.SEL["chat_list"])

            if chat_list:
                self._log.info("Session loaded — no QR scan needed.")
                return True

            # QR code appeared — guide the user to scan
            print("\n" + "=" * 55)
            print("  SCAN QR CODE TO LOG IN TO WHATSAPP")
            print("=" * 55)
            print("  1. Open WhatsApp on your phone")
            print("  2. Tap Menu (⋮) → Linked Devices → Link a Device")
            print("  3. Point your phone camera at the QR code")
            print("     shown in the browser window that just opened")
            print("  You have 2 minutes. The session will be saved")
            print("  automatically — you won't need to scan again.")
            print("=" * 55 + "\n")

            # Wait up to 2 minutes for the scan to complete
            await self._page.wait_for_selector(
                self.SEL["chat_list"],
                timeout=120_000   # 2 minutes
            )
            self._log.info("QR scanned. Session saved for future runs.")
            return True

        except Exception as e:
            raise ConnectionError(
                f"Could not connect to WhatsApp Web.\n"
                f"Error: {e}\n"
                f"Check your internet connection and run --setup again."
            )

    async def disconnect(self) -> None:
        """
        Close the browser cleanly.
        CRITICAL: Never delete .sessions/ — it holds the saved login.
        Always called in a finally block so it runs even on crash.
        """
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
            self._log.info("Browser closed cleanly.")
        except Exception as e:
            self._log.error(f"Error during disconnect: {e}")

    async def is_connected(self) -> bool:
        """
        Check if the WhatsApp session is still alive.
        Called by Scheduler at the start of each session.
        If False: Scheduler attempts to reconnect before proceeding.
        """
        try:
            if not self._page:
                return False
            el = await self._page.query_selector(self.SEL["chat_list"])
            return el is not None
        except Exception:
            return False

    # ── STEALTH HELPERS ────────────────────────────────────────

    async def _rotate_tab(self):
        """
        STEALTH 8: Navigate home after N messages to reset session patterns.

        WhatsApp's browser-side code tracks navigation patterns.
        Sending message after message to different URLs in a tight
        loop looks different from normal browsing behaviour.
        Navigating home and pausing resets this accumulated pattern.

        N (self._rotate_after) is randomized per cycle so the
        rotation itself doesn't happen at a predictable interval.
        """
        if self._msgs_on_tab >= self._rotate_after:
            self._log.info(
                f"Tab rotation after {self._msgs_on_tab} messages — "
                f"resetting session pattern..."
            )
            await self._page.goto(
                "https://web.whatsapp.com",
                wait_until="domcontentloaded"
            )
            # Pause as if the user is looking at their chat list
            await asyncio.sleep(random.uniform(3, 6))

            # Reset counters — new threshold is random (8–12)
            self._msgs_on_tab  = 0
            self._rotate_after = random.randint(8, 12)
            self._log.debug(
                f"Tab rotated. Next rotation after {self._rotate_after} messages."
            )

    async def _mouse_human_to(self, target_x: int, target_y: int):
        """
        STEALTH 3: Move mouse via a random intermediate position.

        Human mouse movement is never a straight teleport.
        We move to a random point first, pause, then move to target.
        This prevents the cursor jumping directly to an input element
        which is a detectable bot pattern.

        Args:
            target_x: Final x coordinate (center of target element)
            target_y: Final y coordinate (center of target element)
        """
        # Random intermediate point anywhere on the visible page
        mid_x = random.randint(150, 1100)
        mid_y = random.randint(80,  650)

        await self._page.mouse.move(mid_x, mid_y)
        await asyncio.sleep(random.uniform(0.3, 0.8))   # Natural pause
        await self._page.mouse.move(target_x, target_y)
        await asyncio.sleep(random.uniform(0.1, 0.3))   # Settle on target

    async def _type_human(self, selector: str, message: str):
        """
        STEALTH 1 + STEALTH 6: Type message exactly like a human.

        Text segments: typed character-by-character with random ms delay.
        Emoji segments: injected via JavaScript to prevent garbled chars.
        Long messages: occasional mid-message thinking pause.

        WHY page.type() AND NOT page.fill():
            page.fill() sets the input value directly in the DOM.
            It fires NO keyboard events (keydown, keypress, keyup).
            WhatsApp Web uses keyboard events to detect typing.
            Absence of keyboard events = detectable bot pattern.

            page.type() fires real keyboard events per character.
            With a random delay between each character, the event
            timing is statistically identical to a human typing.

        Args:
            selector: CSS selector of the message input element.
            message:  The full message string to type.
        """
        # ── Split message into text and emoji segments ──────────
        # Emoji are multi-byte Unicode characters. page.type() can
        # garble them on some OS/keyboard combinations. We handle
        # them separately via JavaScript injection (STEALTH 6).
        emoji_re = re.compile(
            "["
            "\U0001F600-\U0001F64F"   # Emoticons (😊👋🙏)
            "\U0001F300-\U0001F5FF"   # Symbols & pictographs
            "\U0001F680-\U0001F6FF"   # Transport & map symbols
            "\U0001F1E0-\U0001F1FF"   # Country flags
            "\U00002702-\U000027B0"   # Dingbats (✂)
            "\U000024C2-\U0001F251"   # Enclosed characters
            "\U0001f926-\U0001f937"   # Supplemental symbols (🤷)
            "\U00010000-\U0010ffff"   # Other emoji
            "\u2640-\u2642"           # Gender symbols
            "\u2600-\u2B55"           # Misc (☀🔴)
            "\u200d"                  # Zero-width joiner (compound emoji)
            "\ufe0f"                  # Variation selector
            "]+",
            flags=re.UNICODE
        )

        # Build list of (type, content) tuples in message order
        text_parts  = emoji_re.split(message)
        emoji_parts = emoji_re.findall(message)
        segments    = []
        for i, text in enumerate(text_parts):
            if text:
                segments.append(("text", text))
            if i < len(emoji_parts):
                segments.append(("emoji", emoji_parts[i]))

        d          = self._cfg.delays
        char_count = 0

        for kind, content in segments:

            if kind == "text":
                # ── STEALTH 1: Type character by character ──────
                for char in content:
                    await self._page.type(
                        selector,
                        char,
                        delay=random.uniform(
                            d.type_speed_min,
                            d.type_speed_max
                        )
                    )
                    char_count += 1

                    # Mid-message thinking pause for longer messages.
                    # A real person sometimes pauses to think about
                    # phrasing mid-sentence. We simulate that here.
                    if (
                        len(message) > 80
                        and char_count % random.randint(40, 60) == 0
                    ):
                        await asyncio.sleep(random.uniform(0.8, 2.0))

            elif kind == "emoji":
                # ── STEALTH 6: Inject emoji via JavaScript ───────
                # This inserts the emoji at the current cursor position
                # in the WhatsApp input box without using keyboard events.
                # A separate input event is dispatched so WhatsApp
                # registers the change and enables the send button.
                try:
                    await self._page.evaluate(
                        """(args) => {
                            const el = document.querySelector(args.selector);
                            if (!el) return;
                            const sel = window.getSelection();
                            if (!sel || !sel.rangeCount) return;
                            const range = sel.getRangeAt(0);
                            const node  = document.createTextNode(args.emoji);
                            range.insertNode(node);
                            range.setStartAfter(node);
                            range.setEndAfter(node);
                            sel.removeAllRanges();
                            sel.addRange(range);
                            // Tell WhatsApp the content changed
                            el.dispatchEvent(
                                new Event('input', { bubbles: true })
                            );
                        }""",
                        {"selector": selector, "emoji": content}
                    )
                    await asyncio.sleep(random.uniform(0.05, 0.2))
                except Exception:
                    # Fallback: try typing directly
                    # May garble on some systems but better than crashing
                    self._log.debug(f"JS emoji injection failed — falling back to type()")
                    await self._page.type(selector, content, delay=50)

    async def _check_invalid_number(self) -> bool:
        """
        Check if WhatsApp is showing an 'invalid/unregistered number' modal.

        After navigating to a chat URL, WhatsApp Web may show a modal
        with text like "Phone number shared via url is invalid" if the
        number is not registered. We check for these strings in the
        page body before attempting to type anything.

        Returns:
            True  → number is NOT on WhatsApp, mark INVALID_NUMBER
            False → number appears valid, proceed with sending
        """
        try:
            body_text = (await self._page.inner_text("body")).lower()
            return any(pattern in body_text for pattern in self.INVALID_TEXTS)
        except Exception:
            # If we can't read the body, assume valid and let send fail naturally
            return False

    async def _take_screenshot(self, order_id: str) -> str:
        """
        Capture a screenshot of the browser at the moment of delivery.
        Saved as: screenshots/{order_id}_{timestamp}.png

        This gives you visual proof that each message was sent,
        showing the message in the chat with the delivery tick.

        Args:
            order_id: Used in the filename so screenshots are traceable.

        Returns:
            Path string to the saved screenshot, or "" on failure.
        """
        try:
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Sanitize order_id — remove characters invalid in filenames
            safe_id  = re.sub(r"[^\w\-]", "_", str(order_id))
            filename = f"{safe_id}_{ts}.png"
            path     = self._ss_path / filename

            await self._page.screenshot(
                path=str(path),
                full_page=False   # Capture viewport only, not full page
            )
            self._log.debug(f"Screenshot saved: {path}")
            return str(path)
        except Exception as e:
            self._log.warning(f"Screenshot failed: {e}")
            return ""

    # ── SEND METHODS ───────────────────────────────────────────

    async def send_text(
        self,
        phone:    str,
        message:  str,
        order_id: str
    ) -> SendResult:
        """
        Send a text message to one WhatsApp number.
        Applies all 8 stealth techniques in order.

        Flow:
          1. Tab rotation check         (STEALTH 8)
          2. Navigate to chat URL       (STEALTH 4 — no ?text=)
          3. Wait for chat to load
          4. Check for invalid number modal
          5. Pre-typing pause           (STEALTH 2)
          6. Occasional scroll          (STEALTH 7)
          7. Human mouse to input       (STEALTH 3)
          8. Click input, type message  (STEALTH 1 + 6)
          9. Press Enter
         10. Wait for delivery tick
         11. Screenshot for proof
         12. Return SendResult

        Args:
            phone:    13-digit normalized phone e.g. "2348038365784"
            message:  Full rendered message string
            order_id: For logging and screenshot naming

        Returns:
            SendResult with status SENT | FAILED | INVALID_NUMBER
        """
        self._log.info(f"→ Sending to +{phone} [Order: {order_id}]")

        try:
            # ── STEALTH 8: Rotate tab if threshold reached ──────
            await self._rotate_tab()

            # ── STEALTH 4: Navigate WITHOUT ?text= parameter ────
            # Clean URL — no pre-filled text
            url = f"https://web.whatsapp.com/send?phone={phone}"
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20_000
            )

            # Wait for the message input to appear (chat loaded)
            try:
                await self._page.wait_for_selector(
                    self.SEL["msg_input"],
                    timeout=15_000
                )
            except Exception:
                # Input didn't appear — check if it's an invalid number
                if await self._check_invalid_number():
                    self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )
                # Genuine timeout — not invalid number, just slow load
                raise TimeoutError(
                    f"Chat did not load within 15s for +{phone}"
                )

            # Double-check for invalid number after load
            # (modal sometimes appears after the input is visible)
            if await self._check_invalid_number():
                self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            # ── STEALTH 2: Pre-typing pause ─────────────────────
            # Simulate the user reading the chat before replying
            pre_pause = random.uniform(
                self._cfg.delays.pre_type_min,
                self._cfg.delays.pre_type_max
            )
            self._log.debug(f"  Pre-type pause: {pre_pause:.1f}s")
            await asyncio.sleep(pre_pause)

            # ── STEALTH 7: Occasional random scroll ─────────────
            # Simulate glancing at previous messages before typing
            if random.choice([True, False]):
                scroll_up = random.uniform(100, 400)
                await self._page.evaluate(f"window.scrollBy(0, -{scroll_up})")
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await self._page.evaluate("window.scrollBy(0, 10000)")
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # ── STEALTH 3: Human mouse movement ─────────────────
            # Move to a random position first, then to the input box
            el = await self._page.query_selector(self.SEL["msg_input"])
            if el:
                box = await el.bounding_box()
                if box:
                    center_x = int(box["x"] + box["width"]  / 2)
                    center_y = int(box["y"] + box["height"] / 2)
                    await self._mouse_human_to(center_x, center_y)

            # Click the input to focus it
            await self._page.click(self.SEL["msg_input"])
            await asyncio.sleep(random.uniform(0.2, 0.5))

            # ── STEALTH 1 + 6: Type message ─────────────────────
            await self._type_human(self.SEL["msg_input"], message)

            # Small pause after finishing typing — human habit
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Press Enter to send
            await self._page.keyboard.press("Enter")
            self._log.debug("  Message sent. Waiting for delivery tick...")

            # Wait for send confirmation tick
            # Single tick (msg-check) = delivered to WhatsApp server
            # Double tick (msg-dblcheck) = delivered to recipient's phone
            # Either is sufficient confirmation — we don't wait for blue ticks
            try:
                await self._page.wait_for_selector(
                    self.SEL["sent_tick"],
                    timeout=15_000
                )
                self._log.info(f"  ✓ Delivered: +{phone}")
            except Exception:
                # Tick didn't appear within 15s.
                # Message was likely sent but we couldn't confirm.
                # Mark as SENT anyway — better than a false FAILED.
                self._log.warning(
                    f"  ⚠ Tick timeout for +{phone} — "
                    f"message likely delivered (unconfirmed)"
                )

            # Screenshot for delivery proof
            screenshot_path = await self._take_screenshot(order_id)

            # Increment tab message counter (for STEALTH 8)
            self._msgs_on_tab += 1

            return SendResult(
                success=True,
                status="SENT",
                screenshot_path=screenshot_path
            )

        except Exception as e:
            self._log.error(
                f"  ✗ Failed for +{phone}: {e}",
                exc_info=True
            )
            return SendResult(
                success=False,
                status="FAILED",
                error_message=str(e)
            )

    async def send_image(
        self,
        phone:      str,
        image_path: str,
        caption:    str,
        order_id:   str
    ) -> SendResult:
        """
        Send an image with a caption to one WhatsApp number.
        Used when cfg.image_path is configured in config.py.

        Flow:
          1. Navigate to chat (same as send_text)
          2. Click attachment button (paperclip icon)
          3. Upload image file via file chooser
          4. Wait for image preview to load
          5. Type caption in caption input
          6. Press Enter to send

        Args:
            phone:      13-digit normalized phone
            image_path: Local path to image file e.g. "data/product.jpg"
            caption:    Text caption rendered from message template
            order_id:   For logging and screenshot naming

        Returns:
            SendResult with status SENT | FAILED | INVALID_NUMBER
        """
        self._log.info(f"→ Sending image to +{phone} [Order: {order_id}]")

        try:
            await self._rotate_tab()

            await self._page.goto(
                f"https://web.whatsapp.com/send?phone={phone}",
                wait_until="domcontentloaded",
                timeout=20_000
            )

            try:
                await self._page.wait_for_selector(
                    self.SEL["msg_input"],
                    timeout=15_000
                )
            except Exception:
                if await self._check_invalid_number():
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )
                raise

            if await self._check_invalid_number():
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            # Click the attachment (paperclip) button
            await self._page.click(self.SEL["attach_btn"])
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Handle the file chooser dialog
            async with self._page.expect_file_chooser() as fc_info:
                await self._page.click('input[accept*="image"]')
            file_chooser = await fc_info.value
            await file_chooser.set_files(image_path)

            # Wait for the image preview to appear in the media editor
            await self._page.wait_for_selector(
                'div[data-testid="media-editor"]',
                timeout=10_000
            )
            await asyncio.sleep(1.0)

            # Type caption in the caption input field
            try:
                await self._page.click(self.SEL["caption_input"])
                await self._type_human(self.SEL["caption_input"], caption)
            except Exception:
                self._log.warning(
                    "Caption input not found — sending image without caption"
                )

            # Send with Enter
            await self._page.keyboard.press("Enter")
            await asyncio.sleep(2.0)   # Image upload takes longer to confirm

            screenshot_path = await self._take_screenshot(order_id)
            self._msgs_on_tab += 1

            return SendResult(
                success=True,
                status="SENT",
                screenshot_path=screenshot_path
            )

        except Exception as e:
            self._log.error(
                f"  ✗ Image send failed for +{phone}: {e}",
                exc_info=True
            )
            return SendResult(
                success=False,
                status="FAILED",
                error_message=str(e)
            )

# ── END OF FILE 11 ────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 12
#  PATH:  whatsapp_automation/src/scheduler.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   The orchestration layer — coordinates every module to run
#   the full sending day. Uses APScheduler to trigger 6 sessions
#   at the times configured in config.py.
#
#   Each session:
#     1. Verifies the WhatsApp connection is still alive
#     2. Fetches the next batch of customers from the database
#     3. For each customer:
#        a. Checks deduplication (skip if already sent)
#        b. Builds personalized message (random template A or B)
#        c. Sends via the WhatsApp sender
#        d. Updates the database with the result
#        e. Applies human-like delays before next message
#     4. After last session: triggers the daily report
#
#   Human-like delay pattern within each session:
#     Message → wait 55–110s (+ jitter)
#     Every 4 messages → burst pause 4–8 min
#     Once per session (30% chance) → long pause 8–15 min
#     Minimum enforced gap: 30 seconds between any two messages
#
# FUTURE / FASTAPI NOTE:
#   Scheduler is a pure async service class with no CLI dependency.
#   When adding FastAPI, expose these capabilities as API routes:
#
#   Run immediately (for a "send now" button in Next.js UI):
#     @app.post("/sessions/run-now")
#     async def run_now_endpoint(count: int = 5):
#         await scheduler.run_now(count)
#         return {"status": "started", "count": count}
#
#   Start scheduled day (for a "start today's campaign" button):
#     @app.post("/sessions/start")
#     async def start_schedule():
#         # Run as FastAPI background task so it doesn't block the response
#         background_tasks.add_task(scheduler.start_background)
#         return {"status": "scheduled"}
#
#   The Scheduler itself doesn't need to change — only new route
#   functions need to be added in api.py.
# ================================================================
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/scheduler.py ────

import asyncio
import logging
import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.whatsapp_sender import WhatsAppSender, SendResult

logger = logging.getLogger(__name__)


class Scheduler:
    """
    Orchestrates the full sending day across 6 APScheduler sessions.

    Connects all modules together:
        AppConfig   → session timing, delay settings, daily limit
        Database    → fetch pending customers, record results
        Sender      → WhatsApp Web automation (or future API)
        Builder     → personalized message per customer
        Reporter    → end-of-day summary generation

    The Scheduler never imports PlaywrightSender directly.
    It only knows about the WhatsAppSender interface.
    This makes swapping to CloudAPISender a one-line change in main.py.
    """

    def __init__(self, cfg, db, sender: WhatsAppSender, builder, reporter):
        """
        Args:
            cfg:      AppConfig instance
            db:       Database instance
            sender:   Any WhatsAppSender implementation
            builder:  MessageBuilder instance
            reporter: Reporter instance
        """
        self._cfg       = cfg
        self._db        = db
        self._sender    = sender
        self._builder   = builder
        self._reporter  = reporter
        self._scheduler = AsyncIOScheduler()
        self._log       = logging.getLogger(self.__class__.__name__)

        # Index of the last session — used to trigger end-of-day tasks
        self._last_session_idx = len(cfg.session_schedule) - 1

    async def run_session(self, session_idx: int, session_count: int):
        """
        Execute one sending session.
        Called by APScheduler at the scheduled time, or by run_now() directly.

        Args:
            session_idx:   Index of this session (0-based).
                           Used to detect the last session of the day.
            session_count: How many messages to send in this session.
        """
        self._log.info(
            f"\n{'=' * 50}\n"
            f"Session {session_idx + 1} of {len(self._cfg.session_schedule)} "
            f"— {session_count} messages planned\n"
            f"{'=' * 50}"
        )

        # ── Verify connection before starting ──────────────────
        # The session from --setup may have expired (phone logged out,
        # browser was restarted, etc.) Check and reconnect if needed.
        if not await self._sender.is_connected():
            self._log.warning(
                "WhatsApp session appears disconnected. "
                "Attempting to reconnect..."
            )
            try:
                await self._sender.connect()
                self._log.info("Reconnected successfully.")
            except Exception as e:
                self._log.error(
                    f"Reconnect failed: {e}\n"
                    f"Skipping session {session_idx + 1}. "
                    f"Will retry at next scheduled session."
                )
                return

        # ── Fetch next batch of customers ──────────────────────
        customers = self._db.get_pending(
            limit=session_count,
            order=self._cfg.send_order
        )

        if not customers:
            self._log.info("No pending customers for this session.")
            # Still trigger end-of-day if this is the last session
            if session_idx == self._last_session_idx:
                await self._end_of_day()
            return

        self._log.info(f"Fetched {len(customers)} customers for this session.")

        # ── Per-session counters ───────────────────────────────
        sent_count    = 0
        failed_count  = 0
        invalid_count = 0
        long_pause_used = False   # Only one long pause per session

        d = self._cfg.delays   # DelayConfig shorthand

        for i, customer in enumerate(customers):
            order_id = customer["order_id"]
            phone    = customer["normalized_phone"]
            name     = customer["first_name"]
            is_last  = (i == len(customers) - 1)

            # ── Deduplication check ────────────────────────────
            # Check again just before sending. Another session running
            # concurrently (rare but possible) may have already sent.
            if self._db.already_sent(order_id):
                self._log.info(f"  Skip (already sent): {name} [{order_id}]")
                continue

            # ── Build personalized message ─────────────────────
            message, template = self._builder.build(customer)

            self._log.info(
                f"  [{i + 1}/{len(customers)}] "
                f"{name} — Template {template} — +{phone}"
            )

            # ── Send (image or text) ───────────────────────────
            # Decide which send method to use based on config
            if (
                self._cfg.image_path
                and __import__("pathlib").Path(self._cfg.image_path).exists()
            ):
                result = await self._sender.send_image(
                    phone=phone,
                    image_path=self._cfg.image_path,
                    caption=message,
                    order_id=order_id
                )
            else:
                result = await self._sender.send_text(
                    phone=phone,
                    message=message,
                    order_id=order_id
                )

            # ── Update database with result ────────────────────
            if result.status == "SENT":
                self._db.mark_sent(
                    order_id,
                    message,
                    template,
                    result.screenshot_path
                )
                sent_count += 1

            elif result.status == "INVALID_NUMBER":
                self._db.mark_invalid(order_id)
                invalid_count += 1

            else:  # FAILED
                self._db.mark_failed(order_id, result.error_message)
                failed_count += 1

            # ── Apply delays ───────────────────────────────────
            # Skip delays after the last message in this session —
            # no point waiting when there's nothing next to send.
            if is_last:
                continue

            # Base delay between messages with random jitter
            base   = random.uniform(
                d.between_messages_min,
                d.between_messages_max
            )
            jitter = random.uniform(d.jitter_min, d.jitter_max)
            delay  = max(30, base + jitter)   # Never below 30 seconds

            self._log.info(f"  Waiting {delay:.0f}s before next message...")
            await asyncio.sleep(delay)

            # Burst pause after every burst_size messages
            # burst_size=4: pause after messages 4, 8, 12...
            if (i + 1) % d.burst_size == 0:
                burst = random.uniform(d.after_burst_min, d.after_burst_max)
                self._log.info(
                    f"  Burst pause after {d.burst_size} messages — "
                    f"waiting {burst:.0f}s ({burst / 60:.1f} min)..."
                )
                await asyncio.sleep(burst)

            # Long pause once per session (30% chance)
            # Only fires in the middle of the session, not near the end
            if (
                d.long_pause_enabled
                and not long_pause_used
                and random.random() < 0.30
                and i < len(customers) - 3   # Not near end of session
            ):
                long_p = random.uniform(d.long_pause_min, d.long_pause_max)
                self._log.info(
                    f"  Long pause — waiting {long_p:.0f}s "
                    f"({long_p / 60:.1f} min)..."
                )
                await asyncio.sleep(long_p)
                long_pause_used = True   # Only one per session

        # ── Session summary ────────────────────────────────────
        self._log.info(
            f"Session {session_idx + 1} complete — "
            f"{sent_count} sent, "
            f"{failed_count} failed, "
            f"{invalid_count} invalid."
        )

        # ── End-of-day tasks after the last session ────────────
        if session_idx == self._last_session_idx:
            await self._end_of_day()

    async def _end_of_day(self):
        """
        Run after the final session of the day.
        Generates the daily report and emails it if configured.
        Logs info about any messages eligible for retry tomorrow.
        """
        self._log.info("All sessions complete. Running end-of-day tasks...")

        report_text = self._reporter.generate_report(self._db)
        print("\n" + report_text)

        if self._cfg.has_email():
            sent = self._reporter.send_email(report_text, self._cfg)
            if sent:
                self._log.info(f"Report emailed to {self._cfg.smtp_to}")
            else:
                self._log.warning(
                    "Email report failed. Check logs for SMTP error details."
                )

        # Report retry-eligible customers for tomorrow
        retries = self._db.get_retry_eligible()
        if retries:
            self._log.info(
                f"{len(retries)} message(s) eligible for retry. "
                f"Run: python main.py --reset-failed"
            )
        else:
            self._log.info("No messages need retry.")

    def start(self):
        """
        Register all 6 sessions as APScheduler cron jobs and start.
        Prints the full schedule before starting.
        Blocks the process until all sessions complete or Ctrl+C.

        Called by: python main.py --run
        """
        jobs = self._cfg.session_jobs()

        # Print schedule so the user knows what's coming
        print("\n" + "=" * 50)
        print("  TODAY'S SENDING SCHEDULE")
        print("=" * 50)
        for i, job in enumerate(jobs):
            print(
                f"  Session {i + 1}:  "
                f"{job['hour']:02d}:{job['minute']:02d}  →  "
                f"{job['count']} messages"
            )
        print(f"\n  Total planned:  {self._cfg.total_daily_count()} messages")
        print("=" * 50)
        print("\n  Keep this window open and your laptop on.")
        print("  Do NOT open WhatsApp Web manually while running.")
        print("  Press Ctrl+C to stop.\n")

        # Register each session as a cron job
        for i, job in enumerate(jobs):
            self._scheduler.add_job(
                self.run_session,
                trigger="cron",
                hour=job["hour"],
                minute=job["minute"],
                args=[i, job["count"]],
                id=f"session_{i}",
                name=f"Session {i + 1} ({job['count']} msgs)",
                misfire_grace_time=300,   # Allow 5-minute late start
            )

        self._scheduler.start()

        # Run the event loop until Ctrl+C or all sessions complete
        loop = asyncio.get_event_loop()
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            self._log.info("Scheduler stopped by user (Ctrl+C).")
        finally:
            self._scheduler.shutdown(wait=False)

    async def run_now(self, count: int):
        """
        Run one immediate session without waiting for APScheduler.
        Used for testing: python main.py --run --now --count 3

        Args:
            count: Number of messages to send in this immediate session.
        """
        self._log.info(
            f"Immediate session — sending {count} message(s) now..."
        )
        await self.run_session(session_idx=0, session_count=count)

# ── END OF FILE 12 ────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 13
#  PATH:  whatsapp_automation/src/reporter.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   Generates the daily summary report and optionally emails it.
#   Called automatically after the last session by the Scheduler,
#   or manually via: python main.py --report
#
# REPORT CONTENTS:
#   - Count per status (SENT, FAILED, INVALID_NUMBER, etc.)
#   - Template A vs B breakdown (which got more sends today)
#   - List of failed customers with names, phones, error messages
#   - List of invalid numbers
#   - Timestamp
#
# EMAIL:
#   Uses Gmail SMTP with STARTTLS.
#   Requires a Gmail App Password (not regular Gmail password).
#   Email failure NEVER crashes the system — logged and continued.
#
# FUTURE / FASTAPI NOTE:
#   Reporter is a pure service class. FastAPI can expose it as:
#
#   GET /reports/today  →  returns get_daily_summary() as JSON
#   GET /reports/list   →  lists all report files in reports/ folder
#   GET /reports/{date} →  serves a specific report file
#
#   The Next.js dashboard fetches /reports/today and renders
#   the counts as charts, the failed list as a table, etc.
#
#   For that, add a to_dict() method to Reporter that returns
#   a clean JSON-serializable dict from get_daily_summary().
#   No changes to the existing methods needed.
# ================================================================
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/reporter.py ─────

import logging
import smtplib
from datetime import datetime, date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)


class Reporter:
    """
    Generates the daily summary report and emails it.

    Has no dependency on any other src/ module except Database
    (passed at call time, not stored as a dependency).
    This makes Reporter trivially testable and reusable from
    any context — CLI, FastAPI route, scheduled task.

    Usage:
        reporter = Reporter()

        # Generate and save the report:
        report_text = reporter.generate_report(db)

        # Optionally email it:
        sent = reporter.send_email(report_text, cfg)
    """

    def __init__(self):
        self._log = logging.getLogger(self.__class__.__name__)

    def generate_report(self, db) -> str:
        """
        Build the daily text report from DB summary data.
        Saves it to reports/daily_report_{date}.txt.
        Returns the report as a string (for printing and emailing).

        Args:
            db: Database instance

        Returns:
            Full report as a plain text string.
        """
        summary   = db.get_daily_summary()
        today_str = date.today().strftime("%Y-%m-%d")

        # ── Build report lines ─────────────────────────────────
        lines = [
            f"WhatsApp Automation Report — {today_str}",
            "=" * 52,
            "",
            "SUMMARY",
            f"  Sent:             {summary.get('SENT', 0)}",
            f"  Failed:           {summary.get('FAILED', 0)}",
            f"  Failed (final):   {summary.get('FAILED_FINAL', 0)}",
            f"  Invalid number:   {summary.get('INVALID_NUMBER', 0)}",
            f"  Pending:          {summary.get('PENDING', 0)}",
            "",
            "TEMPLATE PERFORMANCE (today)",
            f"  Template A sent:  {summary.get('template_A', 0)}",
            f"  Template B sent:  {summary.get('template_B', 0)}",
            "",
        ]

        # ── Failed details ─────────────────────────────────────
        failed = summary.get("failed_details", [])
        if failed:
            lines.append("FAILED — eligible for retry (run --reset-failed)")
            lines.append("-" * 40)
            for item in failed:
                lines.append(
                    f"  {item['name']}  |  "
                    f"+{item['phone']}  |  "
                    f"{item['error']}"
                )
        else:
            lines.append("FAILED: None")

        lines.append("")

        # ── Invalid number details ─────────────────────────────
        invalid = summary.get("invalid_details", [])
        if invalid:
            lines.append("INVALID NUMBERS — not on WhatsApp (will not retry)")
            lines.append("-" * 40)
            for item in invalid:
                lines.append(f"  {item['name']}  |  {item['phone']}")
        else:
            lines.append("INVALID NUMBERS: None")

        lines += [
            "",
            "=" * 52,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        report_text = "\n".join(lines)

        # ── Save to file ───────────────────────────────────────
        Path("reports").mkdir(exist_ok=True)
        report_path = Path("reports") / f"daily_report_{today_str}.txt"
        report_path.write_text(report_text, encoding="utf-8")
        self._log.info(f"Report saved: {report_path}")

        return report_text

    def send_email(self, report_text: str, cfg) -> bool:
        """
        Email the daily report via Gmail SMTP with STARTTLS.

        IMPORTANT: This method NEVER raises an exception.
        Email failure is logged and False is returned.
        The automation must not crash because of a failed email.

        Gmail App Password setup:
          myaccount.google.com → Security → App Passwords
          Generate one for "Mail" and add it to config.py smtp_password.
          Do NOT use your regular Gmail password here.

        Args:
            report_text: The plain text report string.
            cfg:         AppConfig instance (provides SMTP settings).

        Returns:
            True if email sent successfully.
            False if email failed (check logs for details).
        """
        if not cfg.has_email():
            self._log.info(
                "Email not configured (smtp_password is empty) — skipping."
            )
            return False

        try:
            today_str   = date.today().strftime("%Y-%m-%d")
            report_path = Path("reports") / f"daily_report_{today_str}.txt"

            # ── Build the email ────────────────────────────────
            msg            = MIMEMultipart()
            msg["From"]    = cfg.smtp_email
            msg["To"]      = cfg.smtp_to
            msg["Subject"] = f"WhatsApp Automation Report — {today_str}"

            # Plain text body
            msg.attach(MIMEText(report_text, "plain"))

            # Attach the saved report file if it exists
            if report_path.exists():
                with open(report_path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f"attachment; filename={report_path.name}"
                    )
                    msg.attach(part)

            # ── Send via Gmail SMTP ────────────────────────────
            # Port 587 with STARTTLS is the secure Gmail standard.
            # Do not use port 465 (SSL) — STARTTLS is preferred.
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(cfg.smtp_email, cfg.smtp_password)
                smtp.sendmail(
                    cfg.smtp_email,
                    cfg.smtp_to,
                    msg.as_string()
                )

            self._log.info(f"Report emailed successfully to {cfg.smtp_to}")
            return True

        except Exception as e:
            # Log the full error but DO NOT raise
            # Email failure must never crash the automation
            self._log.error(f"Email report failed: {e}", exc_info=True)
            return False

# ── END OF FILE 13 ────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 14
#  PATH:  whatsapp_automation/main.py
#  TYPE:  Python file — the entry point
# ================================================================
# PURPOSE:
#   The only file you ever run directly.
#   Wires all modules together and exposes clean CLI commands.
#
# COMMANDS:
#   python main.py --setup           First-time: import Excel, scan QR
#   python main.py --preview         Stats + schedule (no browser, no sending)
#   python main.py --dry-run         Preview messages (no browser, no sending)
#   python main.py --run             Start full 6-session scheduled day
#   python main.py --run --now       Immediate test session (default: 3 msgs)
#   python main.py --run --now --count 10   Immediate test, 10 messages
#   python main.py --report          Generate today's report + email it
#   python main.py --reset-failed    Reset FAILED → PENDING for retry
#
# STARTUP CHECKS (run before every command):
#   - Create all required folders silently (data/, logs/, etc.)
#   - Load and validate AppConfig — exit cleanly if invalid
#   - Register signal handlers for Ctrl+C clean shutdown
#
# FUTURE / FASTAPI NOTE:
#   When you add FastAPI, create a separate api.py file in the root:
#
#   # api.py
#   from fastapi import FastAPI
#   from src.config import AppConfig
#   from src.database import Database
#   from src.playwright_sender import PlaywrightSender
#   from src.scheduler import Scheduler
#   from src.reporter import Reporter
#   from src.message_builder import MessageBuilder
#
#   app = FastAPI()
#   cfg = AppConfig()
#   db  = Database(cfg.database_url); db.init()
#   ...
#
#   main.py stays exactly as it is — the CLI runner for local use.
#   api.py becomes the FastAPI runner for hosted/production use.
#   Both share the same src/ modules — no code duplication.
#
#   Run locally:       python main.py --run
#   Run as API server: uvicorn api:app --reload
# ================================================================
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: main.py ─────────────

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# ── Logging must be set up before any other imports ────────────
# Other modules call logging.getLogger() at module load time.
# If we set up logging after importing them, their loggers
# won't have handlers attached and messages will be silently lost.
def _setup_logging():
    """Configure console + rotating file logging for the application."""
    import logging.handlers

    Path("logs").mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console: INFO and above — clean output the user reads in real time
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S"
    ))

    # File: DEBUG and above — full trace for troubleshooting
    file_h = logging.handlers.RotatingFileHandler(
        "logs/automation.log",
        maxBytes=5 * 1024 * 1024,   # 5 MB per file
        backupCount=3,
        encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    root.addHandler(console)
    root.addHandler(file_h)

_setup_logging()   # Must be called before the imports below

# ── Application imports ────────────────────────────────────────
from src.config import AppConfig
from src.database import Database
from src.data_reader import DataReader
from src.message_builder import MessageBuilder
from src.playwright_sender import PlaywrightSender
from src.scheduler import Scheduler
from src.reporter import Reporter

log = logging.getLogger("main")


# ==============================================================
# FOLDER SETUP — Ensures all required directories exist
# ==============================================================

def _create_directories():
    """
    Create all required project directories if they don't exist.
    Called silently before every command so you never hit a
    FileNotFoundError mid-session because a folder is missing.
    """
    for folder in ["data", ".sessions", "logs", "reports", "screenshots"]:
        Path(folder).mkdir(parents=True, exist_ok=True)


# ==============================================================
# CLI COMMAND FUNCTIONS
# ==============================================================
# One async function per command.
# main() parses arguments and calls the right function.
# ==============================================================

async def cmd_setup(cfg: AppConfig):
    """
    --setup: First-time initialization.

    1. Creates all folders
    2. Initializes the database (creates tables)
    3. Imports Excel file → database
    4. Opens browser for WhatsApp QR scan
    5. Saves session for future runs

    Run this once before ever using --run.
    You can re-run it to import a fresh Excel export — existing
    customers are updated (upsert), not duplicated.
    """
    print("\n" + "=" * 55)
    print("  WHATSAPP AUTOMATION — SETUP")
    print("=" * 55)

    _create_directories()
    print("✅ Folders ready.")

    # Initialize database tables
    db = Database(cfg.database_url)
    db.init()
    print("✅ Database ready.")

    # Check Excel file exists before proceeding
    excel_path = cfg.excel_path()
    if not excel_path.exists():
        print(f"\n❌ Excel file not found: {excel_path}")
        print(f"   Drop your file into the data/ folder as: {cfg.excel_filename}")
        print("   Then run --setup again.")
        return

    # Import Excel → database
    reader    = DataReader(cfg.country_code)
    customers = reader.read_and_filter(excel_path, cfg.target_product)

    if not customers:
        print(
            f"\n⚠️  No customers matched product keyword: '{cfg.target_product}'\n"
            "   Check your Excel file and the target_product setting in config.py."
        )
        return

    for customer in customers:
        db.upsert_customer(customer)

    stats = db.get_stats()
    print(
        f"✅ Excel imported: {stats['total']} customers, "
        f"{stats['pending']} pending, "
        f"{stats['invalid_phones']} invalid phones."
    )

    # Open browser for WhatsApp login
    print("\n" + "=" * 55)
    print("  Opening browser for WhatsApp login...")
    print("=" * 55)

    sender = PlaywrightSender(cfg)
    try:
        connected = await sender.connect()
        if connected:
            print("\n✅ WhatsApp connected. Login session saved.")
            print("   You won't need to scan QR again on future runs.")
            print(
                f"\n✅ Setup complete. Run next:\n"
                f"   python main.py --preview\n"
                f"   python main.py --dry-run"
            )
    finally:
        await sender.disconnect()


def cmd_preview(cfg: AppConfig):
    """
    --preview: Show customer stats and today's schedule.
    No browser opened. No messages sent. Safe to run any time.
    """
    db = Database(cfg.database_url)
    db.init()

    stats   = db.get_stats()
    samples = db.get_sample_customers(5)

    print("\n" + "=" * 50)
    print("  WHATSAPP AUTOMATION — PREVIEW")
    print("=" * 50)
    print(f"  Target product:    {cfg.target_product}")
    print(f"  Daily limit:       {cfg.daily_limit}")
    print(f"  Send order:        {cfg.send_order}")
    print(f"  Email reports:     {'yes' if cfg.has_email() else 'not configured'}")
    print()
    print(f"  Total customers:   {stats['total']}")
    print(f"  Pending (unsent):  {stats['pending']}")
    print(f"  Already sent:      {stats['sent']}")
    print(f"  Invalid numbers:   {stats['invalid']}")
    print(f"  Bad phones:        {stats['invalid_phones']}")

    if samples:
        print(f"\n  Sample names:      {', '.join(samples)}")

    print("\n  TODAY'S SCHEDULE")
    print("  " + "-" * 35)
    for i, job in enumerate(cfg.session_jobs()):
        print(
            f"  Session {i + 1}:  "
            f"{job['hour']:02d}:{job['minute']:02d}  →  "
            f"{job['count']} messages"
        )
    print(f"\n  Total today:       {cfg.total_daily_count()} messages\n")


def cmd_dry_run(cfg: AppConfig):
    """
    --dry-run: Show customer stats + print both message templates
    for the first 3 pending customers. Zero messages sent.
    No browser opened.

    Always run this after --setup to confirm messages look correct
    before committing to the full send run.
    """
    cmd_preview(cfg)

    db = Database(cfg.database_url)
    db.init()

    pending = db.get_pending(limit=3, order=cfg.send_order)

    if not pending:
        print("  No pending customers to preview messages for.")
        return

    builder = MessageBuilder(cfg)

    print("=" * 50)
    print("  DRY RUN — SAMPLE MESSAGES (nothing is being sent)")
    print("=" * 50)

    for customer in pending:
        print(f"\n  {'─' * 45}")
        print(
            f"  Customer: {customer['first_name']}  "
            f"|  Order: {customer['order_id']}"
        )
        print(f"  {'─' * 45}")

        both = builder.preview(customer)

        print("\n  [TEMPLATE A — Results Check-In]\n")
        for line in both["A"].split("\n"):
            print(f"  {line}")

        print("\n  [TEMPLATE B — Honest Feedback]\n")
        for line in both["B"].split("\n"):
            print(f"  {line}")

    print("\n" + "=" * 50)
    print("  DRY RUN COMPLETE — Zero messages were sent.")
    print("=" * 50 + "\n")


async def cmd_run(cfg: AppConfig, run_now: bool = False, count: int = 3):
    """
    --run:              Start the full 6-session scheduled day.
    --run --now:        Run one immediate session (default: 3 messages).
    --run --now --count N: Immediate session with N messages.

    The browser must already be connected (run --setup first).
    If the session has expired, connect() will detect it and
    show the QR code again automatically.
    """
    if cfg.daily_limit > 200:
        print(
            f"\n⚠️  WARNING: daily_limit={cfg.daily_limit} exceeds "
            f"recommended maximum of 200.\n"
        )

    db       = Database(cfg.database_url)
    db.init()
    builder  = MessageBuilder(cfg)
    reporter = Reporter()
    sender   = PlaywrightSender(cfg)

    try:
        log.info("Connecting to WhatsApp Web...")
        connected = await sender.connect()
        if not connected:
            print("❌ Could not connect to WhatsApp. Run --setup first.")
            return

        scheduler = Scheduler(cfg, db, sender, builder, reporter)

        if run_now:
            # Immediate single session for testing
            await scheduler.run_now(count)
        else:
            # Full scheduled day — this blocks until all sessions complete
            scheduler.start()

    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user (Ctrl+C).")
    except Exception as e:
        log.error(f"Run failed: {e}", exc_info=True)
        print(f"\n❌ Error: {e}")
        print("   Full details in: logs/automation.log")
    finally:
        # Always disconnect cleanly — never leave browser hanging open
        log.info("Disconnecting browser...")
        await sender.disconnect()


def cmd_report(cfg: AppConfig):
    """
    --report: Generate today's report and optionally email it.
    No messages sent.
    """
    db       = Database(cfg.database_url)
    db.init()
    reporter = Reporter()

    report_text = reporter.generate_report(db)
    print("\n" + report_text)

    if cfg.has_email():
        sent = reporter.send_email(report_text, cfg)
        if sent:
            print(f"\n✅ Report emailed to {cfg.smtp_to}")
        else:
            print("\n❌ Email failed — check logs/automation.log for details")
    else:
        print(
            "\n(Email not configured — "
            "set smtp_email + smtp_password in config.py to enable)"
        )


def cmd_reset_failed(cfg: AppConfig):
    """
    --reset-failed: Reset FAILED entries back to PENDING for retry.
    FAILED_FINAL entries (2+ attempts) are NOT reset.
    Run this the next morning before --run to retry yesterday's failures.
    """
    db    = Database(cfg.database_url)
    db.init()
    count = db.reset_failed()
    print(f"\n✅ {count} message(s) reset to PENDING for next --run.")
    if count == 0:
        print("   (No FAILED entries found — nothing to reset)")


# ==============================================================
# MAIN ENTRY POINT
# ==============================================================

def main():
    """
    Parse CLI arguments, run startup checks, dispatch to commands.
    This is the only function called when you run: python main.py
    """
    # ── Argument parser ────────────────────────────────────────
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="WhatsApp Review Automation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --setup                    First-time setup
  python main.py --preview                  Check customer count + schedule
  python main.py --dry-run                  Preview messages before sending
  python main.py --run --now --count 3      Send 3 test messages now
  python main.py --run                      Start full scheduled day
  python main.py --report                   Generate and email today's report
  python main.py --reset-failed             Reset failed messages for retry
        """
    )

    parser.add_argument(
        "--setup",
        action="store_true",
        help="First-time setup: import Excel, scan QR, save session"
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show customer stats and today's schedule (no sending)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview messages that will be sent (no browser, no sending)"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Start the scheduled sending day"
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run one session immediately (use with --run)"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Number of messages for --now session (default: 3)"
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate today's report and email it"
    )
    parser.add_argument(
        "--reset-failed",
        action="store_true",
        dest="reset_failed",
        help="Reset failed messages back to pending for retry"
    )

    args = parser.parse_args()

    # Show help if no command given
    if not any([
        args.setup, args.preview, args.dry_run,
        args.run, args.report, args.reset_failed
    ]):
        parser.print_help()
        sys.exit(0)

    # ── Pre-flight: create folders ─────────────────────────────
    _create_directories()

    # ── Pre-flight: load and validate config ───────────────────
    try:
        cfg = AppConfig()
    except ValueError as e:
        print(f"\n❌ Configuration error:\n   {e}")
        print("   Edit src/config.py → CONFIG dict to fix this.")
        sys.exit(1)

    # ── Signal handlers for clean shutdown ─────────────────────
    # These ensure the browser is closed cleanly on Ctrl+C or kill signal.
    def _handle_signal(signum, frame):
        log.info(f"Signal {signum} received — shutting down cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Dispatch to the right command ──────────────────────────
    try:
        if args.setup:
            asyncio.run(cmd_setup(cfg))

        elif args.preview:
            cmd_preview(cfg)

        elif args.dry_run:
            cmd_dry_run(cfg)

        elif args.run:
            asyncio.run(
                cmd_run(cfg, run_now=args.now, count=args.count)
            )

        elif args.report:
            cmd_report(cfg)

        elif args.reset_failed:
            cmd_reset_failed(cfg)

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        sys.exit(0)
    except Exception as e:
        log.error(f"Unexpected error: {e}", exc_info=True)
        print(f"\n❌ Unexpected error: {e}")
        print("   Full details in: logs/automation.log")
        sys.exit(1)


# ── Standard Python entry point guard ─────────────────────────
# Ensures main() only runs when this file is executed directly.
# Running: python main.py     → main() is called
# Running: import main        → main() is NOT called (safe for testing)
if __name__ == "__main__":
    main()

# ── END OF FILE 14 ────────────────────────────────────────────


# ==============================================================
# DAY 3 VERIFICATION — Run in order after all 4 files are saved
# ==============================================================
#
# ── STEP 1: Verify imports are clean ──────────────────────────
#   python -c "
#   from src.playwright_sender import PlaywrightSender
#   from src.scheduler import Scheduler
#   from src.reporter import Reporter
#   print('All Day 3 imports OK')
#   "
#   Expected: All Day 3 imports OK
#
# ── STEP 2: First-time setup (runs once) ──────────────────────
#   python main.py --setup
#
#   What happens:
#     - Folders created
#     - Database tables created
#     - Your 96 Sadoer customers imported from Excel
#     - Chrome browser opens
#     - QR code appears
#     - Scan with your phone → session saved
#     - Browser closes
#
#   What you should see:
#     ✅ Folders ready.
#     ✅ Database ready.
#     ✅ Excel imported: 96 customers, 96 pending, X invalid phones.
#     ✅ WhatsApp connected. Login session saved.
#
# ── STEP 3: Preview customers and schedule ────────────────────
#   python main.py --preview
#
#   Expected output includes:
#     Total customers:   96
#     Pending (unsent):  96
#     TODAY'S SCHEDULE:  6 sessions listed
#
# ── STEP 4: Preview the exact messages ────────────────────────
#   python main.py --dry-run
#
#   Read Template A and Template B for 3 customers.
#   Confirm names are correct (Titilayo not TITILAYO).
#   Confirm discount_offer shows your actual offer.
#   Edit templates/ files or src/config.py if anything looks wrong.
#
# ── STEP 5: Send 3 test messages ──────────────────────────────
#   python main.py --run --now --count 3
#
#   Browser opens → loads your saved session → sends 3 messages.
#   You will see the messages appear in WhatsApp in real time.
#   Check your WhatsApp Web browser to confirm sending.
#   Then run --preview again to see sent count went from 0 to 3.
#
# ── STEP 6: Confirm deduplication works ───────────────────────
#   python main.py --run --now --count 3
#   (Run the exact same command again)
#
#   The system should skip the 3 already-sent customers and
#   move on to the next 3. You should see:
#     Skip (already sent): [name]
#   in the output for the first 3 customers.
#
# ── STEP 7: Full scheduled day ────────────────────────────────
#   python main.py --run
#   (Only run this when you're ready for real sending)
#
#   Keep your laptop on. The system will run 6 sessions
#   between 08:15 and 17:00. Total: 50 messages.
#   You can check progress any time with:
#     python main.py --preview
#
# ── IF ANYTHING GOES WRONG ────────────────────────────────────
#   1. Check logs/automation.log for full error details
#   2. If WhatsApp session expired: python main.py --setup
#      (re-scans QR, keeps existing customers in DB)
#   3. If messages failed: python main.py --reset-failed
#      then: python main.py --run --now --count 5
#      to retry the failed ones
#
# ==============================================================
# COMPLETE SYSTEM — ALL 14 FILES BUILT
# ==============================================================
# Day 1: requirements.txt, .gitignore, src/__init__.py,
#         src/config.py, src/database.py
#
# Day 2: src/data_reader.py, templates/message_a.j2,
#         templates/message_b.j2, src/message_builder.py,
#         src/whatsapp_sender.py
#
# Day 3: src/playwright_sender.py, src/scheduler.py,
#         src/reporter.py, main.py
#
# WHEN YOU'RE READY TO ADD FASTAPI + NEXT.JS:
#   The src/ modules need zero changes.
#   Add these new files only:
#     api.py           ← FastAPI app, routes, startup/shutdown events
#     schemas.py       ← Pydantic models for request/response validation
#     frontend/        ← Next.js project (separate folder)
#
#   api.py imports from src/ exactly like main.py does.
#   The CLI (main.py) and the API (api.py) share everything.
# ==============================================================