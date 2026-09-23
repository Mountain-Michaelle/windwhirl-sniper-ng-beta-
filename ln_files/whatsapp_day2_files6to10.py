# ==============================================================
# WHATSAPP REVIEW AUTOMATION — DAY 2 BUILD
# ==============================================================
# FILES IN THIS DOCUMENT:
#   FILE 6  → src/data_reader.py
#   FILE 7  → templates/message_a.j2
#   FILE 8  → templates/message_b.j2
#   FILE 9  → src/message_builder.py
#   FILE 10 → src/whatsapp_sender.py
#
# PREREQUISITE:
#   Day 1 files must be complete and all 5 tests passing before
#   building these. If Day 1 tests fail, fix them first.
#
# WHAT YOU ARE BUILDING TODAY:
#   FILE 6  — Reads your Excel, filters Sadoer customers, cleans
#             names, normalizes all Nigerian phone formats to E.164.
#             This is where your messy real-world data gets cleaned.
#
#   FILE 7  — Template A: "Results Check-In" message style.
#             Opens with a skin results question, not an ask.
#
#   FILE 8  — Template B: "Honest Feedback" message style.
#             Leads with "not looking for a perfect 5-star review".
#
#   FILE 9  — Renders templates per customer, randomly assigns A or B.
#             Tracks which template was used for reply-rate analytics.
#
#   FILE 10 — Abstract sender interface. Defines WHAT a sender
#             must do. PlaywrightSender (Day 3) implements it.
#             FastAPI routes (future) will call this same interface.
#
# FUTURE-AWARE DESIGN DECISIONS IN TODAY'S FILES:
#   - DataReader returns plain dicts, not ORM objects
#     → FastAPI endpoints can return these directly as JSON
#   - MessageBuilder is stateless enough to be injected as a
#     FastAPI dependency later
#   - WhatsAppSender abstract interface means:
#     CLI calls PlaywrightSender today
#     FastAPI routes call the same interface tomorrow
#     Official API integration replaces only one class later
#   - Templates live in templates/ folder (not hardcoded strings)
#     → A future UI can let users edit templates without touching code
#

# FOLDER STRUCTURE AFTER TODAY:
#
#   whatsapp_automation/
#   ├── requirements.txt          ← Day 1 FILE 1
#   ├── .gitignore                ← Day 1 FILE 2
#   ├── data/
#   │   └── customers.xlsx        ← your Excel file (drop it here now)
#   ├── templates/                ← CREATE THIS FOLDER TODAY
#   │   ├── message_a.j2          ← FILE 7
#   │   └── message_b.j2          ← FILE 8
#   └── src/
#       ├── __init__.py           ← Day 1 FILE 3
#       ├── config.py             ← Day 1 FILE 4
#       ├── database.py           ← Day 1 FILE 5
#       ├── data_reader.    py        ← FILE 6  (today)
#       ├── message_builder.py    ← FILE 9  (today)
#       └── whatsapp_sender.py    ← FILE 10 (today)
#
# DAY 2 VERIFICATION (run after all 5 files are saved):
#   python -c "
#   from src.config import AppConfig
#   from src.database import Database
#   from src.data_reader import DataReader
#
#   cfg = AppConfig()
#   db  = Database(cfg.database_url)
#   db.init()
#
#   reader    = DataReader(cfg.country_code)
#   customers = reader.read_and_filter(cfg.excel_path(), cfg.target_product)
#   for c in customers:
#       db.upsert_customer(c)
#
#   stats = db.get_stats()
#   print(f'Imported: {stats[\"total\"]} customers')
#   print(f'Pending:  {stats[\"pending\"]}')
#   print(f'Bad phones: {stats[\"invalid_phones\"]}')
#   "
#   Expected: Imported: 96 customers, Pending: 96, Bad phones: small number
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 6
#  PATH:   whatsapp_automation/src/data_reader.py
#  TYPE:   Python file
# ================================================================
# PURPOSE:
#   Reads your Excel file, filters to Sadoer customers only,
#   normalizes every phone number, cleans every name, and returns
#   a list of customer dicts ready to insert into the database.
#
# WHY THIS FILE IS THE MOST IMPORTANT FOR DATA QUALITY:
#   Garbage in = failed messages. Every messy format in your
#   real Excel file is handled here. If a row has a problem,
#   it is logged and skipped — the rest continue normally.
#
# PHONE FORMATS IN YOUR ACTUAL EXCEL:
#   "+08038365784"       → "+0" prefix (wrong but common)
#   "+2348053968527"     → already correct
#   "08130571075"        → local format, no country code
#   8068526757.0         → float (WhatsApp Number column)
#   2.348077e+12         → scientific notation float
#   NaN                  → empty cell
#   All of these normalize to: "2348XXXXXXXXX" (13 digits)
#
# PRODUCT VARIANTS YOUR EXCEL CONTAINS:
#   "Sadoer Collagen Combo Set"            → matched by "sadoer"
#   "Sadoer Collagen Combo Set G"          → matched by "sadoer"
#   "Sadoer Collagen Combo + Body Lotion"  → matched by "sadoer"
#   "New Executive Rolex Wristwatch"       → SKIPPED
#   "Aesthtany Advanced Vitamin C Serum"   → SKIPPED
#
# FUTURE / FASTAPI NOTE:
#   DataReader is a pure service class with no Flask/FastAPI
#   dependency. When you build the FastAPI layer, you can call:
#     reader.read_and_filter(path, product)
#   directly from an endpoint without changing anything here.
#   The returned list of dicts can be serialized to JSON as-is.
#
# TEST AFTER SAVING:
#   Drop your customers.xlsx into the data/ folder first, then:
#   python -c "
#   from src.config import AppConfig
#   from src.data_reader import DataReader
#   cfg = AppConfig()
#   r   = DataReader(cfg.country_code)
#   customers = r.read_and_filter(cfg.excel_path(), cfg.target_product)
#   print(f'Found: {len(customers)} Sadoer customers')
#   print('Sample:', customers[0]['first_name'], customers[0]['normalized_phone'])
#   "
#   Expected: Found: 96 Sadoer customers
# ================================================================
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/data_reader.py ──

# ── END OF FILE 6 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 7
#  PATH:  whatsapp_automation/templates/message_a.j2
#  TYPE:  Jinja2 template — plain text file, NOT Python
# ================================================================
# CREATE FIRST: make the templates/ folder in your project root
#               then create message_a.j2 inside it
#
# PURPOSE:
#   Template A — "Results Check-In" style message.
#   This is sent to roughly 50% of customers (random selection
#   in MessageBuilder alternates between A and B).
#
# PSYCHOLOGY BEHIND THIS TEMPLATE:
#   - Opens with the customer's name and a warm check-in
#     NOT with "can you leave us a review?" — that triggers
#     the "corporate blast" feeling before they've even engaged
#   - First question "Have you noticed any changes to your skin?"
#     is easy to answer AND makes them recall their experience
#     before the ask even lands — they're already thinking about it
#   - Voice note is explicitly suggested — most people find it
#     far easier to speak for 30 seconds than to type a paragraph
#   - Discount offer is mentioned LAST — leading with a bribe
#     makes the whole message feel transactional before they read it
#   - Short paragraphs — WhatsApp is not email. Long walls of
#     text get ignored. Each paragraph is one idea only.
#   - Ends with the customer's name again — feels like a real
#     person signed off, not an automated campaign
#
# VARIABLES AVAILABLE IN THIS TEMPLATE:
#   {{ first_name }}     — customer's cleaned first name (e.g. "Titilayo")
#   {{ discount_offer }} — from CONFIG: "10% off your next order"
#   {{ review_link }}    — optional URL, empty string if not configured
#
# JINJA2 SYNTAX USED:
#   {% if review_link %}...{% endif %}
#   → Only renders the link line if review_link is not empty string
#
# EDITING THIS TEMPLATE:
#   You can freely edit the message text without touching any code.
#   Run --dry-run after editing to preview the rendered output.
#
# FUTURE / UI NOTE:
#   When the Next.js UI is built, template files can be read from
#   this folder and displayed in an editor component. Users can
#   edit them through the browser without touching the filesystem.
# ================================================================
# ==============================================================

# ── COPY EVERYTHING INSIDE THE TRIPLE QUOTES INTO: templates/message_a.j2
# ── Remove the triple quotes themselves — save only the message text

"""
Hi {{ first_name }}! 👋

It's been a few months since you got your Sadoer Collagen Combo — just wanted to check in and see how it's been going for you? 😊

Have you noticed any changes to your skin since using it?

Your honest experience means a lot — even a quick voice note works perfectly 🎙️
{% if review_link %}
You can also drop your thoughts here: {{ review_link }}
{% endif %}
As a little thank you, I'll send you {{ discount_offer }} on your next order once you share 🎁

Looking forward to hearing from you, {{ first_name }}! 🙏
"""

# ── END OF FILE 7 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 8
#  PATH:  whatsapp_automation/templates/message_b.j2
#  TYPE:  Jinja2 template — plain text file, NOT Python
# ================================================================
# PURPOSE:
#   Template B — "Honest Feedback" style message.
#   Sent to the other ~50% of customers.
#
# PSYCHOLOGY BEHIND THIS TEMPLATE:
#   - "Not looking for a perfect 5-star review" is the key line.
#     It removes pressure immediately. People feel free to respond
#     honestly rather than feeling they need to perform positivity.
#     This paradoxically gets MORE replies, not fewer.
#   - "Your real experience" signals authenticity — it feels like
#     a genuine person asking, not a marketing department.
#   - Acknowledging the product might not have worked for everyone
#     builds trust. Customers who had mixed results feel safe to
#     share honestly rather than staying silent.
#   - Discount framed as secondary: "but honestly, your feedback
#     is what matters" feels authentic rather than transactional.
#   - Structurally different from Template A — different opening
#     hook, different paragraph order, different sign-off.
#     The 50/50 random split creates genuine variation across
#     your 96 customers so WhatsApp doesn't see a bulk pattern.
#
# SAME VARIABLES AS TEMPLATE A:
#   {{ first_name }}, {{ discount_offer }}, {{ review_link }}
# ================================================================
# ==============================================================

# ── COPY EVERYTHING INSIDE THE TRIPLE QUOTES INTO: templates/message_b.j2
# ── Remove the triple quotes themselves — save only the message text

"""
Hi {{ first_name }} 🙏

Quick one — you ordered your Sadoer Collagen Combo a few months back, and I'd genuinely love to know what you actually think of it.

I'm not looking for a perfect 5-star review — just your real experience. Did it work for you? What did you notice?

Even a short voice note is totally fine 🎙️
{% if review_link %}
Or share here: {{ review_link }}
{% endif %}
Everyone who shares gets {{ discount_offer }} — but honestly, your feedback is what shapes how we improve for you and others 💬

Thank you, {{ first_name }}!
"""

# ── END OF FILE 8 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 9
#  PATH:  whatsapp_automation/src/message_builder.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   Renders the correct Jinja2 template for each customer and
#   returns the final message string ready to send.
#
# KEY RESPONSIBILITIES:
#   1. Load templates from the templates/ folder on init
#   2. Randomly assign Template A or B per customer
#   3. Never assign the same template more than twice in a row
#      (prevents long runs of identical messages to consecutive customers)
#   4. Render the template with customer data + config values
#   5. Return (message_string, template_label) so the label
#      can be saved to DB for reply-rate analytics later
#
# WHY RANDOM A/B MATTERS:
#   - WhatsApp: 96 identical messages in one day is a spam pattern.
#     Two different templates breaks that pattern.
#   - Reply rate: different customers respond to different styles.
#     A/B lets you measure which works better for your audience.
#   - The template_used column in send_log tracks this for you.
#
# FUTURE / FASTAPI NOTE:
#   MessageBuilder has no framework dependency.
#   It can be used as a FastAPI dependency:
#     @app.post("/preview-message")
#     def preview(customer: CustomerSchema, builder: MessageBuilder = Depends(...)):
#         message, label = builder.build(customer.dict())
#         return {"message": message, "template": label}
#   No changes to this file needed for that.
#
# TEST AFTER SAVING:
#   python -c "
#   from src.config import AppConfig
#   from src.message_builder import MessageBuilder
#   cfg = AppConfig()
#   builder = MessageBuilder(cfg)
#   sample = {'first_name': 'Titilayo'}
#   msg, label = builder.build(sample)
#   print(f'Template {label}:')
#   print(msg)
#   "
# ================================================================
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/message_builder.py

import logging
import random
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined

logger = logging.getLogger(__name__)


class MessageBuilder:
    """
    Renders personalized WhatsApp messages from Jinja2 templates.

    Loads templates once on init. Renders on every build() call.
    Tracks last template used to prevent long runs of same template.

    Usage:
        builder = MessageBuilder(cfg)

        # Build one message (random A or B):
        message, label = builder.build(customer_dict)
        # message → "Hi Titilayo! 👋 ..."
        # label   → "A" or "B" — save this to DB

        # Preview both templates for dry-run:
        both = builder.preview(customer_dict)
        # both → {"A": "...", "B": "..."}
    """

    # Folder where .j2 template files live
    # Relative to the project root (where you run python from)
    TEMPLATES_DIR = Path("templates")

    def __init__(self, cfg):
        """
        Args:
            cfg: AppConfig instance — provides discount_offer and review_link.
        """
        self._cfg  = cfg
        self._log  = logging.getLogger(self.__class__.__name__)
        self._last = None  # Last template label used ('A' or 'B')

        # ── Load Jinja2 environment ─────────────────────────────
        # FileSystemLoader: loads .j2 files from the templates/ folder
        # undefined=Undefined: missing variables render as empty string
        #   (safer than StrictUndefined which raises an error if a
        #    variable is missing — a missing {{ review_link }} should
        #    just render as nothing, not crash the whole send session)
        self._env = Environment(
            loader=FileSystemLoader(str(self.TEMPLATES_DIR)),
            undefined=Undefined,
            keep_trailing_newline=True,
        )

        # Load both templates once at startup
        # Fails early with a clear error if template files are missing
        try:
            self._template_a = self._env.get_template("message_a.j2")
            self._template_b = self._env.get_template("message_b.j2")
            self._log.info(
                f"Templates loaded from: {self.TEMPLATES_DIR.resolve()}"
            )
        except Exception as e:
            raise FileNotFoundError(
                f"Could not load message templates from {self.TEMPLATES_DIR}/\n"
                f"Make sure message_a.j2 and message_b.j2 exist there.\n"
                f"Error: {e}"
            )

        self._templates = {
            "A": self._template_a,
            "B": self._template_b,
        }

    def _render(self, label: str, customer: dict) -> str:
        """
        Render one template with customer data and config values.

        Args:
            label:    "A" or "B" — which template to render
            customer: dict with at minimum {"first_name": "Titilayo"}

        Returns:
            Rendered message string, whitespace-cleaned.
        """
        template = self._templates[label]

        rendered = template.render(
            first_name    =customer.get("first_name", "Customer"),
            discount_offer=self._cfg.discount_offer,
            review_link   =self._cfg.review_link,
        )

        # ── Clean up whitespace ────────────────────────────────
        # Jinja2 {% if %} blocks can leave extra blank lines.
        # Collapse 3+ consecutive newlines into 2 (one blank line).
        # This keeps the message looking clean in WhatsApp.
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)

        return rendered.strip()

    def build(self, customer: dict) -> tuple:
        """
        Choose a template, render it, return (message, label).

        Selection logic:
          1. Random choice between A and B
          2. If same as last template → flip to the other one
             This prevents the same template being sent to
             3+ consecutive customers without variation
          3. Update self._last

        Args:
            customer: dict from db.get_pending() or DataReader output.
                      Must have "first_name" key at minimum.

        Returns:
            Tuple of (rendered_message_string, template_label)
            template_label is "A" or "B" — save this to the DB.

        Example:
            message, label = builder.build({"first_name": "Blessing"})
            # message → "Hi Blessing 🙏\n\nQuick one ..."
            # label   → "B"
        """
        # Random choice, but flip if it matches the last one used
        choice = random.choice(["A", "B"])
        if choice == self._last:
            choice = "B" if choice == "A" else "A"
        self._last = choice

        message = self._render(choice, customer)

        self._log.debug(
            f"Built Template {choice} for {customer.get('first_name', '?')} "
            f"({len(message)} chars)"
        )

        return message, choice

    def preview(self, customer: dict) -> dict:
        """
        Render BOTH templates for one customer. Used by --dry-run.

        Returns both versions so the user can read them before
        committing to the full send run.

        Args:
            customer: dict with "first_name" key at minimum.

        Returns:
            {"A": "Hi Titilayo! 👋 ...", "B": "Hi Titilayo 🙏 ..."}
        """
        return {
            "A": self._render("A", customer),
            "B": self._render("B", customer),
        }

# ── END OF FILE 9 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 10
#  PATH:  whatsapp_automation/src/whatsapp_sender.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   Defines the abstract interface that ALL WhatsApp sender
#   implementations must follow. Does not send anything itself.
#
# WHY AN ABSTRACT INTERFACE:
#   Today: PlaywrightSender implements this → WhatsApp Web browser
#   Future: CloudAPISender implements this → official WhatsApp API
#   Future: MockSender implements this → for testing without browser
#
#   Every other module (Scheduler, CLI, FastAPI routes) only
#   ever talks to WhatsAppSender — never to PlaywrightSender directly.
#   This means swapping the sending backend requires:
#     1. Writing a new class that extends WhatsAppSender
#     2. Changing one line in main.py (or a FastAPI dependency)
#     3. Nothing else changes anywhere in the system
#
# ALSO DEFINES: SendResult dataclass
#   The standard return type from every send operation.
#   All implementations return this same object so callers
#   can handle results without knowing which sender was used.
#
# FUTURE / FASTAPI NOTE:
#   When building the FastAPI layer, the sender can be injected
#   as a dependency per request or held as an application-level
#   singleton. The abstract interface makes both patterns work:
#
#   Option A (singleton — recommended for Playwright):
#     app.state.sender = PlaywrightSender(cfg)
#     await app.state.sender.connect()
#
#   Option B (dependency injection — better for API sender):
#     async def get_sender() -> WhatsAppSender:
#         return CloudAPISender(cfg)
#
#     @app.post("/send")
#     async def send_msg(sender: WhatsAppSender = Depends(get_sender)):
#         result = await sender.send_text(...)
#
#   No changes to this file needed for either pattern.
#
# TEST AFTER SAVING:
#   python -c "
#   from src.whatsapp_sender import WhatsAppSender, SendResult
#   print('SendResult:', SendResult(success=True, status='SENT'))
#   print('Interface loaded OK')
#   "
# ================================================================
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/whatsapp_sender.py

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


# ==============================================================
# SEND RESULT — Standard return type from every send operation
# ==============================================================
# Using a dataclass ensures every sender implementation returns
# the same structure. The Scheduler reads result.status to decide
# which DB update to make — it never checks which sender was used.
# ==============================================================

@dataclass
class SendResult:
    """
    The outcome of one send attempt.
    Returned by every method in every WhatsAppSender implementation.

    Fields:
        success:         True if the message was delivered.
        status:          One of: 'SENT' | 'FAILED' | 'INVALID_NUMBER'
                         Matches the SendStatus enum values in database.py
                         so the Scheduler can call db.mark_sent() etc.
        error_message:   What went wrong. Empty string on success.
        screenshot_path: Path to proof screenshot. Empty string if none taken.
        timestamp:       When this result was determined.

    Usage by Scheduler:
        result = await sender.send_text(phone, message, order_id)
        if result.status == "SENT":
            db.mark_sent(order_id, message, template, result.screenshot_path)
        elif result.status == "INVALID_NUMBER":
            db.mark_invalid(order_id)
        else:
            db.mark_failed(order_id, result.error_message)
    """
    success:         bool
    status:          str             # 'SENT' | 'FAILED' | 'INVALID_NUMBER'
    error_message:   str = ""        # Empty on success
    screenshot_path: str = ""        # Empty if no screenshot taken
    timestamp:       datetime = field(default_factory=datetime.now)


# ==============================================================
# WHATSAPP SENDER — Abstract interface
# ==============================================================
# Defines the contract that every sender implementation must fulfill.
# The ABC (Abstract Base Class) pattern in Python means:
#   - Any class that extends WhatsAppSender MUST implement all
#     @abstractmethod methods or Python will raise TypeError
#   - This catches missing implementations at startup, not mid-send
# ==============================================================

class WhatsAppSender(ABC):
    """
    Abstract base class for all WhatsApp message senders.

    Current implementation:
        PlaywrightSender (Day 3) — controls Chrome browser via Playwright
        to automate WhatsApp Web. All 8 stealth layers live there.

    Future implementations:
        CloudAPISender — calls the official WhatsApp Business Cloud API.
            To migrate: write CloudAPISender(WhatsAppSender), implement
            all abstract methods below, change one import in main.py.
            Every other file stays identical.

        MockSender — returns fake SendResult objects for testing.
            Useful for running the full scheduling logic without
            needing a real WhatsApp connection or customer data.

    Usage by Scheduler (same regardless of which implementation):
        sender = PlaywrightSender(cfg)       # or CloudAPISender(cfg)
        await sender.connect()
        result = await sender.send_text(phone, message, order_id)
        await sender.disconnect()
    """

    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection to WhatsApp.

        For PlaywrightSender:
            Opens Chromium browser, loads saved session or shows QR code.
            Returns True once the WhatsApp chat list is visible.

        For CloudAPISender (future):
            Validates API credentials, confirms access token is active.
            Returns True if API responds with 200 OK.

        Returns:
            True if connected and ready to send.
            False or raises ConnectionError if connection failed.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Close the connection cleanly.

        For PlaywrightSender:
            Closes the browser context. Does NOT delete .sessions/ folder
            — the saved login must persist for the next run.

        For CloudAPISender (future):
            Revokes or releases any session-scoped resources.

        Called automatically by main.py on Ctrl+C or after --run completes.
        Always called in a finally block so it runs even on crash.
        """
        pass

    @abstractmethod
    async def send_text(
        self,
        phone:    str,
        message:  str,
        order_id: str
    ) -> SendResult:
        """
        Send a plain text message to one WhatsApp number.

        Args:
            phone:    Normalized E.164 phone (no +), e.g. "2348038365784"
            message:  The full message string to send (rendered from template)
            order_id: Customer's order ID — used for screenshot filename
                      and for correlating logs to DB records

        Returns:
            SendResult with status one of:
                "SENT"           — message confirmed delivered
                "FAILED"         — error occurred, eligible for retry
                "INVALID_NUMBER" — phone not registered on WhatsApp, never retry

        Note:
            Must never raise an exception that crashes the caller.
            All errors should be caught internally and returned
            as SendResult(success=False, status="FAILED", error_message=...).
        """
        pass

    @abstractmethod
    async def send_image(
        self,
        phone:      str,
        image_path: str,
        caption:    str,
        order_id:   str
    ) -> SendResult:
        """
        Send an image file with a text caption.

        Args:
            phone:      Normalized E.164 phone (no +)
            image_path: Local file path to the image, e.g. "data/product.jpg"
            caption:    Text to display with the image (rendered from template)
            order_id:   For screenshot naming and log correlation

        Returns:
            Same SendResult as send_text().

        Note:
            Only called when cfg.image_path is not None and the file exists.
            The Scheduler checks this before deciding which method to call.
        """
        pass

    @abstractmethod
    async def is_connected(self) -> bool:
        """
        Check whether the current session is still alive.

        For PlaywrightSender:
            Queries the page for the WhatsApp chat list element.
            Returns False if the session expired (user logged out,
            browser crashed, etc.).

        For CloudAPISender (future):
            Makes a lightweight API call to verify token is still valid.

        Called by the Scheduler at the start of each session.
        If False: Scheduler attempts to reconnect before proceeding.

        Returns:
            True  → session is alive, safe to send
            False → session needs reconnection (call connect() again)
        """
        pass

# ── END OF FILE 10 ────────────────────────────────────────────


# ==============================================================
# DAY 2 VERIFICATION COMMANDS
# ==============================================================
# Run these from your project root after saving all 5 files.
# All must pass before starting Day 3.
#
# ── Test FILE 6 (data_reader) ──────────────────────────────────
# Drop your customers.xlsx into data/ first, then:
#
#   python -c "
#   from src.config import AppConfig
#   from src.data_reader import DataReader
#   cfg       = AppConfig()
#   reader    = DataReader(cfg.country_code)
#   customers = reader.read_and_filter(cfg.excel_path(), cfg.target_product)
#   print(f'Customers found: {len(customers)}')
#   if customers:
#       c = customers[0]
#       print(f'Sample name:  {c[\"first_name\"]}')
#       print(f'Sample phone: {c[\"normalized_phone\"]}')
#       print(f'Phone valid:  {c[\"phone_valid\"]}')
#   "
#   Expected: Customers found: 96
#
# ── Test FILE 6 + FILE 5 (import into database) ────────────────
#
#   python -c "
#   from src.config import AppConfig
#   from src.database import Database
#   from src.data_reader import DataReader
#   cfg       = AppConfig()
#   db        = Database(cfg.database_url)
#   db.init()
#   reader    = DataReader(cfg.country_code)
#   customers = reader.read_and_filter(cfg.excel_path(), cfg.target_product)
#   for c in customers:
#       db.upsert_customer(c)
#   stats = db.get_stats()
#   print('DB stats:', stats)
#   "
#   Expected: {'total': 96, 'pending': 96, 'sent': 0, ...}
#
# ── Test FILE 9 (message_builder) ─────────────────────────────
# Templates must exist first (files 7 and 8 saved). Then:
#
#   python -c "
#   from src.config import AppConfig
#   from src.message_builder import MessageBuilder
#   cfg     = AppConfig()
#   builder = MessageBuilder(cfg)
#   sample  = {'first_name': 'Titilayo'}
#   for i in range(4):
#       msg, label = builder.build(sample)
#       print(f'--- Template {label} ---')
#       print(msg[:80], '...')
#       print()
#   "
#   Expected: alternating A and B labels, never same 3× in a row
#
# ── Test FILE 10 (whatsapp_sender interface) ───────────────────
#
#   python -c "
#   from src.whatsapp_sender import WhatsAppSender, SendResult
#   r = SendResult(success=True, status='SENT')
#   print('SendResult OK:', r)
#   print('WhatsAppSender is abstract:', hasattr(WhatsAppSender, '__abstractmethods__'))
#   "
#   Expected:
#     SendResult OK: SendResult(success=True, status='SENT', ...)
#     WhatsAppSender is abstract: True
#
# ── IF ALL PASS — ready for Day 3 ─────────────────────────────
#
# Day 3 will build:
#   FILE 11 → src/playwright_sender.py  (all 8 stealth layers)
#   FILE 12 → src/scheduler.py          (6 sessions, human delays)
#   FILE 13 → src/reporter.py           (daily report + email)
#   FILE 14 → main.py                   (CLI: setup/preview/dry-run/run)
#
# That completes the full working system.
# After Day 3 you will be able to:
#   python main.py --setup     (import Excel, scan QR)
#   python main.py --preview   (see customer count)
#   python main.py --dry-run   (read exact messages)
#   python main.py --run --now --count 3   (send 3 test messages)
#   python main.py --run       (full scheduled day)
# ==============================================================
