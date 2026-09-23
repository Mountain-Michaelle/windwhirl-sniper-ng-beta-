# ==============================================================
# HUMAN DISTRACTION ENGINE
# ==============================================================
# PATH: apps/core/lib/utils/distraction.py  (NEW FILE)
#
# PHILOSOPHY:
#   A fixed distraction pattern (e.g. "check own chat every 3 msgs")
#   is detectable because it creates a perfectly regular signal.
#   The WhatsApp algorithm learns periodic patterns — it's what
#   machine learning is specifically designed to do.
#
#   Real humans are NOT periodic. They:
#     - Sometimes check 5 messages without any distraction
#     - Sometimes get distracted twice in a row
#     - Stay different amounts of time in each place they visit
#     - Visit different things each time (not always the same chat)
#     - Sometimes scroll, sometimes just glance, sometimes type
#     - Occasionally go back to the chat list without opening anything
#
#   To defeat pattern learning, the system must have:
#     1. Variable FREQUENCY — distraction fires probabilistically,
#        not on a fixed interval
#     2. Variable DESTINATION — different chats visited each time,
#        chosen from a pool that itself varies
#     3. Variable BEHAVIOUR — different actions taken when there
#        (just scroll, scroll and pause, open a message, etc.)
#     4. Variable DURATION — time spent differs each distraction
#     5. MEMORY — avoid repeating the same destination twice in a row
#     6. OCCASIONAL ABSTENTION — sometimes no distraction happens
#        for several messages in a row (humans get focused sometimes)
#
#   The result: no two distraction events look the same.
#   No fixed period, no fixed destination, no fixed behaviour.
#   The algorithm cannot build a model of this pattern because
#   there is no pattern — there is only probability.
#
# HOW IT INTEGRATES:
#   The Scheduler calls distraction_engine.maybe_distract(page)
#   after each message send. The engine decides internally whether
#   to distract and what to do. The Scheduler just awaits the result.
#
# WHAT COUNTS AS A "DISTRACTION DESTINATION":
#   1. Your own number (self-chat / Saved Messages)
#   2. The chat list home page — just scrolling, not opening anything
#   3. An existing contact from a predefined pool
#   4. Searching for something and immediately closing search
#   5. Opening the Status tab and closing it
#   6. Hovering over the New Chat button without clicking
#
# ==============================================================

import asyncio
import logging
import random
from datetime import datetime

logger = logging.getLogger(__name__)


class DistractionEngine:
    """
    Simulates genuine human browsing behaviour between messages.

    Called after each message send. Decides probabilistically
    whether to distract, then executes a random behaviour from
    a pool of realistic human actions.

    No fixed period. No fixed destination. No repeating pattern.
    The algorithm cannot learn what it cannot predict.

    Usage:
        engine = DistractionEngine(cfg, page)
        await engine.maybe_distract()  # Called after each message
    """

    # ── Distraction behaviours ──────────────────────────────────
    # Each behaviour is a method name + human-readable label.
    # The pool is weighted so some behaviours are more common
    # than others (mimicking real usage frequency distributions).
    #
    # Weights are relative — higher weight = more likely chosen.
    # Weights intentionally NOT equal — equal weights look uniform.
    BEHAVIOURS = [
        # (method_name, label, weight)
        ("_visit_chat_list",          "Browse chat list",         30),
        ("_visit_own_chat",           "Check saved messages",     20),
        ("_open_and_close_search",    "Open search briefly",      15),
        ("_scroll_chat_list",         "Scroll through chats",     15),
        ("_visit_status_tab",         "Glance at Status tab",     10),
        ("_hover_new_chat_button",    "Hover over New Chat",       5),
        ("_open_existing_contact",    "Check existing chat",      20),
        ("_do_nothing",               "No distraction (focused)",  0),
        # Note: _do_nothing weight is 0 here — it's handled by
        # the probability gate in maybe_distract() instead
    ]

    def __init__(self, cfg, page, own_number: str = ""):
        """
        Args:
            cfg:        AppConfig instance — provides delay settings
            page:       Playwright page object (the active browser tab)
            own_number: Your personal WhatsApp number for self-chat visits.
                        Format: 13-digit normalized e.g. "2348XXXXXXXXX"
                        Can be empty — self-chat visit is skipped if so.
        """
        self._cfg        = cfg
        self._page       = page
        self._own_number = own_number
        self._log        = logging.getLogger(self.__class__.__name__)

        # ── State tracking ──────────────────────────────────────
        # Track messages since last distraction — used to ensure
        # we don't go TOO long without any distraction (unrealistic)
        # but also don't distract too frequently (also unrealistic)
        self._msgs_since_last_distraction = 0
        self._last_behaviour              = None  # Avoid immediate repeat
        self._total_distractions          = 0

        # ── Configurable probability thresholds ─────────────────
        # Base probability of distraction after each message
        # This is NOT a fixed interval — it's a coin flip each time
        self._base_prob = 0.35  # 35% chance of distraction after each message

        # After this many messages without distraction, increase probability
        # (humans always get distracted eventually)
        self._max_focus_streak = 5  # After 5 msgs focused, next is likely distracted

        # Pool of existing contacts to "visit" during distraction
        # Populated from your actual WhatsApp chat list on first use
        # Empty by default — filled by _discover_existing_contacts()
        self._contact_pool = []
        self._contacts_discovered = False

    async def maybe_distract(self):
        """
        Main entry point. Called by Scheduler after each message send.

        Decides probabilistically whether to distract, then executes
        a random behaviour. Returns control when distraction is done.

        The probability of distraction increases with the number of
        consecutive focused messages — humans always get distracted
        eventually, but the timing is unpredictable.
        """
        self._msgs_since_last_distraction += 1

        # ── Calculate effective probability ─────────────────────
        # Base probability + increasing bonus for long focus streaks
        # After 5 focused messages: 35% + 30% = 65% chance
        # After 7 focused messages: 35% + 50% = 85% chance
        focus_bonus = max(
            0,
            (self._msgs_since_last_distraction - 3) * 0.10
        )
        effective_prob = min(0.85, self._base_prob + focus_bonus)

        # ── Decide whether to distract ───────────────────────────
        if random.random() > effective_prob:
            self._log.debug(
                f"  No distraction (prob={effective_prob:.0%}, "
                f"streak={self._msgs_since_last_distraction})"
            )
            return  # No distraction this time

        # ── Choose a behaviour ───────────────────────────────────
        behaviour = self._choose_behaviour()

        self._log.info(
            f"  🔀 Distraction #{self._total_distractions + 1}: "
            f"{behaviour[1]} "
            f"(streak was {self._msgs_since_last_distraction} msgs)"
        )

        # ── Execute the behaviour ────────────────────────────────
        try:
            method = getattr(self, behaviour[0])
            await method()
        except Exception as e:
            # Distraction failure must never affect the send flow
            self._log.debug(f"  Distraction error (non-critical): {e}")

        # ── Reset streak counter ─────────────────────────────────
        self._last_behaviour              = behaviour[0]
        self._msgs_since_last_distraction = 0
        self._total_distractions         += 1

    def _choose_behaviour(self) -> tuple:
        """
        Weighted random selection from BEHAVIOURS pool.
        Excludes the last behaviour to prevent immediate repeats.
        Excludes self-chat if own_number not configured.
        """
        available = [
            b for b in self.BEHAVIOURS
            if b[0] != "_do_nothing"            # Handled by probability gate
            and b[0] != self._last_behaviour    # No immediate repeats
            and not (                            # Skip self-chat if not configured
                b[0] == "_visit_own_chat"
                and not self._own_number
            )
            and not (                            # Skip existing contact if pool empty
                b[0] == "_open_existing_contact"
                and not self._contact_pool
                and not self._contacts_discovered
            )
        ]

        if not available:
            # Fallback if all filtered out
            return ("_visit_chat_list", "Browse chat list", 30)

        # Weighted random selection
        weights = [b[2] for b in available]
        return random.choices(available, weights=weights, k=1)[0]

    # ──────────────────────────────────────────────────────────
    # DISTRACTION BEHAVIOURS
    # Each simulates a specific human WhatsApp browsing action.
    # Duration is randomized within realistic human ranges.
    # ──────────────────────────────────────────────────────────

    async def _visit_chat_list(self):
        """
        Navigate to WhatsApp Web home (chat list).
        Pause as if reading recent message previews.
        Sometimes scroll slightly through the list.

        Duration: 3–12 seconds
        Human analogy: checking to see if anyone replied
        """
        await self._page.goto(
            "https://web.whatsapp.com",
            wait_until="domcontentloaded"
        )

        # Random pause — just looking at chat list
        await asyncio.sleep(random.uniform(3, 12))

        # 40% chance of scrolling through the list a bit
        if random.random() < 0.4:
            scroll_amount = random.randint(200, 800)
            await self._page.evaluate(
                f"document.querySelector('[data-tab=\"8\"]')"
                f"?.scrollBy(0, {scroll_amount})"
            )
            await asyncio.sleep(random.uniform(1, 4))
            # Scroll back up sometimes
            if random.random() < 0.5:
                await self._page.evaluate(
                    f"document.querySelector('[data-tab=\"8\"]')"
                    f"?.scrollBy(0, -{scroll_amount})"
                )
                await asyncio.sleep(random.uniform(0.5, 2))

    async def _visit_own_chat(self):
        """
        Navigate to your own number (Saved Messages / self-chat).
        Read your own notes briefly, then leave.

        Duration: 5–20 seconds
        Human analogy: checking something you noted to yourself
        """
        if not self._own_number:
            await self._visit_chat_list()
            return

        await self._page.goto(
            f"https://web.whatsapp.com/send?phone={self._own_number}",
            wait_until="domcontentloaded"
        )

        # Wait for chat to load
        await asyncio.sleep(random.uniform(2, 5))

        # Stay and "read" for a random amount of time
        await asyncio.sleep(random.uniform(3, 15))

        # Sometimes scroll up through old messages
        if random.random() < 0.3:
            await self._page.evaluate("window.scrollBy(0, -300)")
            await asyncio.sleep(random.uniform(1, 5))
            await self._page.evaluate("window.scrollBy(0, 300)")
            await asyncio.sleep(random.uniform(0.5, 2))

    async def _open_and_close_search(self):
        """
        Click the search button, type 1–3 characters, close it.

        Duration: 4–10 seconds
        Human analogy: looking for a specific chat then changing mind
        """
        try:
            # Click the search icon or search bar
            search_selectors = [
                'div[aria-label="Search input textbox"]',
                'button[aria-label="Search or start new chat"]',
                'div[data-tab="3"]',
                'span[data-icon="search"]',
            ]

            clicked = False
            for sel in search_selectors:
                try:
                    el = await self._page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                await self._visit_chat_list()
                return

            await asyncio.sleep(random.uniform(0.5, 1.5))

            # Type 1–3 random characters (like starting to search)
            chars_to_type = random.randint(1, 3)
            for _ in range(chars_to_type):
                char = random.choice("abcdefghijklmnoprstuw")
                await self._page.keyboard.type(char)
                await asyncio.sleep(random.uniform(0.2, 0.8))

            # Pause as if reading search results
            await asyncio.sleep(random.uniform(1, 4))

            # Close search with Escape
            await self._page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.5, 1.5))

        except Exception:
            await self._visit_chat_list()

    async def _scroll_chat_list(self):
        """
        Stay on the chat list and scroll through it without opening anything.

        Duration: 5–15 seconds
        Human analogy: scanning through chats out of habit
        """
        # Ensure we're on the chat list home
        try:
            chat_list = await self._page.query_selector(
                'div[aria-label="Chat list"]'
            )
            if not chat_list:
                await self._page.goto(
                    "https://web.whatsapp.com",
                    wait_until="domcontentloaded"
                )
                await asyncio.sleep(2)
        except Exception:
            pass

        # Scroll down through the list
        scroll_steps = random.randint(2, 5)
        for _ in range(scroll_steps):
            scroll_amount = random.randint(100, 400)
            await self._page.evaluate(
                f"window.scrollBy(0, {scroll_amount})"
            )
            # Pause at each scroll position — reading previews
            await asyncio.sleep(random.uniform(0.8, 3.0))

        # Random chance of scrolling back up
        if random.random() < 0.5:
            await self._page.evaluate("window.scrollBy(0, -9999)")
            await asyncio.sleep(random.uniform(1, 3))

    async def _visit_status_tab(self):
        """
        Click the Status tab, pause briefly, return to chats.

        Duration: 4–12 seconds
        Human analogy: checking who posted a status update
        """
        try:
            status_selectors = [
                'span[data-icon="status-v3"]',
                'button[aria-label="Status"]',
                'div[aria-label="Status"]',
                '[data-tab="4"]',
            ]

            clicked = False
            for sel in status_selectors:
                try:
                    el = await self._page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                await self._visit_chat_list()
                return

            # Browse status page briefly
            await asyncio.sleep(random.uniform(3, 10))

            # Navigate back to chat list
            back_selectors = [
                'span[data-icon="back"]',
                'button[aria-label="Back"]',
                'div[aria-label="Chats"]',
            ]
            for sel in back_selectors:
                try:
                    el = await self._page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        break
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(1, 3))

        except Exception:
            await self._visit_chat_list()

    async def _hover_new_chat_button(self):
        """
        Move the mouse to the New Chat button without clicking.
        Pause briefly as if about to start a chat, then don't.

        Duration: 2–6 seconds
        Human analogy: almost starting a conversation then deciding not to
        """
        try:
            new_chat_selectors = [
                'span[data-icon="new-chat-outline"]',
                'div[aria-label="New chat"]',
                '[data-icon="compose"]',
            ]

            for sel in new_chat_selectors:
                try:
                    el = await self._page.query_selector(sel)
                    if el:
                        box = await el.bounding_box()
                        if box:
                            cx = int(box["x"] + box["width"] / 2)
                            cy = int(box["y"] + box["height"] / 2)
                            await self._page.mouse.move(cx, cy)
                            await asyncio.sleep(random.uniform(1, 4))
                            # Move away — decided not to click
                            await self._page.mouse.move(
                                random.randint(100, 400),
                                random.randint(300, 600)
                            )
                            await asyncio.sleep(random.uniform(0.5, 2))
                            return
                except Exception:
                    continue

        except Exception:
            pass

        await asyncio.sleep(random.uniform(2, 5))

    async def _open_existing_contact(self):
        """
        Open one of the contacts from the discovered pool, browse it briefly.

        The pool is populated from your actual recent chats.
        This makes the distraction look like checking on a real conversation.

        Duration: 5–20 seconds
        Human analogy: checking if someone replied
        """
        # Discover contacts from chat list if not done yet
        if not self._contacts_discovered:
            await self._discover_existing_contacts()

        if not self._contact_pool:
            # No contacts discovered — fall back to chat list visit
            await self._visit_chat_list()
            return

        # Pick a random contact from the pool
        # Avoid picking the same one twice in a row
        available = [c for c in self._contact_pool if c != self._last_contact]
        if not available:
            available = self._contact_pool

        contact_url = random.choice(available)
        self._last_contact = contact_url

        try:
            await self._page.goto(
                contact_url,
                wait_until="domcontentloaded"
            )
            await asyncio.sleep(random.uniform(2, 5))

            # Stay and "read" the conversation
            read_time = random.uniform(4, 18)
            await asyncio.sleep(read_time)

            # Occasionally scroll up through messages
            if random.random() < 0.35:
                await self._page.evaluate(
                    f"window.scrollBy(0, -{random.randint(200, 600)})"
                )
                await asyncio.sleep(random.uniform(1, 4))

        except Exception:
            await self._visit_chat_list()

    async def _discover_existing_contacts(self):
        """
        Read the current chat list and extract phone numbers of
        existing contacts to use as distraction destinations.

        Only runs once per session. Stores results in self._contact_pool.
        Picks up to 8 existing contacts — enough variety without
        overloading the pool with rarely-visited chats.
        """
        self._contacts_discovered = True

        try:
            # Navigate to home to see the chat list
            await self._page.goto(
                "https://web.whatsapp.com",
                wait_until="domcontentloaded"
            )
            await asyncio.sleep(2)

            # Find all chat list items with phone-like aria-labels
            # These are direct chats (not groups)
            chat_items = await self._page.query_selector_all(
                '[aria-label*="+234"], '    # Nigerian numbers
                '[data-id*="@c.us"]'        # WhatsApp individual chat IDs
            )

            pool = []
            for item in chat_items[:15]:   # Check first 15
                try:
                    # Get the data-id or aria-label to build a URL
                    data_id    = await item.get_attribute("data-id")
                    aria_label = await item.get_attribute("aria-label")

                    if data_id and "@c.us" in data_id:
                        # Extract phone from data-id format: "234XXX@c.us"
                        phone = data_id.replace("@c.us", "")
                        if phone.startswith("234") and len(phone) == 13:
                            pool.append(
                                f"https://web.whatsapp.com/send?phone={phone}"
                            )

                    elif aria_label and "+234" in aria_label:
                        # Extract from aria-label
                        match = __import__("re").search(
                            r'\+?234\d{10}', aria_label
                        )
                        if match:
                            phone = match.group().lstrip("+")
                            pool.append(
                                f"https://web.whatsapp.com/send?phone={phone}"
                            )

                except Exception:
                    continue

            # Take up to 8 unique contacts
            self._contact_pool = list(set(pool))[:8]
            self._last_contact = None

            self._log.debug(
                f"  Contact pool populated: {len(self._contact_pool)} contacts"
            )

        except Exception as e:
            self._log.debug(f"  Contact discovery failed: {e}")
            self._contact_pool = []

# ── END OF FILE ────────────────────────────────────────────────


# ==============================================================
# SCHEDULER INTEGRATION
# ==============================================================
# PATH: apps/core/lib/scheduler/scheduler.py
# ACTION: Add DistractionEngine to __init__ and run_session()
# ==============================================================

# ── ADDITION 1: Add to imports at top of scheduler.py ──────────

"""
from apps.core.lib.utils.distraction import DistractionEngine
"""

# ── ADDITION 2: Add to Scheduler.__init__() ────────────────────
# Find self._log = ... and add after it:

"""
        # DistractionEngine — initialized when sender connects
        # own_number comes from cfg.personal_whatsapp if configured
        self._distraction = None
"""

# ── ADDITION 3: Add to run_session() after sender reconnect ────
# Find "Fetched {len(customers)} customers" log line
# Add BEFORE the customer loop starts:

"""
        # Initialize distraction engine if not done yet
        # Uses the personal_whatsapp number for self-chat visits
        if self._distraction is None:
            self._distraction = DistractionEngine(
                cfg=self._cfg,
                page=self._sender._page,
                own_number=getattr(self._cfg, 'personal_whatsapp', '')
            )
            self._log.debug("Distraction engine initialized.")
"""

# ── ADDITION 4: Add AFTER the DB update block in the customer loop
# After the if/elif status block (SENT/ALREADY_CONTACTED/etc.)
# and BEFORE the delay logic:

"""
            # ── Human distraction between messages ─────────────
            # Probabilistic — not every message triggers distraction.
            # The engine decides internally based on streak length.
            # Skip distraction after ALREADY_CONTACTED (no msg sent
            # so no need to simulate post-send behaviour)
            if result.status != "ALREADY_CONTACTED" and not is_last:
                await self._distraction.maybe_distract()
"""


# ==============================================================
# HOW THE RANDOMNESS PREVENTS PATTERN LEARNING
# ==============================================================
#
# What the WhatsApp algorithm would need to detect this:
#
# FIXED PATTERN (detectable):
#   msg → msg → msg → visit_own_chat → msg → msg → msg → visit_own_chat
#   Period = 3. Algorithm detects period-3 behaviour easily.
#
# THIS SYSTEM (not detectable):
#   msg → (35% chance) → distraction OR no distraction
#   If distraction: random choice from 6 behaviours, random duration
#
#   Example session A:
#   msg → nothing → msg → chat_list(8s) → msg → nothing → msg
#         → status_tab(6s) → msg → nothing → nothing → msg
#         → own_chat(12s) → msg
#
#   Example session B (same code, different run):
#   msg → nothing → nothing → msg → search(3s) → msg → nothing
#         → msg → msg → chat_list(4s) → msg → hover_new_chat(2s)
#         → msg → nothing → msg
#
#   Session A and Session B look NOTHING like each other.
#   There is no period. There is no pattern. There is only entropy.
#
#   For the algorithm to learn this, it would need to observe
#   thousands of sessions from this exact account. By then you've
#   already sent to all 96 customers and are done.
#
# ==============================================================
# ADDITIONAL RANDOMISATION THAT MAKES IT HARDER
# ==============================================================
#
# 1. _choose_behaviour() excludes the last behaviour used
#    → Never the same action twice in a row
#
# 2. Duration of each behaviour is randomized within a range
#    → Even the same behaviour looks different each time
#
# 3. _discover_existing_contacts() reads YOUR actual chat list
#    → The contact pool is unique to your account
#    → Not a hardcoded list of numbers
#
# 4. The probability increases with streak length
#    → A 5-message focused streak bumps probability to 65%
#    → Prevents unrealistically long periods without any distraction
#    → But the exact message count when it fires is unpredictable
#
# 5. After restriction lifts: start base_prob at 0.45 (higher)
#    → More frequent distractions during warmup period
#    → Gradually reduce to 0.35 after 3 days of clean sending
# ==============================================================