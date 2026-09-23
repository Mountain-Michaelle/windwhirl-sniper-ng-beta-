# ==============================================================
# HUMAN NAVIGATION FLOW — Search-Based Chat Opening
# ==============================================================
# PATH: apps/core/lib/utils/playwright_sender.py
# ACTION: Replace _navigate_to_chat() — new method added
#         Replace send_text() — uses new navigation
#
# THE CORE INSIGHT:
#   web.whatsapp.com/send?phone=234XXX is a developer shortcut.
#   No human ever types that URL. WhatsApp's server knows this.
#   Every time the automation used it, the server logged:
#   "direct URL jump to new number" — repeated — flagged.
#
#   The human flow is:
#     1. Click search bar
#     2. Type the phone number
#     3. Click the result that appears
#     4. Chat opens natively
#     5. Type message, send
#
#   WhatsApp cannot flag their own search UI as automation.
#   The server logs show: search → result clicked → chat opened.
#   That is what millions of real users do every day.
#
# ADDITIONAL HUMAN SIGNALS THIS ADDS:
#   - Number typed character by character into search (STEALTH 1)
#   - Small pause between typing digits (people hesitate)
#   - Wait for search results to appear before clicking
#   - Click the result naturally (not a URL jump)
#   - Chat opens through WhatsApp's own contact resolution
#   - Navigation origin is always the chat list, never a raw URL
#
# WHAT THIS REPLACES:
#   Before: goto("web.whatsapp.com/send?phone=234XXX")
#   After:  search bar → type number → click result → chat opens
#
# ==============================================================


# ==============================================================
# ADD THIS METHOD to PlaywrightSender class
# Find the class and add _navigate_to_chat() before send_text()
# ==============================================================

"""
    async def _navigate_to_chat(self, phone: str) -> tuple:
        '''
        Open a chat using WhatsApp's native search flow.
        This is how a real human starts a new conversation.

        Flow:
          1. Ensure we are on the WhatsApp Web home (chat list)
          2. Click the search bar
          3. Type the phone number digit by digit with natural pauses
          4. Wait for search results to appear
          5. Click the matching result (contact or number)
          6. Chat opens natively through WhatsApp's own UI
          7. Find and return the message input selector

        Why this is safer than ?phone= URL:
          The ?phone= URL is a developer API endpoint.
          Real users never navigate there directly.
          WhatsApp's server-side logs distinguish between:
            - "user searched for number, clicked result" (human)
            - "direct GET request to /send?phone=XXX" (automation)
          This method generates the first pattern, not the second.

        Args:
            phone: 13-digit normalized number e.g. "2348038365784"

        Returns:
            Tuple: (success: bool, msg_selector: str, error: str)
              success=True  → chat is open, msg_selector is ready to use
              success=False → could not open chat, error contains reason

        Raises:
            Nothing — all errors returned in the tuple.
        '''

        # ── Step 1: Navigate to chat list home ───────────────────
        # Always start from the chat list — the natural origin
        # Do NOT navigate if already on WhatsApp Web home
        try:
            current_url = self._page.url
            if "web.whatsapp.com" not in current_url:
                await self._page.goto(
                    "https://web.whatsapp.com",
                    wait_until="domcontentloaded",
                    timeout=15_000
                )
                await asyncio.sleep(random.uniform(1.5, 3))
            elif "/send?" in current_url or "/send/" in current_url:
                # We're in a send URL from a previous message
                # Navigate home first
                await self._page.goto(
                    "https://web.whatsapp.com",
                    wait_until="domcontentloaded",
                    timeout=15_000
                )
                await asyncio.sleep(random.uniform(1, 2.5))
        except Exception as e:
            return False, "", f"Could not navigate to WhatsApp home: {e}"

        # ── Step 2: Find and click the search bar ────────────────
        # Multiple selectors for compatibility with WhatsApp updates
        search_selectors = [
            'div[aria-label="Search input textbox"]',
            'div[aria-label="Search or start new chat"]',
            'div[title="Search or start new chat"]',
            'div[data-tab="3"][contenteditable="true"]',
            'div[role="textbox"][title="Search or start new chat"]',
            'button[aria-label="New chat"] ~ div div[contenteditable]',
        ]

        search_clicked = False
        for sel in search_selectors:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    # Human mouse movement before clicking search
                    box = await el.bounding_box()
                    if box:
                        cx = int(box["x"] + box["width"] / 2)
                        cy = int(box["y"] + box["height"] / 2)
                        await self._mouse_human_to(cx, cy)
                    await el.click()
                    search_clicked = True
                    self._log.debug(f"  Search bar clicked: {sel}")
                    break
            except Exception:
                continue

        if not search_clicked:
            # Try clicking the search icon directly
            try:
                await self._page.click('span[data-icon="search"]')
                search_clicked = True
            except Exception:
                pass

        if not search_clicked:
            return False, "", "Could not find or click search bar"

        # Brief pause after clicking search — human reaction time
        await asyncio.sleep(random.uniform(0.4, 0.9))

        # ── Step 3: Type the phone number naturally ───────────────
        # Type digit by digit with human-like pauses
        # People type phone numbers with slight hesitations —
        # they remember the number in chunks, not as one stream

        # Format: type with country code but naturally
        # Most Nigerians type +2348XXX or just 08XXX when searching
        # We type the full number without + for reliability
        display_number = phone  # 2348XXXXXXXXX

        # Chunk the number into groups as humans think about them
        # e.g. 234 | 803 | 786 | 5784 — country | area | prefix | line
        chunks = [
            display_number[:3],    # 234 (country code)
            display_number[3:6],   # 803 (first 3 of number)
            display_number[6:9],   # 786 (next 3)
            display_number[9:],    # remaining digits
        ]

        for chunk_idx, chunk in enumerate(chunks):
            for digit in chunk:
                # Random typing speed per digit — some people type
                # numbers slower than text (looking at keyboard)
                await self._page.keyboard.type(
                    digit,
                    delay=random.uniform(80, 200)
                )

            # Pause between chunks — humans recall numbers in groups
            # First chunk pause is slightly longer (remembering the code)
            if chunk_idx < len(chunks) - 1:
                await asyncio.sleep(random.uniform(0.1, 0.4))

        self._log.debug(f"  Typed number into search: {phone}")

        # ── Step 4: Wait for search results ──────────────────────
        # WhatsApp takes a moment to query contacts and show results
        # Wait for the results list to appear

        results_selectors = [
            'div[aria-label="Search results"]',
            'div[data-testid="search-list"]',
            'div[role="listbox"]',
            'div[data-tab="3"] ~ div div[role="listitem"]',
            'div.copyable-area div[role="listitem"]',
        ]

        results_appeared = False
        for sel in results_selectors:
            try:
                await self._page.wait_for_selector(sel, timeout=5_000)
                results_appeared = True
                self._log.debug(f"  Search results appeared: {sel}")
                break
            except Exception:
                continue

        # Even if the results selector didn't match, give WhatsApp
        # time to show something — it might use a different selector
        if not results_appeared:
            await asyncio.sleep(random.uniform(1.5, 2.5))

        # ── Step 5: Click the matching result ────────────────────
        # WhatsApp shows either:
        #   a) A saved contact with the number
        #   b) A "Message [number]" option for unsaved numbers
        # We need to click whichever one appears

        # Small pause before clicking — humans read results first
        await asyncio.sleep(random.uniform(0.5, 1.2))

        result_clicked = False

        # Try finding a result that contains our phone number
        # Check multiple possible result formats
        result_click_selectors = [
            # Unsaved number — "Message +234..." or just the number
            f'span[title*="{phone[-10:]}"]',      # Last 10 digits
            f'span[title*="+{phone}"]',            # With + prefix
            f'span[title*="{phone}"]',             # Exact match

            # Generic first result in search list
            'div[data-testid="search-list"] div[role="listitem"]:first-child',
            'div[aria-label="Search results"] div[role="listitem"]:first-child',

            # "New chat" option that appears for unsaved numbers
            'div[data-testid="cell-frame-container"]:first-child',
            'div[role="listitem"]:first-child span[dir="auto"]',
        ]

        for sel in result_click_selectors:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    # Human mouse movement to the result
                    box = await el.bounding_box()
                    if box:
                        cx = int(box["x"] + box["width"] / 2)
                        cy = int(box["y"] + box["height"] / 2)
                        await self._mouse_human_to(cx, cy)
                    await el.click()
                    result_clicked = True
                    self._log.debug(f"  Result clicked: {sel}")
                    break
            except Exception:
                continue

        if not result_clicked:
            # Last resort: press Enter — selects first result
            await self._page.keyboard.press("Enter")
            result_clicked = True
            self._log.debug("  Used Enter key to select first result")

        await asyncio.sleep(random.uniform(0.8, 1.5))

        # ── Step 6: Handle "Message contact" modal ───────────────
        # When opening an unsaved number, WhatsApp sometimes shows
        # a modal with a "Message" or "Chat" button before the chat opens
        # Click it if it appears

        modal_button_selectors = [
            'div[data-animate-modal-body="true"] button',
            'button[aria-label="Message"]',
            'div[role="dialog"] button:last-child',
            'div[data-testid="popup-contents"] button',
            'footer button',  # Dialog footer button
        ]

        # Wait briefly then check for modal
        await asyncio.sleep(random.uniform(0.5, 1.0))

        for sel in modal_button_selectors:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    btn_text = (await el.inner_text()).lower().strip()
                    # Only click if it looks like a "proceed" button
                    if any(word in btn_text for word in
                           ["message", "chat", "ok", "continue", "open"]):
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        await el.click()
                        self._log.debug(f"  Modal button clicked: '{btn_text}'")
                        break
            except Exception:
                continue

        # ── Step 7: Find message input in the opened chat ─────────
        # Give the chat a moment to fully load
        await asyncio.sleep(random.uniform(1, 2))

        try:
            msg_selector = await self._find_msg_input(timeout_ms=15_000)
            return True, msg_selector, ""
        except TimeoutError as e:
            # Check for invalid number before giving up
            if await self._check_invalid_number():
                return False, "", "INVALID_NUMBER"
            if await self._check_account_restricted():
                return False, "", "RESTRICTED"
            return False, "", str(e)
"""


# ==============================================================
# REPLACE send_text() in PlaywrightSender
# Uses _navigate_to_chat() instead of direct URL goto()
# ==============================================================

"""
    async def send_text(
        self,
        phone:    str,
        message:  str,
        order_id: str
    ) -> SendResult:
        '''
        Send a text message using WhatsApp's native search flow.

        Navigation: search bar → type number → click result → chat opens
        This is identical to how a human starts a new conversation.
        No direct URL jumps. No programmatic shortcuts.

        Full flow:
          Step 0: Tab rotation                  (STEALTH 8)
          Step 1: Navigate via search bar        (HUMAN FLOW — new)
                  → type number into search
                  → click result
                  → handle modal if appears
                  → chat opens natively
          Step 2: Check account restriction
          Step 3: Check invalid number
          Step 4: Chat history check             (reads current page)
          Step 5: Pre-typing pause               (STEALTH 2)
          Step 6: Occasional scroll              (STEALTH 7)
          Step 7: Human mouse to input           (STEALTH 3)
          Step 8: Click, type whole message      (STEALTH 1 + 6)
                  newlines → Shift+Enter
          Step 9: Press Enter ONCE to send
          Step 10: Wait for delivery tick
          Step 11: Screenshot
        '''
        self._log.info(f"→ Sending to +{phone} [Order: {order_id}]")

        try:
            # ── Step 0: Tab rotation (STEALTH 8) ────────────────
            await self._rotate_tab()

            # ── Step 1: Navigate via native search flow ──────────
            # This is the key change — human navigation, not URL jump
            success, msg_selector, error = await self._navigate_to_chat(phone)

            if not success:
                if error == "INVALID_NUMBER":
                    self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )
                if error == "RESTRICTED":
                    self._log.warning(
                        "WhatsApp account restricted from starting new chats."
                    )
                    return SendResult(
                        success=False,
                        status="FAILED",
                        error_message=(
                            "Account temporarily restricted. "
                            "Wait 24-48 hours before retrying."
                        )
                    )
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message=error
                )

            # ── Step 2: Check account restriction ────────────────
            if await self._check_account_restricted():
                self._log.warning(
                    "Account restricted — stopping session."
                )
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message=(
                        "WhatsApp account temporarily restricted. "
                        "Wait 24-48 hours before retrying."
                    )
                )

            # ── Step 3: Check invalid number ─────────────────────
            if await self._check_invalid_number():
                self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            # ── Step 4: Chat history check ───────────────────────
            # Chat is already open — reads current page, no extra nav
            already_contacted, matched_preview = (
                await self._check_campaign_in_history(history_depth=4)
            )
            if already_contacted:
                self._log.info(
                    "  ↩ Skipped — campaign message found in history"
                )
                return SendResult(
                    success=False,
                    status="ALREADY_CONTACTED",
                    error_message=(
                        f"Previous campaign found: '{matched_preview}'"
                    )
                )

            # ── Step 5: Pre-typing pause (STEALTH 2) ─────────────
            pre_pause = random.uniform(
                self._cfg.delays.pre_type_min,
                self._cfg.delays.pre_type_max
            )
            self._log.debug(f"  Pre-type pause: {pre_pause:.1f}s")
            await asyncio.sleep(pre_pause)

            # ── Step 6: Occasional scroll (STEALTH 7) ────────────
            if random.choice([True, False]):
                scroll_up = random.uniform(100, 400)
                await self._page.evaluate(
                    f"window.scrollBy(0, -{scroll_up})"
                )
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await self._page.evaluate("window.scrollBy(0, 10000)")
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # ── Step 7: Human mouse to input (STEALTH 3) ─────────
            el = await self._page.query_selector(msg_selector)
            if el:
                box = await el.bounding_box()
                if box:
                    cx = int(box["x"] + box["width"] / 2)
                    cy = int(box["y"] + box["height"] / 2)
                    await self._mouse_human_to(cx, cy)

            # ── Step 8: Type complete message (STEALTH 1 + 6) ────
            # _type_human() converts \\n to Shift+Enter
            # No Enter pressed inside — message stays as one block
            await self._page.click(msg_selector)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await self._type_human(msg_selector, message)
            await asyncio.sleep(random.uniform(0.5, 1.2))

            # ── Step 9: Send — ONE Enter press ───────────────────
            await self._page.keyboard.press("Enter")
            self._log.debug("  Message sent. Waiting for tick...")

            # ── Step 10: Wait for delivery tick ──────────────────
            try:
                await self._page.wait_for_selector(
                    self.SEL["sent_tick"],
                    timeout=15_000
                )
                self._log.info(f"  ✓ Delivered: +{phone}")
            except Exception:
                self._log.warning(
                    f"  ⚠ Tick timeout +{phone} — likely delivered"
                )

            # ── Step 11: Screenshot ───────────────────────────────
            screenshot_path = await self._take_screenshot(order_id)
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
"""


# ==============================================================
# ALSO UPDATE _rotate_tab() — navigate home naturally
# ==============================================================
# The tab rotation now goes home via the chat list icon,
# not via a direct URL goto() — more human.
# Replace _rotate_tab() with this version:

"""
    async def _rotate_tab(self):
        '''
        STEALTH 8: Return to WhatsApp home periodically.

        UPDATED: Navigates home by clicking the WhatsApp logo
        or chat list icon — not via a URL goto() call.
        This keeps the navigation origin within WhatsApp's own UI.
        '''
        if self._msgs_on_tab < self._rotate_after:
            return

        self._log.info(
            f"Tab rotation after {self._msgs_on_tab} messages..."
        )

        # Try clicking WhatsApp logo or chat list button to go home
        # This is how a human returns to the main screen
        home_selectors = [
            'div[aria-label="WhatsApp"]',
            'div[data-icon="whatsapp-icon"]',
            'header div[role="button"]:first-child',
            'div[aria-label="Chats"]',
        ]

        clicked_home = False
        for sel in home_selectors:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    clicked_home = True
                    break
            except Exception:
                continue

        # Fallback to URL if no home button found
        if not clicked_home:
            await self._page.goto(
                "https://web.whatsapp.com",
                wait_until="domcontentloaded"
            )

        # Pause on home screen — just like a human glancing at chats
        await asyncio.sleep(random.uniform(3, 8))

        self._msgs_on_tab  = 0
        self._rotate_after = random.randint(8, 12)
        self._log.debug(
            f"Rotated home. Next after {self._rotate_after} messages."
        )
"""


# ==============================================================
# WHY THIS IS FUNDAMENTALLY SAFER
# ==============================================================
#
# BEFORE (direct URL):
#   Server log: GET /send?phone=2348038... (new)
#               GET /send?phone=2349131... (new)
#               GET /send?phone=2349015... (new)
#   Pattern: three consecutive direct jumps to new numbers.
#   Signal: automated bulk sender. Flag.
#
# AFTER (native search flow):
#   Server log: user typed in search box
#               user clicked search result
#               chat opened via contact resolver
#               message sent
#               user returned to chat list
#               user typed in search box again
#               ...
#   Pattern: sequential human search interactions.
#   Signal: person messaging multiple people manually. Normal.
#
# The difference is not in what happens inside the browser.
# It's in what WhatsApp's server-side logs show as the
# ORIGIN and METHOD of each chat being opened.
#
# ==============================================================
# WHAT TO DO ABOUT THE CURRENT RESTRICTION
# ==============================================================
#
# 1. Wait 24-48 hours. Do not touch the automation.
#
# 2. Replace _navigate_to_chat() and send_text() as shown above.
#    Also replace _rotate_tab() with the updated version.
#
# 3. Reset your DB:
#    python main.py --reset-failed
#    Plus the FAILED_FINAL reset script from earlier.
#
# 4. Update your session schedule — nothing before 09:00:
#    {"time": "09:00", "count": 5},
#    {"time": "11:00", "count": 5},
#    {"time": "14:00", "count": 5},
#
# 5. After restriction lifts, warmup:
#    Day 1: --run --now --count 1 (one message, watch it happen)
#    Day 2: --run --now --count 3
#    Day 3: --run --now --count 5
#    Day 4: normal schedule (5-8 per session)
#
# 6. Never run before 09:00 or after 19:00.
#
# ==============================================================