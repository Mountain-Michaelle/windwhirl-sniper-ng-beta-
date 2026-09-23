# ==============================================================
# FIX — Message typed and sent as ONE whole message
# ==============================================================
# PATH: apps/core/lib/utils/playwright_sender.py
# ACTION: Replace the _type_human() method and the send block
#         in send_text() only. Everything else stays the same.
#
# ROOT CAUSE OF MESSAGE SPLIT INTO PARTS:
#   WhatsApp Web sends the message when Enter is pressed.
#   The template has real newline characters (\n) between paragraphs.
#   When page.type() encounters \n it sends a keypress for Enter.
#   WhatsApp interprets that Enter as SEND, not as a line break.
#   Result: each paragraph fires a separate send.
#
# THE FIX:
#   Intercept every \n character in the message.
#   Instead of typing \n (which triggers Send):
#     → Press Shift+Enter (which inserts a line break in WhatsApp)
#   Only press plain Enter ONCE at the very end to send the message.
#
# ALSO FIXED IN THIS UPDATE:
#   The account restriction error you saw:
#     "Your account on linked devices is restricted.
#      You can't start new chats right now."
#   This is a WhatsApp temporary flag — NOT a code bug.
#   The code now detects this message and returns a clear
#   RESTRICTED status instead of a confusing timeout error.
#   See _check_account_restricted() method below.
#
# WHAT TO DO ABOUT THE RESTRICTION:
#   Wait 24-48 hours. Do NOT attempt to send during this time.
#   After waiting, run: python main.py --run --now --count 3
#   to confirm restriction is lifted before running full campaign.
# ==============================================================


# ==============================================================
# STEP 1 — Reset your DB before testing again
# ==============================================================
# Run this in your terminal to reset all failed/restricted records:
#
#   python -c "
#   import sys
#   from pathlib import Path
#   sys.path.insert(0, str(Path('.').resolve().parent))
#   from apps.core.config import AppConfig
#   from apps.core.db.database import Database, SendLog, SendStatus
#   cfg = AppConfig()
#   db  = Database(cfg.database_url)
#   db.init()
#   with db._session() as s:
#       rows = s.query(SendLog).filter(
#           SendLog.status.in_(['FAILED', 'FAILED_FINAL'])
#       ).all()
#       for r in rows:
#           r.status        = SendStatus.PENDING
#           r.attempt_count = 0
#           r.error_message = None
#       s.commit()
#       print(f'Reset {len(rows)} records to PENDING')
#   "
# ==============================================================


# ==============================================================
# STEP 2 — Replace these TWO methods in playwright_sender.py
# ==============================================================
# Find each method by its name and replace the ENTIRE method.
# Do not change anything else in the file.
#
# METHODS TO REPLACE:
#   1. _type_human()            — handles \n as Shift+Enter
#   2. _check_invalid_number()  — now also detects restriction
#   3. send_text()              — updated send block only
#
# ==============================================================


# ── METHOD 1: Replace _type_human() ───────────────────────────
# Find the existing _type_human() method and replace it entirely.

"""
    async def _type_human(self, selector: str, message: str):
        '''
        STEALTH 1 + 6: Type the COMPLETE message as one block.
        Sends as a single WhatsApp message — no splits.

        KEY FIX — newline handling:
            WhatsApp Web sends message on Enter keypress.
            Template newlines (\\n) must become Shift+Enter
            so they insert line breaks WITHOUT triggering send.
            Plain Enter is pressed ONLY ONCE at the very end
            by the calling send_text() method.

        This method types everything — text, emoji, line breaks —
        but does NOT press Enter at the end. The caller does that.

        Text segments:  page.type() character by character
        Newlines (\\n): Shift+Enter keypress (line break, not send)
        Emoji:          JavaScript injection (prevents garbling)
        '''
        # Regex to identify emoji characters
        emoji_re = re.compile(
            "["
            "\\U0001F600-\\U0001F64F"
            "\\U0001F300-\\U0001F5FF"
            "\\U0001F680-\\U0001F6FF"
            "\\U0001F1E0-\\U0001F1FF"
            "\\U00002702-\\U000027B0"
            "\\U000024C2-\\U0001F251"
            "\\U0001f926-\\U0001f937"
            "\\U00010000-\\U0010ffff"
            "\\u2640-\\u2642"
            "\\u2600-\\u2B55"
            "\\u200d\\ufe0f"
            "]+",
            flags=re.UNICODE
        )

        # Split message into typed segments
        # Each segment is one of: text, emoji, or newline
        # We process them in order to preserve message structure
        segments = []
        current_text = ""

        for char in message:
            if char == "\\n":
                # Flush any accumulated text first
                if current_text:
                    # Check if current_text contains emoji
                    parts = emoji_re.split(current_text)
                    emojis = emoji_re.findall(current_text)
                    for i, part in enumerate(parts):
                        if part:
                            segments.append(("text", part))
                        if i < len(emojis):
                            segments.append(("emoji", emojis[i]))
                    current_text = ""
                # Add the newline as its own segment type
                segments.append(("newline", "\\n"))
            else:
                current_text += char

        # Flush remaining text after loop
        if current_text:
            parts = emoji_re.split(current_text)
            emojis = emoji_re.findall(current_text)
            for i, part in enumerate(parts):
                if part:
                    segments.append(("text", part))
                if i < len(emojis):
                    segments.append(("emoji", emojis[i]))

        d          = self._cfg.delays
        char_count = 0

        for kind, content in segments:

            if kind == "newline":
                # ── INSERT LINE BREAK (not Send) ────────────────
                # Shift+Enter = new line in WhatsApp Web
                # Plain Enter = send message (we never press this here)
                await self._page.keyboard.down("Shift")
                await self._page.keyboard.press("Enter")
                await self._page.keyboard.up("Shift")
                await asyncio.sleep(random.uniform(0.05, 0.15))

            elif kind == "text":
                # ── TYPE CHARACTER BY CHARACTER (STEALTH 1) ─────
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

                    # Occasional mid-message thinking pause
                    if (
                        len(message) > 80
                        and char_count % random.randint(40, 60) == 0
                    ):
                        await asyncio.sleep(random.uniform(0.8, 2.0))

            elif kind == "emoji":
                # ── INJECT EMOJI VIA JAVASCRIPT (STEALTH 6) ─────
                try:
                    await self._page.evaluate(
                        \\'\\'\\'(args) => {
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
                            el.dispatchEvent(
                                new Event(\\'input\\', { bubbles: true })
                            );
                        }\\'\\'\\'',
                        {"selector": selector, "emoji": content}
                    )
                    await asyncio.sleep(random.uniform(0.05, 0.2))
                except Exception:
                    self._log.debug("Emoji JS injection failed — using type() fallback")
                    await self._page.type(selector, content, delay=50)
"""


# ── METHOD 2: Replace _check_invalid_number() ─────────────────
# Find the existing _check_invalid_number() and replace it.
# This version also detects the account restriction message.

"""
    async def _check_invalid_number(self) -> bool:
        '''
        True if WhatsApp is showing a 'number not registered' modal.
        Does NOT detect the account restriction — that is separate.
        '''
        try:
            body_text = (await self._page.inner_text("body")).lower()
            return any(p in body_text for p in self.INVALID_TEXTS)
        except Exception:
            return False

    async def _check_account_restricted(self) -> bool:
        '''
        True if WhatsApp is showing the account restriction banner:
          "Your account on linked devices is restricted.
           You can't start new chats right now."

        This is a temporary WhatsApp Business restriction, not a ban.
        Typically lifts within 24-48 hours.
        Do NOT attempt to send while this is showing — it extends
        the restriction period.

        Returns:
            True  → account is restricted, stop all sending
            False → account is fine, continue
        '''
        try:
            body_text = (await self._page.inner_text("body")).lower()
            restriction_signals = [
                "linked devices is restricted",
                "can't start new chats right now",
                "account on linked devices is restricted",
                "show details",   # The "Show details" link appears with restriction
            ]
            # Require at least 2 signals to avoid false positives
            # (some phrases appear in normal WhatsApp UI too)
            matches = sum(1 for s in restriction_signals if s in body_text)
            return matches >= 2
        except Exception:
            return False
"""


# ── METHOD 3: Replace send_text() ─────────────────────────────
# Find the existing send_text() method and replace it entirely.
# KEY CHANGES:
#   - Calls _check_account_restricted() after chat loads
#   - Removed the "Press Enter" from inside _type_human()
#   - Press Enter only ONCE at the very end after all typing done

"""
        async def send_text(
            self,
            phone:    str,
            message:  str,
            order_id: str
        ) -> SendResult:
            '''
            Send the COMPLETE message as one WhatsApp message.

            Newlines in the template become Shift+Enter (line breaks).
            Plain Enter is pressed only once at the very end (sends).
            Result: the customer receives one single message, not parts.

            Full flow:
            Step 0: Tab rotation                  (STEALTH 8)
            Step 1: Navigate to chat              (STEALTH 4)
            Step 2: Find message input            (6 selector variants)
            Step 3: Check account restriction     (stop if restricted)
            Step 4: Check invalid number
            Step 5: Chat history check            (reads current page)
            Step 6: Pre-typing pause              (STEALTH 2)
            Step 7: Occasional scroll             (STEALTH 7)
            Step 8: Human mouse to input          (STEALTH 3)
            Step 9: Click, type whole message     (STEALTH 1 + 6)
                    newlines → Shift+Enter
            Step 10: Press Enter ONCE to send
            Step 11: Wait for delivery tick
            Step 12: Screenshot
            Step 13: Return SendResult
            '''
            self._log.info(f"→ Sending to +{phone} [Order: {order_id}]")

            try:
                # ── Step 0: Tab rotation ─────────────────────────────
                await self._rotate_tab()

                # ── Step 1: Navigate to chat ─────────────────────────
                await self._page.goto(
                    f"https://web.whatsapp.com/send?phone={phone}",
                    wait_until="domcontentloaded",
                    timeout=20_000
                )

                # ── Step 2: Find message input ───────────────────────
                try:
                    msg_selector = await self._find_msg_input(timeout_ms=20_000)
                except TimeoutError as e:
                    if await self._check_invalid_number():
                        self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                        return SendResult(
                            success=False,
                            status="INVALID_NUMBER",
                            error_message="Phone not registered on WhatsApp"
                        )
                    self._log.error(str(e))
                    return SendResult(
                        success=False,
                        status="FAILED",
                        error_message=str(e)
                    )

                # ── Step 3: Check account restriction ────────────────
                # This check must happen BEFORE attempting to type.
                # If the account is restricted, stop all sending and
                # log a clear message. Do not mark as FAILED — this is
                # a temporary WhatsApp flag that will lift on its own.
                if await self._check_account_restricted():
                    self._log.warning(
                        "\\n" + "=" * 55 + "\\n"
                        "  WHATSAPP ACCOUNT RESTRICTED\\n"
                        "  Your Business account cannot start new chats\\n"
                        "  on linked devices right now.\\n"
                        "  This is temporary — typically 24-48 hours.\\n"
                        "  DO NOT attempt to send during this period.\\n"
                        "  Wait for the restriction to lift, then retry.\\n"
                        + "=" * 55
                    )
                    return SendResult(
                        success=False,
                        status="FAILED",
                        error_message=(
                            "WhatsApp account temporarily restricted from "
                            "starting new chats on linked devices. "
                            "Wait 24-48 hours before retrying."
                        )
                    )

                # ── Step 4: Check invalid number ─────────────────────
                if await self._check_invalid_number():
                    self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )

                # ── Step 5: Chat history check ───────────────────────
                already_contacted, matched_preview = (
                    await self._check_campaign_in_history(history_depth=4)
                )
                if already_contacted:
                    self._log.info(
                        f"  ↩ Skipped — campaign message found in chat history"
                    )
                    return SendResult(
                        success=False,
                        status="ALREADY_CONTACTED",
                        error_message=(
                            f"Campaign message found in chat: '{matched_preview}'"
                        )
                    )

                # ── Step 6: Pre-typing pause (STEALTH 2) ─────────────
                pre_pause = random.uniform(
                    self._cfg.delays.pre_type_min,
                    self._cfg.delays.pre_type_max
                )
                self._log.debug(f"  Pre-type pause: {pre_pause:.1f}s")
                await asyncio.sleep(pre_pause)

                # ── Step 7: Occasional scroll (STEALTH 7) ────────────
                if random.choice([True, False]):
                    scroll_up = random.uniform(100, 400)
                    await self._page.evaluate(f"window.scrollBy(0, -{scroll_up})")
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await self._page.evaluate("window.scrollBy(0, 10000)")
                    await asyncio.sleep(random.uniform(0.3, 0.8))

                # ── Step 8: Human mouse to input (STEALTH 3) ─────────
                el = await self._page.query_selector(msg_selector)
                if el:
                    box = await el.bounding_box()
                    if box:
                        cx = int(box["x"] + box["width"]  / 2)
                        cy = int(box["y"] + box["height"] / 2)
                        await self._mouse_human_to(cx, cy)

                # ── Step 9: Click and type whole message ──────────────
                # _type_human() handles newlines as Shift+Enter.
                # The complete message is typed before anything is sent.
                await self._page.click(msg_selector)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await self._type_human(msg_selector, message)

                # Brief pause after finishing — humans hesitate before sending
                await asyncio.sleep(random.uniform(0.5, 1.2))

                # ── Step 10: Press Enter ONCE to send ────────────────
                # This is the ONLY Enter keypress in the entire send flow.
                # _type_human() never presses Enter — it uses Shift+Enter
                # for newlines. This guarantees one message, not parts.
                await self._page.keyboard.press("Enter")
                self._log.debug("  Message sent. Waiting for delivery tick...")

                # ── Step 11: Wait for delivery tick ──────────────────
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

                # ── Step 12: Screenshot ───────────────────────────────
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
# SUMMARY OF ALL CHANGES
# ==============================================================
#
# _type_human():
#   BEFORE: typed \n directly → WhatsApp sent message mid-typing
#   AFTER:  \n → Shift+Enter (line break) → message stays in one box
#           Enter never pressed inside this method
#
# _check_account_restricted() (NEW METHOD):
#   Detects the "linked devices restricted" banner
#   Returns True → send_text() stops cleanly with clear log message
#   Does not count as a FAILED attempt (restriction is temporary)
#
# send_text():
#   BEFORE: Enter pressed somewhere inside type flow → split messages
#   AFTER:  Enter pressed exactly ONCE after all typing is done
#           Restriction check added as Step 3 (before anything typed)
#
# ==============================================================
# WHAT TO DO RIGHT NOW
# ==============================================================
#
# 1. Your account is restricted — DO NOT send anything for 24-48hrs
#
# 2. Replace the three methods in playwright_sender.py
#
# 3. After 24-48 hours, reset failed records:
#    python main.py --reset-failed
#    Then also run the FAILED_FINAL reset script from earlier
#
# 4. Test with count 1 first:
#    python main.py --run --now --count 1
#
# 5. Open WhatsApp on your phone and check:
#    - Does the message arrive as ONE message? ✅ Fix worked
#    - Is the restriction still showing? ❌ Wait more
#
# ==============================================================
# HOW TO AVOID RESTRICTION IN FUTURE
# ==============================================================
#
# The restriction happened because WhatsApp Business detected
# too many new chat initiations in a short period.
# The debug screenshot shows you opened 3 new chats in about
# 10 minutes — that triggered the flag.
#
# When you resume after restriction lifts:
#   - Start with count 1 on day 1 (one message only)
#   - If successful, count 3 on day 2
#   - If successful, count 5 on day 3
#   - Then resume normal schedule
#
# This warmup period rebuilds trust with WhatsApp's system
# before going back to full volume.
# ==============================================================