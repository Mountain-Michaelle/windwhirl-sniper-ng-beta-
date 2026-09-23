# ==============================================================
# PLAYWRIGHT SENDER — COMPLETE FILE (FIXED)
# ==============================================================
# FULL HUMAN EMULATION + FINGERPRINT ROTATION
# ==============================================================

import asyncio
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List

from apps.core.lib.utils.whatsapp_sender import WhatsAppSender, SendResult
from apps.core.lib.utils.fingerprint_manager import FingerprintManager

logger = logging.getLogger(__name__)


class PlaywrightSender(WhatsAppSender):
    """
    WhatsApp Web automation with full human emulation + fingerprint rotation.
    Single page, zero post-connect navigation.
    """

    # ── Selectors ──────────────────────────────────────────────
    SEARCH_SEL = 'input[aria-label="Search or start a new chat"]'
    
    MSG_INPUT_SELS = [
        'div[aria-label="Type a message"]',
        'div[aria-label="Message"]',
        'div[contenteditable="true"][data-tab="10"]',
        'div[contenteditable="true"][data-tab="1"]',
        'footer div[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
    ]

    SEL = {
        "chat_list":     'div[aria-label="Chat list"]',
        "qr_code":       'canvas[aria-label="Scan me!"], div[data-ref]',
        "sent_tick":     'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"]',
        "attach_btn":    'span[data-icon="attach-menu-plus"]',
        "caption_input": 'div[aria-label="Add a caption"]',
        "outgoing_msgs": 'div.message-out span.selectable-text',
        "outgoing_alt":  '.message-out .copyable-text span',
    }

    CAMPAIGN_KEYWORDS = [
        "sadoer", "collagen", "nabeau", "face serum",
        "face cream", "honest experience", "share your review",
        "voice note", "next order", "discount",
    ]

    INVALID_TEXTS = [
        "phone number shared via url is invalid",
        "not on whatsapp",
        "invalid phone number",
    ]

    # ── Timeouts ──────────────────────────────────────────────
    _TIMEOUTS = {
        "connect": 130_000,
        "qr_scan": 140_000,
        "search_bar": 8_000,
        "msg_input": 15_000,
        "tick": 15_000,
        "file_upload": 8_000,
        "media_editor": 10_000,
    }

    # ── Human Behavior Parameters ─────────────────────────────
    _HUMAN_BEHAVIOR = {
        "typing_speed_min": 80,
        "typing_speed_max": 450,
        "mistake_rate": 0.025,
        "hesitation_min": 0.5,
        "hesitation_max": 3.0,
        "reading_time_min": 2.0,
        "reading_time_max": 8.0,
        "scroll_chance": 0.3,
        "tab_switch_chance": 0.1,
        "idle_chance": 0.2,
        "idle_time_min": 2.0,
        "idle_time_max": 8.0,
        "daily_message_limit": 150,
        "session_break_after": 1500,
        "session_break_duration": 300,
        "inter_message_delay_min": 30,
        "inter_message_delay_max": 90,
    }

    def __init__(self, cfg):
        self._cfg = cfg
        self._log = logging.getLogger(self.__class__.__name__)
        self._sess_path = Path(".sessions") / "whatsapp_session"
        self._ss_path = Path("screenshots")
        self._pw = None
        self._ctx = None
        self._page = None
        self._sends_this_session = 0
        self._is_initialized = False
        self._last_state = None
        self._is_restricted = False
        
        # ── Fingerprint Manager ──────────────────────────────────
        self._fingerprint_manager = FingerprintManager(cache_dir=".fingerprints")
        self._current_fingerprint = None
        
        # ── Session activity tracking ──────────────────────────
        self._session_activity = {
            "messages_sent": 0,
            "session_start": datetime.now(),
            "active_time": 0,
            "last_activity": datetime.now(),
            "daily_count": 0,
            "last_daily_reset": datetime.now().date(),
        }

    # ═══════════════════════════════════════════════════════════
    # CONNECTION WITH FINGERPRINT ROTATION
    # ═══════════════════════════════════════════════════════════

    async def connect(self) -> bool:
        """
        Open browser with a fresh fingerprint.
        This maintains 100% backward compatibility.
        """
        from playwright.async_api import async_playwright

        self._log.info("Launching browser with fresh fingerprint...")
        self._sess_path.mkdir(parents=True, exist_ok=True)
        self._ss_path.mkdir(exist_ok=True)

        # ── Generate fresh fingerprint ──────────────────────────
        self._current_fingerprint = self._fingerprint_manager.get_fresh_fingerprint()
        self._log.info(f"Using fingerprint: {self._current_fingerprint.fingerprint_hash[:8]}")

        # ── Launch browser ──────────────────────────────────────
        self._pw = await async_playwright().start()
        
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._sess_path),
            headless=False,
            viewport={
                "width": self._current_fingerprint.screen_width,
                "height": self._current_fingerprint.screen_height
            },
            locale=self._current_fingerprint.language,
            timezone_id=self._current_fingerprint.timezone,
            user_agent=self._current_fingerprint.user_agent,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-sync",
                "--disable-default-apps",
                "--disable-translate",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
                "--disable-background-networking",
                "--safebrowsing-disable-auto-update",
                "--disable-client-side-phishing-detection",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--no-default-browser-check",
                "--no-first-run",
                "--password-store=basic",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-crash-reporter",
                "--disable-device-discovery-notifications",
                "--disable-notifications",
                "--disable-renderer-backgrounding",
                "--disable-software-rasterizer",
            ],
        )

        self._page = (
            self._ctx.pages[0]
            if self._ctx.pages
            else await self._ctx.new_page()
        )

        # ── Inject fingerprint script ──────────────────────────
        stealth_script = self._fingerprint_manager.generate_stealth_script(
            self._current_fingerprint
        )
        await self._page.add_init_script(stealth_script)

        # ── Navigate with human timing ─────────────────────────
        await asyncio.sleep(random.uniform(0.5, 2.0))
        
        self._log.info("Loading WhatsApp Web...")
        await self._page.goto(
            "https://web.whatsapp.com",
            wait_until="domcontentloaded",
            timeout=30000
        )

        # ── Human behavior after load ──────────────────────────
        await self._human_idle(2.0, 5.0)

        # ── Wait for QR or chat list ───────────────────────────
        try:
            await self._page.wait_for_selector(
                f"{self.SEL['chat_list']}, {self.SEL['qr_code']}",
                timeout=self._TIMEOUTS["connect"]
            )

            chat_list = await self._page.query_selector(self.SEL["chat_list"])
            if chat_list and await chat_list.is_visible():
                self._log.info("✅ Session loaded. WhatsApp Web is ready.")
                self._is_initialized = True
                self._last_state = "chat_list"
                await self._simulate_human_after_load()
                return True

            # QR code flow
            print("\n" + "=" * 55)
            print("  SCAN QR CODE TO LOG IN")
            print("=" * 55)
            print("  1. Open WhatsApp on your phone")
            print("  2. Menu (⋮) → Linked Devices → Link a Device")
            print("  3. Scan the QR code in the browser window")
            print("  You have 2 minutes.")
            print("=" * 55 + "\n")

            await self._page.wait_for_selector(
                self.SEL["chat_list"],
                timeout=self._TIMEOUTS["qr_scan"]
            )
            self._log.info("✅ QR scanned. Session saved.")
            self._is_initialized = True
            self._last_state = "chat_list"
            await self._simulate_human_after_login()
            return True

        except Exception as e:
            self._log.error(f"Connection failed: {e}")
            await self._cleanup_browser()
            raise ConnectionError(f"Could not connect to WhatsApp Web: {e}")

    async def rotate_fingerprint(self) -> bool:
        """
        Rotate to a new fingerprint and reconnect.
        Call this if WhatsApp detects you.
        
        Returns:
            bool: True if rotation was successful
        """
        self._log.info("🔄 Rotating fingerprint...")
        
        try:
            # Disconnect current session
            await self.disconnect()
            
            # Generate new fingerprint (force new)
            self._current_fingerprint = self._fingerprint_manager.get_fresh_fingerprint()
            
            self._log.info(f"✅ New fingerprint: {self._current_fingerprint.fingerprint_hash[:8]}")
            
            # Small delay before reconnecting
            await asyncio.sleep(random.uniform(2.0, 5.0))
            
            # Reconnect with new fingerprint
            return await self.connect()
            
        except Exception as e:
            self._log.error(f"Fingerprint rotation failed: {e}")
            return False

    async def disconnect(self) -> None:
        """Close browser cleanly with proper resource cleanup."""
        try:
            if self._page:
                try:
                    await self._page.close()
                except Exception as e:
                    self._log.debug(f"Page close warning: {e}")
                self._page = None
                
            if self._ctx:
                try:
                    await asyncio.wait_for(self._ctx.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._log.warning("Context close timeout - forcing cleanup")
                    try:
                        await self._ctx.stop()
                    except Exception:
                        pass
                except Exception as e:
                    self._log.debug(f"Context close warning: {e}")
                self._ctx = None
                
            if self._pw:
                try:
                    await asyncio.wait_for(self._pw.stop(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._log.warning("Playwright stop timeout - forcing cleanup")
                except Exception as e:
                    self._log.debug(f"Playwright stop warning: {e}")
                self._pw = None
                
            self._is_initialized = False
            self._log.info("Browser closed cleanly.")
            
        except Exception as e:
            self._log.error(f"Disconnect error: {e}")
        finally:
            import gc
            gc.collect()

    async def is_connected(self) -> bool:
        """True if WhatsApp chat list is visible."""
        if not self._page or not self._is_initialized:
            return False
        try:
            return await self._wait_for_selector_with_retry(
                self.SEL["chat_list"], timeout=2_000
            )
        except Exception:
            return False

    # ═══════════════════════════════════════════════════════════
    # HUMAN BEHAVIOR SIMULATION
    # ═══════════════════════════════════════════════════════════

    async def _simulate_human_after_load(self):
        """Simulate human behavior after WhatsApp loads."""
        self._log.debug("Simulating human behavior after loading...")
        
        await self._human_idle(3.0, 8.0)
        
        for _ in range(random.randint(2, 5)):
            await self._mouse_human_to(
                random.randint(100, 1100),
                random.randint(100, 700)
            )
            await self._human_pause(0.5, 2.0)
        
        for _ in range(random.randint(1, 3)):
            if random.random() < 0.4:
                await self._scroll_natural("down", random.randint(200, 500))
                await self._human_pause(0.5, 1.5)
        
        if random.random() < 0.3:
            await self._page.mouse.move(
                random.randint(900, 1100),
                random.randint(50, 150)
            )
            await self._human_pause(0.5, 1.5)
        
        if random.random() < 0.2:
            await self._simulate_tab_switching()
        
        await self._human_idle(1.0, 4.0)
        self._log.debug("Human behavior simulation complete.")

    async def _simulate_human_after_login(self):
        """Simulate human behavior after QR scan."""
        await self._simulate_human_after_load()
        await self._human_pause(2.0, 5.0)
        
        try:
            unread = await self._page.query_selector('span[data-icon="unread-count"]')
            if unread:
                await self._human_pause(1.0, 3.0)
        except Exception:
            pass

    async def _human_idle(self, min_sec: float = 1.0, max_sec: float = 5.0):
        """Idle with occasional mouse movement."""
        total_time = random.uniform(min_sec, max_sec)
        elapsed = 0
        
        while elapsed < total_time:
            if random.random() < 0.1:
                await self._mouse_human_to(
                    random.randint(100, 1100),
                    random.randint(100, 700)
                )
            
            wait_time = random.uniform(0.5, 2.0)
            await asyncio.sleep(min(wait_time, total_time - elapsed))
            elapsed += wait_time

    async def _simulate_natural_hesitation(self):
        """Simulate human hesitation before actions."""
        if random.random() < 0.3:
            await self._human_pause(0.5, 2.0)
            await self._mouse_human_to(
                random.randint(800, 1200),
                random.randint(100, 400)
            )
            await self._human_pause(0.3, 1.0)
            await self._mouse_human_to(
                random.randint(300, 900),
                random.randint(300, 500)
            )
            await self._human_pause(0.2, 0.8)

    # ═══════════════════════════════════════════════════════════
    # STATE VALIDATION
    # ═══════════════════════════════════════════════════════════

    async def _ensure_page_state(self, expected_state: str = "chat_list") -> bool:
        """Validate that the page is in the expected state."""
        if not self._page or not self._is_initialized:
            return False
        try:
            selector = self.SEL["chat_list"] if expected_state == "chat_list" else self.MSG_INPUT_SELS[0]
            return await self._wait_for_selector_with_retry(selector, timeout=3_000)
        except Exception:
            return False

    async def _wait_for_selector_with_retry(self, selector: str, timeout: int = 5_000, retries: int = 2) -> bool:
        """Wait for selector with retry logic."""
        for attempt in range(retries + 1):
            try:
                element = await self._page.wait_for_selector(
                    selector, timeout=timeout // (attempt + 1)
                )
                if element and await element.is_visible():
                    return True
            except Exception:
                if attempt == retries:
                    return False
                await asyncio.sleep(0.5 * (attempt + 1))
        return False

    async def _cleanup_browser(self) -> None:
        """Clean up browser resources."""
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        finally:
            self._pw = None
            self._ctx = None
            self._page = None
            self._is_initialized = False

    # ═══════════════════════════════════════════════════════════
    # BASIC HUMAN BEHAVIOR HELPERS
    # ═══════════════════════════════════════════════════════════

    async def _human_pause(self, min_sec: float = 0.5, max_sec: float = 2.0) -> None:
        """Random pause to simulate human behavior."""
        pause = random.uniform(min_sec, max_sec)
        await asyncio.sleep(pause)

    async def _simulate_reading(self, message: str) -> None:
        """Simulate reading a message before responding."""
        words = len(message.split())
        read_time = max(0.5, (words / 250) * 60)
        await self._human_pause(read_time * 0.8, read_time * 1.2)

    async def _simulate_tab_switching(self) -> None:
        """Simulate switching to other tabs like a human."""
        if random.random() < 0.08:
            await self._human_pause(1.0, 4.0)
            await self._page.keyboard.press("Control+Tab")
            await self._human_pause(0.5, 2.0)
            await self._page.keyboard.press("Control+Tab")
            await self._human_pause(0.3, 1.0)

    # FIXED: Added delta_x=0 parameter
    async def _scroll_natural(self, direction: str = "down", distance: int = None) -> None:
        """Natural scrolling with random speed and distance."""
        if distance is None:
            distance = random.randint(150, 600)
        steps = random.randint(3, 10)
        step_distance = distance // steps
        for _ in range(steps):
            speed = random.uniform(0.1, 0.3)
            if direction == "down":
                await self._page.mouse.wheel(delta_x=0, delta_y=step_distance)
            else:
                await self._page.mouse.wheel(delta_x=0, delta_y=-step_distance)
            await asyncio.sleep(speed)
        await self._human_pause(0.1, 0.3)

    async def _mouse_human_to(self, tx: int, ty: int) -> None:
        """Enhanced human-like mouse movement with natural curve."""
        waypoints = random.randint(2, 4)
        for _ in range(waypoints):
            mx = max(50, min(1230, tx + random.randint(-200, 200)))
            my = max(50, min(750, ty + random.randint(-150, 150)))
            await self._page.mouse.move(mx, my)
            await self._human_pause(0.2, 0.8)
        await self._page.mouse.move(tx, ty)
        await self._human_pause(0.05, 0.15)

    def _reset_daily_count_if_needed(self) -> None:
        """Reset daily message count if it's a new day."""
        today = datetime.now().date()
        if self._session_activity["last_daily_reset"] != today:
            self._session_activity["daily_count"] = 0
            self._session_activity["last_daily_reset"] = today
            self._log.debug("Daily message count reset.")

    async def _manage_session_activity(self) -> bool:
        """Track and manage session activity to avoid patterns."""
        self._reset_daily_count_if_needed()
        
        if self._session_activity["daily_count"] >= self._HUMAN_BEHAVIOR["daily_message_limit"]:
            self._log.warning(f"⚠ Daily message limit reached: {self._session_activity['daily_count']}")
            return False
        
        now = datetime.now()
        active_time = (now - self._session_activity["session_start"]).total_seconds()
        
        if active_time > random.randint(
            self._HUMAN_BEHAVIOR["session_break_after"] - 300,
            self._HUMAN_BEHAVIOR["session_break_after"] + 300
        ):
            break_time = random.randint(
                self._HUMAN_BEHAVIOR["session_break_duration"],
                self._HUMAN_BEHAVIOR["session_break_duration"] * 2
            )
            self._log.info(f"🌴 Taking natural break: {break_time//60} minutes")
            await asyncio.sleep(break_time)
            self._session_activity["session_start"] = now
            self._session_activity["active_time"] = 0
        
        if self._session_activity["messages_sent"] > 0:
            wait_time = random.uniform(
                self._HUMAN_BEHAVIOR["inter_message_delay_min"],
                self._HUMAN_BEHAVIOR["inter_message_delay_max"]
            )
            self._log.debug(f"Natural pause between messages: {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
        
        self._session_activity["messages_sent"] += 1
        self._session_activity["daily_count"] += 1
        self._session_activity["last_activity"] = now
        return True

    # ═══════════════════════════════════════════════════════════
    # CHAT NAVIGATION (NO PAGE.GOTO)
    # ═══════════════════════════════════════════════════════════

    async def _return_to_chat_list(self) -> bool:
        """Return to chat list using ESC key (SPA-native)."""
        try:
            if await self._ensure_page_state("chat_list"):
                self._last_state = "chat_list"
                return True

            for attempt in range(3):
                await self._page.keyboard.press("Escape")
                await self._human_pause(0.3, 0.6)
                if await self._ensure_page_state("chat_list"):
                    self._last_state = "chat_list"
                    self._log.debug("Returned to chat list via ESC.")
                    return True

            for back_sel in [
                'div[data-testid="chat-back-button"]',
                'button[aria-label="Back"]',
                'span[data-icon="back"]'
            ]:
                try:
                    back_el = await self._page.query_selector(back_sel)
                    if back_el and await back_el.is_visible():
                        await back_el.click()
                        await self._human_pause(0.3, 0.7)
                        if await self._ensure_page_state("chat_list"):
                            self._last_state = "chat_list"
                            self._log.debug("Returned to chat list via back button.")
                            return True
                except Exception:
                    continue

            self._log.warning("Could not return to chat list.")
            return False
        except Exception as e:
            self._log.debug(f"_return_to_chat_list error: {e}")
            return False

    async def _focus_search_bar(self) -> bool:
        """Focus the search bar."""
        try:
            search_el = await self._page.wait_for_selector(
                self.SEARCH_SEL, timeout=self._TIMEOUTS["search_bar"]
            )
            if not search_el or not await search_el.is_visible():
                return False

            box = await search_el.bounding_box()
            if box:
                cx = int(box["x"] + box["width"] / 2)
                cy = int(box["y"] + box["height"] / 2)
                await self._mouse_human_to(cx, cy)

            await search_el.click()
            await self._human_pause(0.2, 0.5)
            return True
        except Exception:
            return False

    async def _type_phone_number(self, phone: str) -> None:
        """Type phone number with human-like chunking."""
        await self._page.keyboard.press("Control+a")
        await self._human_pause(0.05, 0.15)
        await self._page.keyboard.press("Delete")
        await self._human_pause(0.1, 0.3)

        chunks = [phone[:3], phone[3:6], phone[6:9], phone[9:]]
        for i, chunk in enumerate(chunks):
            if chunk:
                for digit in chunk:
                    delay = random.uniform(80, 350)
                    await self._page.keyboard.type(digit, delay=delay)
                if i < len(chunks) - 1 and chunks[i + 1]:
                    await self._human_pause(0.1, 0.3)

    async def _click_search_result(self, phone: str) -> bool:
        """Click the search result for the given phone number."""
        await self._human_pause(1.0, 2.5)
        await self._human_pause(0.2, 0.6)

        result_sels = [
            f'span[title="+{phone}"]',
            f'span[title="{phone}"]',
            f'span[title*="{phone[-10:]}"]',
            'div[data-testid="cell-frame-container"]:first-child',
            'div[data-testid="list-item-0"]',
            'div[role="option"]:first-child',
            'div[role="listitem"]:first-child',
        ]

        for sel in result_sels:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    box = await el.bounding_box()
                    if box:
                        cx = int(box["x"] + box["width"] / 2)
                        cy = int(box["y"] + box["height"] / 2)
                        await self._mouse_human_to(cx, cy)
                    await el.click()
                    self._log.debug(f"Result clicked: {sel}")
                    return True
            except Exception:
                continue

        await self._page.keyboard.press("Enter")
        self._log.debug("Pressed Enter to open top result")
        return True

    async def _open_chat(self, phone: str) -> Tuple[bool, str, str]:
        """Open a chat using search bar. No page.goto()."""
        if not await self._return_to_chat_list():
            return False, "", "Failed to return to chat list"
        
        await self._human_pause(0.8, 1.8)

        if not await self._focus_search_bar():
            return False, "", "Search bar not found"

        await self._type_phone_number(phone)

        if not await self._click_search_result(phone):
            return False, "", "Failed to click search result"

        await self._human_pause(0.8, 1.5)
        await self._handle_new_chat_modal()
        await self._human_pause(0.5, 1.0)

        try:
            msg_sel = await self._find_msg_input(timeout_ms=self._TIMEOUTS["msg_input"])
            self._last_state = "chat_open"
            return True, msg_sel, ""
        except TimeoutError as e:
            if await self._check_invalid_number():
                return False, "", "INVALID_NUMBER"
            if await self._check_account_restricted():
                return False, "", "RESTRICTED"
            await self._save_debug_screenshot()
            return False, "", str(e)

    async def _handle_new_chat_modal(self) -> None:
        """Handle the new chat modal if it appears."""
        await self._human_pause(0.4, 0.8)
        for sel in [
            'div[data-animate-modal-body="true"] button',
            'div[role="dialog"] button',
            'div[data-testid="popup-contents"] button',
        ]:
            try:
                modal_el = await self._page.query_selector(sel)
                if modal_el and await modal_el.is_visible():
                    btn_text = (await modal_el.inner_text()).lower().strip()
                    if any(w in btn_text for w in ["message", "chat", "ok", "open", "start"]):
                        await self._human_pause(0.2, 0.5)
                        await modal_el.click()
                        self._log.debug(f"Modal: '{btn_text}'")
                        await self._human_pause(0.5, 1.0)
                        break
            except Exception:
                continue

    async def _save_debug_screenshot(self) -> None:
        """Save a debug screenshot."""
        try:
            debug = self._ss_path / f"debug_{datetime.now().strftime('%H%M%S')}.png"
            await self._page.screenshot(path=str(debug))
            self._log.warning(f"Debug screenshot: {debug}")
        except Exception:
            pass

    async def _find_msg_input(self, timeout_ms: int = 15_000) -> str:
        """Find the message compose box."""
        start = asyncio.get_event_loop().time()
        deadline = start + (timeout_ms / 1000)

        while asyncio.get_event_loop().time() < deadline:
            for sel in self.MSG_INPUT_SELS:
                try:
                    if await self._wait_for_selector_with_retry(sel, timeout=500):
                        self._log.debug(f"Message input: {sel}")
                        return sel
                except Exception:
                    continue
            await asyncio.sleep(0.5)

        raise TimeoutError(f"Message input not found after {timeout_ms}ms.")

    # ═══════════════════════════════════════════════════════════
    # ENHANCED TYPING WITH HUMAN BEHAVIOR
    # ═══════════════════════════════════════════════════════════

    async def _type_human_with_mistakes(self, selector: str, message: str) -> None:
        """Enhanced typing with more human-like behavior."""
        try:
            await self._page.click(selector)
            await self._human_pause(0.2, 0.6)
        except Exception:
            pass
        
        words = message.split()
        for i, word in enumerate(words):
            for char in word:
                await self._type_char_natural(char)
            
            if i < len(words) - 1:
                await self._human_pause(0.05, 0.15)
            
            if random.random() < 0.08:
                await self._human_pause(0.3, 1.2)
            
            if random.random() < 0.05 and len(word) > 3:
                await self._page.keyboard.press("Backspace")
                await self._human_pause(0.1, 0.3)
                await self._type_char_natural(word[-1])
        
        await self._human_pause(0.3, 1.0)

    async def _type_char_natural(self, char: str) -> None:
        """Type a character with natural variations including mistakes."""
        mistake_rate = 0.025
        delay = random.uniform(80, 450)
        
        if random.random() < mistake_rate and char != " " and char.isalpha():
            wrong_char = random.choice("abcdefghijklmnopqrstuvwxyz")
            await self._page.keyboard.type(wrong_char, delay=delay)
            await self._human_pause(0.08, 0.25)
            await self._page.keyboard.press("Backspace")
            await self._human_pause(0.05, 0.15)
        
        await self._page.keyboard.type(char, delay=delay)
        if char == " ":
            await self._human_pause(0.05, 0.15)

    async def _inject_emoji(self, selector: str, emoji: str) -> None:
        """Inject emoji using JavaScript."""
        try:
            await self._page.evaluate(
                """(args) => {
                    const el = document.querySelector(args.selector);
                    if (!el) return;
                    const sel = window.getSelection();
                    if (!sel || !sel.rangeCount) return;
                    const range = sel.getRangeAt(0);
                    const node = document.createTextNode(args.emoji);
                    range.insertNode(node);
                    range.setStartAfter(node);
                    range.setEndAfter(node);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }""",
                {"selector": selector, "emoji": emoji}
            )
            await self._human_pause(0.05, 0.15)
        except Exception:
            await self._page.keyboard.type(emoji)

    # ═══════════════════════════════════════════════════════════
    # CHECKS AND HELPERS
    # ═══════════════════════════════════════════════════════════

    async def _check_invalid_number(self) -> bool:
        """True if WhatsApp shows 'number not registered'."""
        try:
            body = (await self._page.inner_text("body")).lower()
            return any(p in body for p in self.INVALID_TEXTS)
        except Exception:
            return False

    async def _check_account_restricted(self) -> bool:
        """True if WhatsApp shows restriction banner."""
        try:
            body = (await self._page.inner_text("body")).lower()
            signals = [
                "linked devices is restricted",
                "can't start new chats right now",
                "account on linked devices is restricted",
            ]
            return any(s in body for s in signals)
        except Exception:
            return False

    async def _check_campaign_in_history(self, history_depth: int = 4) -> Tuple[bool, str]:
        """Read last N outgoing messages for campaign keywords."""
        await self._human_pause(0.8, 1.5)
        outgoing = []
        for sel in [self.SEL["outgoing_msgs"], self.SEL["outgoing_alt"]]:
            try:
                els = await self._page.query_selector_all(sel)
                if els:
                    for el in els[-history_depth:]:
                        try:
                            t = (await el.inner_text()).strip()
                            if t:
                                outgoing.append(t)
                        except Exception:
                            continue
                if outgoing:
                    break
            except Exception:
                continue

        if not outgoing:
            return False, ""

        for msg in outgoing:
            msg_l = msg.lower()
            for kw in self.CAMPAIGN_KEYWORDS:
                if kw.lower() in msg_l:
                    preview = msg[:80] + "..." if len(msg) > 80 else msg
                    self._log.info(f"⚠ Campaign keyword '{kw}' in history")
                    return True, preview
        return False, ""

    async def _take_screenshot(self, order_id: str) -> str:
        """Save screenshot to screenshots/."""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_id = re.sub(r"[^\w\-]", "_", str(order_id))
            path = self._ss_path / f"{safe_id}_{ts}.png"
            await self._page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception as e:
            self._log.warning(f"Screenshot failed: {e}")
            return ""

    async def _wait_for_delivery_tick(self) -> None:
        """Wait for delivery tick with human-like patience."""
        try:
            await self._human_pause(0.5, 2.0)
            tick_found = await self._page.query_selector(self.SEL["sent_tick"])
            if tick_found:
                self._log.debug("✓ Delivery confirmed.")
                return

            for attempt in range(3):
                await self._human_pause(1.0, 3.0)
                tick_found = await self._page.query_selector(self.SEL["sent_tick"])
                if tick_found:
                    self._log.debug("✓ Delivery confirmed.")
                    return

            self._log.warning("⚠ Tick timeout — likely delivered")
        except Exception:
            self._log.warning("⚠ Tick timeout — likely delivered")

    def _handle_open_chat_error(self, error: str, phone: str) -> SendResult:
        """Handle errors from _open_chat."""
        if error == "INVALID_NUMBER":
            self._log.info(f"✗ Not on WhatsApp: +{phone}")
            return SendResult(
                success=False,
                status="INVALID_NUMBER",
                error_message="Phone not registered on WhatsApp"
            )
        if error == "RESTRICTED":
            self._log.warning("Account restricted — stopping.")
            return SendResult(
                success=False,
                status="FAILED",
                error_message="Account temporarily restricted. Wait 24-48 hours."
            )
        return SendResult(
            success=False,
            status="FAILED",
            error_message=error
        )

    # ═══════════════════════════════════════════════════════════
    # MAIN SEND METHODS (BACKWARD COMPATIBLE)
    # ═══════════════════════════════════════════════════════════

    async def send_text(self, phone: str, message: str, order_id: str) -> SendResult:
        """Send text message with full human emulation."""
        self._log.info(f"→ Sending to +{phone} [Order: {order_id}]")

        try:
            if not await self._manage_session_activity():
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message="Daily message limit reached. Try again tomorrow."
                )

            await self._simulate_tab_switching()
            
            if random.random() < 0.2:
                await self._scroll_natural("down", random.randint(100, 300))
                await self._human_pause(0.5, 1.5)

            success, msg_sel, error = await self._open_chat(phone)
            if not success:
                return self._handle_open_chat_error(error, phone)

            if await self._check_account_restricted():
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message="Account temporarily restricted."
                )

            if await self._check_invalid_number():
                self._log.info(f"✗ Not on WhatsApp: +{phone}")
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            if random.random() < 0.3:
                await self._scroll_natural("down", random.randint(100, 400))
                await self._human_pause(0.5, 1.5)
                await self._scroll_natural("up", random.randint(100, 400))
                await self._human_pause(0.5, 1.0)

            already, preview = await self._check_campaign_in_history(4)
            if already:
                self._log.info("↩ Skipped — campaign in history")
                return SendResult(
                    success=False,
                    status="ALREADY_CONTACTED",
                    error_message=f"Previous campaign: '{preview}'"
                )

            await self._simulate_reading(message)
            await self._simulate_natural_hesitation()
            await self._type_human_with_mistakes(msg_sel, message)

            if random.random() < 0.2:
                await self._human_pause(0.5, 1.5)
                await self._page.mouse.move(
                    random.randint(300, 800),
                    random.randint(300, 500)
                )
                await self._human_pause(0.5, 1.0)

            if random.random() < 0.4:
                await self._human_pause(0.5, 2.0)
                await self._mouse_human_to(
                    random.randint(600, 1100),
                    random.randint(200, 500)
                )
                await self._human_pause(0.3, 0.8)

            await self._page.keyboard.press("Enter")
            self._log.debug("Sent. Waiting for tick...")
            await self._wait_for_delivery_tick()

            await self._human_pause(0.5, 1.5)
            if random.random() < 0.2:
                await self._scroll_natural("up", random.randint(50, 150))
                await self._human_pause(0.3, 0.8)
                await self._scroll_natural("down", random.randint(50, 150))

            if random.random() < 0.15:
                await self._human_pause(2.0, 5.0)
                await self._return_to_chat_list()
                await self._human_pause(1.0, 3.0)

            ss = await self._take_screenshot(order_id)
            self._sends_this_session += 1

            return SendResult(
                success=True,
                status="SENT",
                screenshot_path=ss
            )

        except Exception as e:
            self._log.error(f"✗ Failed +{phone}: {e}", exc_info=True)
            return SendResult(
                success=False,
                status="FAILED",
                error_message=str(e)
            )

    async def send_image(
        self,
        phone: str,
        image_path: str,
        caption: str,
        order_id: str
    ) -> SendResult:
        """Send image with caption."""
        self._log.info(f"→ Sending image to +{phone} [{order_id}]")

        try:
            if not await self._manage_session_activity():
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message="Daily message limit reached."
                )

            await self._simulate_tab_switching()

            success, msg_sel, error = await self._open_chat(phone)
            if not success:
                return self._handle_open_chat_error(error, phone)

            if await self._check_invalid_number():
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            already, preview = await self._check_campaign_in_history(4)
            if already:
                return SendResult(
                    success=False,
                    status="ALREADY_CONTACTED",
                    error_message=f"Previous campaign: '{preview}'"
                )

            await self._page.click(self.SEL["attach_btn"])
            await self._human_pause(0.5, 1.0)

            async with self._page.expect_file_chooser() as fc_info:
                await self._page.click('input[accept*="image"]')
            fc = await fc_info.value
            await fc.set_files(image_path)

            await self._page.wait_for_selector(
                'div[data-testid="media-editor"]',
                timeout=self._TIMEOUTS["media_editor"]
            )
            await self._human_pause(0.8, 1.5)

            if caption:
                try:
                    await self._page.click(self.SEL["caption_input"])
                    await self._type_human_with_mistakes(self.SEL["caption_input"], caption)
                except Exception:
                    self._log.warning("Caption input not found")

            await self._page.keyboard.press("Enter")
            await self._human_pause(1.0, 2.0)

            ss = await self._take_screenshot(order_id)
            self._sends_this_session += 1

            return SendResult(success=True, status="SENT", screenshot_path=ss)

        except Exception as e:
            self._log.error(f"✗ Image failed: {e}", exc_info=True)
            return SendResult(
                success=False,
                status="FAILED",
                error_message=str(e)
            )

    async def send_file_to_number(
        self,
        phone: str,
        file_path: str,
        caption: str,
        order_id: str
    ) -> SendResult:
        """Send file as document attachment."""
        self._log.info(f"→ Sending file to +{phone} [{order_id}]")

        if not Path(file_path).exists():
            return SendResult(
                success=False,
                status="FAILED",
                error_message=f"File not found: {file_path}"
            )

        try:
            if not await self._manage_session_activity():
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message="Daily message limit reached."
                )

            success, _, error = await self._open_chat(phone)
            if not success:
                return self._handle_open_chat_error(error, phone)

            await self._human_pause(1.0, 2.0)

            await self._page.click(self.SEL["attach_btn"])
            await self._human_pause(0.8, 1.5)

            try:
                doc = await self._page.query_selector(
                    'li[data-testid="mi-attach-document"], span[data-icon="attach-document"]'
                )
                if doc:
                    await doc.click()
                    await self._human_pause(0.3, 0.7)
            except Exception:
                pass

            try:
                async with self._page.expect_file_chooser(
                    timeout=self._TIMEOUTS["file_upload"]
                ) as fc_info:
                    inputs = await self._page.query_selector_all('input[type="file"]')
                    if inputs:
                        await inputs[-1].click()
                    else:
                        await self._page.keyboard.press("Enter")
                fc = await fc_info.value
                await fc.set_files(file_path)
            except Exception as e:
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message=f"File upload failed: {e}"
                )

            await self._human_pause(1.5, 2.5)

            if caption:
                for sel in [
                    'div[aria-label="Add a caption"]',
                    'div[aria-label="Type a message"]',
                ]:
                    try:
                        el = await self._page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.click()
                            await self._type_human_with_mistakes(sel, caption)
                            break
                    except Exception:
                        continue

            await self._human_pause(0.5, 1.0)
            await self._page.keyboard.press("Enter")

            await self._wait_for_delivery_tick()

            ss = await self._take_screenshot(order_id)
            self._sends_this_session += 1

            return SendResult(success=True, status="SENT", screenshot_path=ss)

        except Exception as e:
            self._log.error(f"✗ File failed: {e}", exc_info=True)
            return SendResult(
                success=False,
                status="FAILED",
                error_message=str(e)
            )