
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ==============================================================
# USER CONFIGURATION — EDIT THIS SECTION
# ==============================================================
# This is the only place you need to make changes.
# Search for EDIT ME to find every field that needs your input.
# Everything else in the system reads from this dict.
# ==============================================================

CONFIG = {

    # ── PRODUCT TARGETING ──────────────────────────────────────
    # Only customers whose Product column contains this keyword
    # (case-insensitive) will be messaged. All others are skipped.
    # Your Excel has three Sadoer variants — "sadoer" catches all.
    "target_product": "sadoer",  # EDIT ME — change for a different product

    # ── SEND ORDER ─────────────────────────────────────────────
    # Determines which customers get messaged first each day.
    # "recent_first" is safest — recent buyers remember you best
    # and are least likely to block or report your message.
    # Options: "recent_first" | "oldest_first" | "random"
    "send_order": "recent_first",

    # ── DAILY LIMIT ────────────────────────────────────────────
    # Maximum messages to send per day across all 6 sessions.
    # Start at 50 for your first run. Increase to 100 after confirming
    # messages are landing well and reply rate looks healthy.
    # Never set above 200 — unnecessary risk with no benefit.
    "daily_limit": 50,

    # ── SESSION SCHEDULE ───────────────────────────────────────
    # 6 sessions spread across a natural working day.
    # The count values must add up to daily_limit (6×8=48, 6×9=54 ≈ 50).
    # Adjust times to match your actual working hours.
    # Times are in 24-hour format: "08:15" = 8:15 AM.
    "session_schedule": [
        {"time": "08:15", "count": 8},
        {"time": "09:45", "count": 8},
        {"time": "11:30", "count": 8},
        {"time": "13:15", "count": 8},
        {"time": "15:30", "count": 9},
        {"time": "17:00", "count": 9},
    ],

    # ── HUMAN-LIKE DELAYS (all values in SECONDS) ──────────────
    # The system picks a RANDOM value within each range every time.
    # Never set min == max — a perfectly uniform delay looks like a bot.
    # These ranges are tuned for low detection risk with 50-100 msgs/day.
    "delays": {
        # Gap between individual messages within a session burst
        "between_messages_min": 55,    # Minimum seconds between messages
        "between_messages_max": 110,   # Maximum seconds between messages

        # Longer pause after every N messages (a "burst")
        # Simulates: someone pausing to do something else
        "after_burst_min": 240,        # 4 minutes minimum
        "after_burst_max": 480,        # 8 minutes maximum
        "burst_size": 4,               # Number of messages before burst pause

        # Random ± seconds added to every delay for unpredictability
        "jitter_min": -20,
        "jitter_max": 30,

        # Once per session: a random long pause simulating distraction
        # Only fires 30% of the time and never near end of session
        "long_pause_enabled": True,
        "long_pause_min": 480,         # 8 minutes
        "long_pause_max": 900,         # 15 minutes

        # How long to wait after navigating to a chat before typing
        # Simulates: user reads the chat before starting to reply
        "pre_type_min": 3,
        "pre_type_max": 8,

        # Milliseconds per character when typing a message
        # Real human typing: 40-100ms per character with variation
        "type_speed_min": 40,
        "type_speed_max": 100,
    },

    # ── MESSAGE SETTINGS ───────────────────────────────────────
    # image_path: set to None for text-only messages (recommended to start).
    # Set to a file path like "data/product_image.jpg" to send image + caption.
    "image_path": None,

    # Optional review link added to the message. Leave "" if you don't have one.
    "review_link": "",

    # The discount offer line in the message. Change this to match your offer.
    "discount_offer": "10% off your next order",  # EDIT ME

    # ── EMAIL REPORT SETTINGS ──────────────────────────────────
    # After the last session each day, a summary report is emailed to you.
    # Leave smtp_password as "" to disable email reports for now.
    # When ready: use a Gmail App Password, NOT your normal Gmail password.
    # How to get one: myaccount.google.com → Security → App Passwords
    "smtp_email":    "youremail@gmail.com",   # EDIT ME
    "smtp_password": "",                       # EDIT ME (Gmail App Password)
    "smtp_to":       "youremail@gmail.com",   # EDIT ME

    # ── DATABASE ───────────────────────────────────────────────
    # SQLite runs locally with zero setup. File is created automatically.
    # To switch to PostgreSQL later: change only this URL. Nothing else changes.
    "database_url": "sqlite:///data/automation.db",

    # ── PHONE NORMALIZATION ────────────────────────────────────
    # Country calling code for your customers.
    # Nigeria = 234. Ghana = 233. UK = 44. Kenya = 254.
    # Change this when selling this tool to businesses in other countries.
    "country_code": "234",  # EDIT ME if your customers are in another country

    # ── EXCEL FILE ─────────────────────────────────────────────
    # The exact filename of your Excel file inside the data/ folder.
    # Drop the file in data/ and make sure this name matches exactly.
    "excel_filename": "customers.xlsx",  # EDIT ME if your file has a different name
}


# ==============================================================
# DELAY CONFIG — Typed wrapper for the delays sub-dict
# ==============================================================
# Separating delays into their own class makes the code cleaner
# and allows: cfg.delays.burst_size instead of cfg["delays"]["burst_size"]
# ==============================================================

class DelayConfig:
    """
    Typed wrapper for all delay settings.
    Accessed from AppConfig as: cfg.delays.between_messages_min
    """

    def __init__(self, d: dict):
        # Between-message delays
        self.between_messages_min = d["between_messages_min"]
        self.between_messages_max = d["between_messages_max"]

        # Burst pause (after every burst_size messages)
        self.after_burst_min  = d["after_burst_min"]
        self.after_burst_max  = d["after_burst_max"]
        self.burst_size       = d["burst_size"]

        # Random jitter applied to every delay
        self.jitter_min = d["jitter_min"]
        self.jitter_max = d["jitter_max"]

        # Optional long pause once per session
        self.long_pause_enabled = d["long_pause_enabled"]
        self.long_pause_min     = d["long_pause_min"]
        self.long_pause_max     = d["long_pause_max"]

        # Pre-typing pause (simulates reading before replying)
        self.pre_type_min = d["pre_type_min"]
        self.pre_type_max = d["pre_type_max"]

        # Typing speed per character (milliseconds)
        self.type_speed_min = d["type_speed_min"]
        self.type_speed_max = d["type_speed_max"]


# ==============================================================
# APP CONFIG — Main configuration object
# ==============================================================
# All other modules import this class and receive an instance.
# None of them read the CONFIG dict directly.
# This keeps the system portable — you can swap CONFIG for a
# YAML file later by changing only this class.
# ==============================================================

"""
    # ── PERSONAL WHATSAPP ──────────────────────────────────────
    # Your personal WhatsApp number to receive the daily Excel report.
    # Format: country code + number, no + sign, no spaces.
    # Nigeria example: "2348012345678"
    # Leave as "" to skip WhatsApp delivery (report saved locally only).
    "personal_whatsapp": "2348XXXXXXXXX",  # EDIT ME
"""

class AppConfig:
    """
    Typed, validated configuration object built from the CONFIG dict.

    All other modules receive an instance of this class.
    None of them read CONFIG directly — all settings go through here.

    Usage from other modules:
        from src.config import AppConfig
        cfg = AppConfig()

        cfg.target_product        → "sadoer"
        cfg.daily_limit           → 50
        cfg.delays.burst_size     → 4
        cfg.excel_path()          → Path("data/customers.xlsx")
        cfg.has_email()           → True or False
        cfg.session_jobs()        → [{"hour": 8, "minute": 15, "count": 8}, ...]
        cfg.total_daily_count()   → 50
    """

    def __init__(self, raw: dict = None):
        # Use the module-level CONFIG if no dict is passed
        # Passing a dict is useful for testing with different settings
        raw = raw or CONFIG
        
        self.personal_whatsapp = str(raw.get("personal_whatsapp", "")).strip()


        # Validate first — fail fast with a clear message
        self._validate(raw)

        # Core settings
        self.personal_whatsapp = str(raw.get("personal_whatsapp", "")).strip()

        self.target_product   = str(raw["target_product"]).lower().strip()
        self.send_order       = str(raw["send_order"])
        self.daily_limit      = int(raw["daily_limit"])
        self.session_schedule = raw["session_schedule"]

        # Nested delay config gets its own typed object
        self.delays = DelayConfig(raw["delays"])

        # Message settings
        self.image_path     = raw.get("image_path")       # None or file path string
        self.review_link    = str(raw.get("review_link", ""))
        self.discount_offer = str(raw["discount_offer"])

        # Email settings
        self.smtp_email    = str(raw.get("smtp_email", "")).strip()
        self.smtp_password = str(raw.get("smtp_password", "")).strip()
        self.smtp_to       = str(raw.get("smtp_to", "")).strip()

        # Infrastructure settings
        self.database_url  = str(raw["database_url"])
        self.country_code  = str(raw["country_code"])
        self.excel_filename = str(raw["excel_filename"])

        logger.debug(f"AppConfig loaded: {self}")

    def _validate(self, raw: dict):
        """
        Check for required fields before the system starts.
        Raises ValueError with a clear message instead of a cryptic crash
        later when a missing value causes an AttributeError mid-session.
        """
        required_fields = [
            "target_product",
            "daily_limit",
            "session_schedule",
            "delays",
            "discount_offer",
            "database_url",
            "country_code",
            "excel_filename",
        ]

        for field in required_fields:
            if field not in raw:
                raise ValueError(
                    f"Missing required config field: '{field}'.\n"
                    f"Check the CONFIG dict in src/config.py."
                )

        # Type checks for critical numeric fields
        if not isinstance(raw["daily_limit"], int) or raw["daily_limit"] < 1:
            raise ValueError(
                f"daily_limit must be a positive integer. Got: {raw['daily_limit']!r}"
            )

        if not isinstance(raw["session_schedule"], list) or not raw["session_schedule"]:
            raise ValueError(
                "session_schedule must be a non-empty list of session dicts."
            )

        # Warn if session counts don't add up to daily_limit
        total = sum(s.get("count", 0) for s in raw["session_schedule"])
        if total != raw["daily_limit"]:
            logger.warning(
                f"session_schedule counts sum to {total} "
                f"but daily_limit is {raw['daily_limit']}. "
                f"The lower value will be used."
            )

    def excel_path(self) -> Path:
        """
        Returns the full Path object to the Excel file.
        Always use this instead of building the path manually elsewhere.

        Example: cfg.excel_path() → Path("data/customers.xlsx")
        """
        return Path("data") / self.excel_filename

    def has_email(self) -> bool:
        """
        Returns True only if both smtp_email AND smtp_password are configured.
        Used by the reporter to decide whether to send the email report.

        Example:
            cfg.has_email() → False   (if smtp_password is "")
            cfg.has_email() → True    (if both are filled in)
        """
        return bool(self.smtp_email and self.smtp_password)

    def session_jobs(self) -> list:
        """
        Parses the time strings in session_schedule into APScheduler-ready dicts.
        Called by the Scheduler class when registering cron jobs.

        Input:  [{"time": "08:15", "count": 8}, ...]
        Output: [{"hour": 8, "minute": 15, "count": 8}, ...]
        """
        jobs = []
        for session in self.session_schedule:
            hour, minute = session["time"].split(":")
            jobs.append({
                "hour":   int(hour),
                "minute": int(minute),
                "count":  int(session["count"]),
            })
        return jobs

    def total_daily_count(self) -> int:
        """
        Returns the sum of all session counts.
        Used in --preview to show total messages planned for today.

        Example: total_daily_count() → 50
        """
        return sum(s["count"] for s in self.session_schedule)

    def __repr__(self) -> str:
        """
        Safe string representation — masks smtp_password so it never
        appears in logs even at DEBUG level.
        """
        return (
            f"AppConfig("
            f"product={self.target_product!r}, "
            f"limit={self.daily_limit}, "
            f"email={'configured' if self.has_email() else 'not configured'}"
            f")"
        )
    def has_personal_whatsapp(self) -> bool:
        """ True if a personal WhatsApp number is configured for report delivery """      
        return bool(self.personal_whatsapp)