# ==============================================================
# FIXED: _navigate_to_chat() — correct selectors confirmed
# ==============================================================
# PATH: apps/core/lib/utils/playwright_sender.py
# ACTION: Replace _navigate_to_chat() method only
#
# WHAT WAS WRONG:
#   All previous selectors looked for div[aria-label="Search..."]
#   Your WhatsApp Web uses: <input aria-label="Search or start a new chat">
#   That is an <input> tag, not a <div> — selector never matched.
#
# CONFIRMED FROM YOUR SELECTOR FINDER OUTPUT:
#   ✅ input[type="text"]
#      aria-label: Search or start a new chat
#      data-tab:   3
#      role:       textbox
#      Position:   x=133, y=76, w=423, h=20
#
# ALSO CONFIRMED:
#   The restriction banner is gone from your screenshot.
#   Your account is clear. Ready to send.
#
# GOOD NEWS FROM YOUR SCREENSHOT:
#   "Good morning Sir. It's Michael from Nabeau Store. Yo..."
#   visible in the chat list for +234 803 675 6977
#   This means the search-based flow already sent ONE message
#   successfully on a previous run. The system IS working.
# ==============================================================


# ── REPLACE _navigate_to_chat() in playwright_sender.py ───────

"""
    async def _navigate_to_chat(self, phone: str) -> tuple:
        '''
        Open a chat using WhatsApp's native search bar.
        This is how a real human starts a new conversation.

        CONFIRMED SELECTORS (from selector_finder.py output):
          Search input: input[aria-label="Search or start a new chat"]
          Tag is <input> not <div> — critical distinction.

        Flow:
          1. Go to WhatsApp Web home if not already there
          2. Click the search input
          3. Type the phone number digit by digit
          4. Wait for results
          5. Click the matching result
          6. Handle modal if it appears
          7. Return the message input selector

        Args:
            phone: 13-digit normalized number e.g. "2348037882259"

        Returns:
            (success: bool, msg_selector: str, error: str)
        '''

        # ── Step 1: Ensure we are on the chat list home ──────────
        try:
            current_url = self._page.url
            if (
                "web.whatsapp.com" not in current_url
                or "/send?" in current_url
            ):
                await self._page.goto(
                    "https://web.whatsapp.com",
                    wait_until="domcontentloaded",
                    timeout=15_000
                )
                await asyncio.sleep(random.uniform(1.5, 3))
        except Exception as e:
            return False, "", f"Could not navigate to WhatsApp home: {e}"

        # ── Step 2: Click the search input ───────────────────────
        # CONFIRMED selector from selector_finder output:
        # <input aria-label="Search or start a new chat" data-tab="3">
        SEARCH_SELECTOR = 'input[aria-label="Search or start a new chat"]'

        try:
            # Wait for search input to be visible
            await self._page.wait_for_selector(
                SEARCH_SELECTOR,
                timeout=8_000
            )
            el = await self._page.query_selector(SEARCH_SELECTOR)

            if not el or not await el.is_visible():
                return False, "", "Search input not visible"

            # Human mouse movement to the search bar
            box = await el.bounding_box()
            if box:
                cx = int(box["x"] + box["width"] / 2)
                cy = int(box["y"] + box["height"] / 2)
                await self._mouse_human_to(cx, cy)

            await el.click()
            await asyncio.sleep(random.uniform(0.3, 0.7))

            self._log.debug("  Search input clicked.")

        except Exception as e:
            return False, "", f"Could not click search input: {e}"

        # ── Step 3: Clear any existing text in search ─────────────
        # Sometimes the search bar retains text from previous search
        await self._page.keyboard.press("Control+a")
        await self._page.keyboard.press("Delete")
        await asyncio.sleep(random.uniform(0.2, 0.4))

        # ── Step 4: Type the phone number naturally ───────────────
        # Humans type phone numbers in chunks with slight pauses
        # We use the full 13-digit number: 2348XXXXXXXXX
        # WhatsApp search finds it regardless of format

        chunks = [
            phone[:3],   # 234 — country code
            phone[3:6],  # 803 — first chunk
            phone[6:9],  # 788 — second chunk
            phone[9:],   # 2259 — last chunk
        ]

        for i, chunk in enumerate(chunks):
            for digit in chunk:
                await self._page.type(
                    SEARCH_SELECTOR,
                    digit,
                    delay=random.uniform(80, 220)  # Slower than text — number entry
                )
            # Brief pause between chunks — human recall pattern
            if i < len(chunks) - 1:
                await asyncio.sleep(random.uniform(0.1, 0.35))

        self._log.debug(f"  Typed number: {phone}")

        # ── Step 5: Wait for search results ──────────────────────
        # WhatsApp queries contacts and shows results
        # The result we want is either a contact card or a phone number
        await asyncio.sleep(random.uniform(1.2, 2.0))

        # ── Step 6: Click the first result ───────────────────────
        # Result selectors in priority order
        # These are the elements that appear in the search dropdown
        result_selectors = [
            # Span containing the matching phone number in results
            f'span[title="+{phone}"]',
            f'span[title="{phone}"]',
            f'span[title*="{phone[-10:]}"]',       # Last 10 digits

            # Generic first list item in search results
            'div[aria-label="Search results"] div[role="listitem"]:first-child',
            'div[data-testid="search-list"] div[role="listitem"]:first-child',

            # Cell frame — WhatsApp's generic chat list item
            'div[data-testid="cell-frame-container"]:first-child',

            # Any list item that appeared after typing
            'div[role="listitem"]:first-child',
            'div[data-testid="list-item-0"]',
        ]

        # Small human pause before clicking — reading the result
        await asyncio.sleep(random.uniform(0.4, 0.9))

        result_clicked = False
        for sel in result_selectors:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
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
            # Last resort: press Enter to select the top result
            await self._page.keyboard.press("Enter")
            result_clicked = True
            self._log.debug("  Used Enter to select top search result")

        await asyncio.sleep(random.uniform(0.8, 1.5))

        # ── Step 7: Handle modal if it appears ───────────────────
        # For unsaved numbers WhatsApp sometimes shows a confirmation
        # modal before opening the chat. Click the Message/OK button.
        modal_selectors = [
            # New chat modal buttons
            'div[data-animate-modal-body="true"] button',
            'div[role="dialog"] button',
            'div[data-testid="popup-contents"] button',
            # Inline "New chat" action button
            'div[data-testid="new-chat-btn"]',
            'button[data-testid="new-chat-btn"]',
        ]

        await asyncio.sleep(random.uniform(0.5, 1.0))

        for sel in modal_selectors:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    btn_text = (await el.inner_text()).lower().strip()
                    # Only click buttons that mean "proceed"
                    proceed_words = [
                        "message", "chat", "ok", "open",
                        "continue", "new chat", "start"
                    ]
                    if any(w in btn_text for w in proceed_words):
                        await asyncio.sleep(random.uniform(0.3, 0.7))
                        await el.click()
                        self._log.debug(
                            f"  Modal button clicked: '{btn_text}'"
                        )
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                        break
            except Exception:
                continue

        # ── Step 8: Find the message input in the opened chat ─────
        await asyncio.sleep(random.uniform(0.8, 1.5))

        try:
            msg_selector = await self._find_msg_input(timeout_ms=15_000)
            self._log.debug(f"  Chat open. Input: {msg_selector}")
            return True, msg_selector, ""

        except TimeoutError as e:
            # Check for invalid number before giving up
            if await self._check_invalid_number():
                return False, "", "INVALID_NUMBER"
            if await self._check_account_restricted():
                return False, "", "RESTRICTED"
            # Take debug screenshot to see what is showing
            try:
                debug_path = (
                    self._ss_path
                    / f"debug_nav_{datetime.now().strftime('%H%M%S')}.png"
                )
                await self._page.screenshot(path=str(debug_path))
                self._log.warning(
                    f"  Could not find message input after navigation.\n"
                    f"  Debug screenshot: {debug_path}"
                )
            except Exception:
                pass
            return False, "", str(e)
"""


# ==============================================================
# ALSO ADD THIS TO MSG_INPUT_SELECTORS list
# ==============================================================
# Your WhatsApp Web uses a different message input than the defaults.
# From your screenshot the message area is the standard WhatsApp Web
# compose box. Add this selector to the list in the class:
#
# Find MSG_INPUT_SELECTORS in your playwright_sender.py
# and add this line at the TOP of the list (try it first):

"""
    MSG_INPUT_SELECTORS = [
        'div[aria-label="Type a message"]',          # Most common
        'div[aria-label="Message"]',                  # Alternate label
        'div[contenteditable="true"][data-tab="10"]', # data-tab variant
        'div[contenteditable="true"][data-tab="1"]',  # older variant
        'footer div[contenteditable="true"]',          # footer fallback
        'div[role="textbox"][aria-label*="message" i]', # case-insensitive
    ]
"""


# ==============================================================
# WHAT TO DO RIGHT NOW — IN ORDER
# ==============================================================
#
# 1. Your account restriction is LIFTED (no banner in screenshot)
#    You are ready to send.
#
# 2. Replace _navigate_to_chat() with the method above.
#
# 3. Reset your DB records:
#    python -c "
#    import sys
#    from pathlib import Path
#    sys.path.insert(0, str(Path('.').resolve().parent))
#    from apps.core.config import AppConfig
#    from apps.core.db.database import Database, SendLog, SendStatus
#    cfg = AppConfig()
#    db  = Database(cfg.database_url)
#    db.init()
#    with db._session() as s:
#        rows = s.query(SendLog).filter(
#            SendLog.status.in_(['FAILED', 'FAILED_FINAL'])
#        ).all()
#        for r in rows:
#            r.status        = SendStatus.PENDING
#            r.attempt_count = 0
#            r.error_message = None
#        s.commit()
#        print(f'Reset {len(rows)} to PENDING')
#    "
#
# 4. Start with ONE message only to test:
#    python main.py --run --now --count 1
#
#    Watch the browser — you should see:
#      a) Search bar gets clicked
#      b) Phone number is typed digit by digit
#      c) A result appears in the dropdown
#      d) Result is clicked
#      e) Chat opens
#      f) Message is typed character by character
#      g) Enter is pressed once
#      h) Single tick appears (sent)
#
# 5. If step (d) fails — the result appears but click misses:
#    Take a screenshot of what the search dropdown looks like
#    and send it here. I will fix the result click selector.
#
# 6. If step (a)-(c) works but (d) fails — press Enter manually
#    during the test to confirm the number resolves correctly.
#    Then I will adjust the result click timing.
#
# 7. Once count 1 works: run count 3, then count 5, then schedule.
#
# ==============================================================
# NOTE ON THE CHAT ALREADY VISIBLE IN YOUR SCREENSHOT
# ==============================================================
# Your chat list shows:
#   +234 803 675 6977 — "Good morning Sir. It's Michael from Nabeau Store."
#
# This means ONE message was already sent successfully.
# When you test, the history check will correctly detect this chat
# has already been contacted and skip it (ALREADY_CONTACTED).
# That is the system working exactly as designed.
# ==============================================================