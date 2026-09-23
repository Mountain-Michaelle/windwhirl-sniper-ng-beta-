# ==============================================================
# WINDWHIRL OMS — DAY 1: PROJECT FOUNDATION
# ==============================================================
# This document contains all files for Day 1.
# Build them in order. Nothing beyond this milestone today.
#
# WHAT THIS OMS DOES (your actual use case):
#   Monitors a WhatsApp group where orders come in for a specific
#   staff member. Detects new orders, tracks their status, and
#   keeps everything organised.
#
# HOW IT RELATES TO THE REVIEW AUTOMATION:
#   - Completely separate module: windwhirl/app/oms/
#   - Review automation lives: windwhirl/apps/
#   - They share nothing yet — clean separation
#   - Future: they can share the browser session (Day 5+)
#
# FOLDER STRUCTURE TO CREATE TODAY:
#
#   windwhirl/
#   └── app/
#       └── oms/
#           ├── __init__.py
#           ├── config/
#           │   ├── __init__.py
#           │   └── settings.py
#           ├── domain/
#           │   ├── __init__.py
#           │   ├── entities.py
#           │   ├── interfaces.py
#           │   └── exceptions.py
#           ├── application/
#           │   ├── __init__.py
#           │   └── services.py
#           ├── infrastructure/
#           │   └── __init__.py
#           ├── events/
#           │   ├── __init__.py
#           │   └── dispatcher.py
#           ├── repositories/
#           │   ├── __init__.py
#           │   └── interfaces.py
#           ├── shared/
#           │   ├── __init__.py
#           │   ├── logger.py
#           │   └── exceptions.py
#           └── tests/
#               └── __init__.py
#
# BUILD ORDER:
#   FILE 1  → app/oms/__init__.py
#   FILE 2  → app/oms/shared/exceptions.py
#   FILE 3  → app/oms/shared/logger.py
#   FILE 4  → app/oms/shared/__init__.py
#   FILE 5  → app/oms/config/settings.py
#   FILE 6  → app/oms/config/__init__.py
#   FILE 7  → app/oms/domain/exceptions.py
#   FILE 8  → app/oms/domain/entities.py
#   FILE 9  → app/oms/domain/interfaces.py
#   FILE 10 → app/oms/domain/__init__.py
#   FILE 11 → app/oms/events/dispatcher.py
#   FILE 12 → app/oms/events/__init__.py
#   FILE 13 → app/oms/repositories/interfaces.py
#   FILE 14 → app/oms/repositories/__init__.py
#   FILE 15 → app/oms/application/services.py
#   FILE 16 → app/oms/application/__init__.py
#   FILE 17 → app/oms/infrastructure/__init__.py
#   FILE 18 → app/oms/tests/__init__.py
#
# VERIFICATION (run after all files saved):
#   python -c "from app.oms.config.settings import OMSSettings; print(OMSSettings())"
#   python -c "from app.oms.shared.logger import get_logger; get_logger('test').info('OK')"
#   python -c "from app.oms.events.dispatcher import EventDispatcher; print('Events OK')"
#
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH: windwhirl/app/oms/__init__.py
# ================================================================
# Marks oms/ as a Python package.
# Exposes the OMS version for diagnostics.
# ================================================================
# ==============================================================

"""
__version__ = "0.1.0"
__description__ = "Windwhirl Order Management System"
"""


# ==============================================================
# ================================================================
#  FILE 2
#  PATH: windwhirl/app/oms/shared/exceptions.py
# ================================================================
# Base exceptions for the entire OMS.
# All other exceptions in the system inherit from these.
#
# WHY A HIERARCHY:
#   catch OMSException          → catch anything OMS-related
#   catch InfrastructureException → catch browser/sheet/db errors only
#   catch ValidationException   → catch bad order data only
#   This lets callers be as specific or broad as needed.
# ================================================================
# ==============================================================

"""
class OMSException(Exception):
    '''
    Root exception for the entire OMS system.
    All OMS-specific exceptions inherit from this.
    Catch this to handle any OMS error in one place.
    '''
    def __init__(self, message: str, context: dict = None):
        super().__init__(message)
        self.message = message
        # Optional dict of extra context for logging/debugging
        # Example: {"order_id": "123", "group": "Nabeau Orders"}
        self.context = context or {}

    def __str__(self):
        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{self.message} [{ctx}]"
        return self.message


class ConfigurationException(OMSException):
    '''
    Raised when configuration is missing, invalid, or inconsistent.
    Examples:
        - Required setting not provided
        - Invalid timezone value
        - Incompatible setting combination
    '''
    pass


class InfrastructureException(OMSException):
    '''
    Raised when an external system fails.
    Examples:
        - Browser cannot connect to WhatsApp Web
        - Google Sheets API returns an error
        - Database connection fails
    Infrastructure errors are usually transient — they may
    resolve on retry. Business logic should not depend on them.
    '''
    pass


class ValidationException(OMSException):
    '''
    Raised when data does not meet business rules.
    Examples:
        - Order message missing required fields
        - Phone number in wrong format
        - Order total is negative
    Validation errors are not retryable — the data itself is bad.
    '''
    def __init__(self, message: str, field: str = None, context: dict = None):
        super().__init__(message, context)
        # The specific field that failed validation (if applicable)
        self.field = field
"""


# ==============================================================
# ================================================================
#  FILE 3
#  PATH: windwhirl/app/oms/shared/logger.py
# ================================================================
# Single global logging service for the entire OMS.
# Every module calls get_logger(__name__) — nothing else.
# Never use print() anywhere in the OMS codebase.
#
# DESIGN:
#   One function: get_logger(name) → returns a configured logger.
#   The root OMS logger is configured once when first called.
#   All child loggers inherit its handlers automatically.
#   This means every module's logger writes to the same file.
# ================================================================
# ==============================================================

"""
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

# Track whether the root OMS logger has been configured
# Prevents duplicate handlers if get_logger is called multiple times
_configured = False


def get_logger(name: str, log_dir: Optional[str] = None) -> logging.Logger:
    '''
    Get a named logger for an OMS module.

    First call configures the root "oms" logger with console and
    rotating file handlers. All subsequent calls return child loggers
    that inherit those handlers automatically.

    Usage in any OMS module:
        from app.oms.shared.logger import get_logger
        log = get_logger(__name__)
        log.info("Order received")
        log.warning("Duplicate detected")
        log.error("Browser failed", exc_info=True)

    Args:
        name:    Module name. Always pass __name__ for correct attribution.
        log_dir: Optional path to log directory.
                 Defaults to "logs/" relative to working directory.
                 Only used on first call — subsequent calls ignore it.

    Returns:
        A configured logging.Logger instance.
    '''
    global _configured

    if not _configured:
        _configure_root_logger(log_dir)
        _configured = True

    # Return a child logger named "oms.{module_name}"
    # e.g. "oms.app.oms.domain.entities"
    # This appears in log files so you can filter by module
    if name.startswith("app.oms."):
        logger_name = "oms." + name[len("app.oms."):]
    elif name == "__main__":
        logger_name = "oms.main"
    else:
        logger_name = f"oms.{name}"

    return logging.getLogger(logger_name)


def _configure_root_logger(log_dir: Optional[str] = None):
    '''
    Configure the root "oms" logger with console and file handlers.
    Called once automatically by get_logger() on first use.
    '''
    log_path = Path(log_dir) if log_dir else Path("logs")
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("oms")
    root.setLevel(logging.DEBUG)

    # Prevent propagation to root Python logger
    # (avoids duplicate output if other loggers are configured)
    root.propagate = False

    # ── Console handler ─────────────────────────────────────────
    # INFO and above — clean, readable output for the operator
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%H:%M:%S"
    ))
    root.addHandler(console)

    # ── Rotating file handler ───────────────────────────────────
    # DEBUG and above — full trace for troubleshooting
    # 5MB per file, keeps 5 backups → up to 30MB of logs retained
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "oms.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root.addHandler(file_handler)
"""


# ==============================================================
# ================================================================
#  FILE 4
#  PATH: windwhirl/app/oms/shared/__init__.py
# ================================================================
# Makes shared/ importable. Exposes key shared utilities.
# ================================================================
# ==============================================================

"""
from app.oms.shared.logger import get_logger
from app.oms.shared.exceptions import (
    OMSException,
    ConfigurationException,
    InfrastructureException,
    ValidationException,
)

__all__ = [
    "get_logger",
    "OMSException",
    "ConfigurationException",
    "InfrastructureException",
    "ValidationException",
]
"""


# ==============================================================
# ================================================================
#  FILE 5
#  PATH: windwhirl/app/oms/config/settings.py
# ================================================================
# All OMS configuration in one place.
# Nothing is hardcoded anywhere else in the system.
# Future modules read their config from here — never from env
# directly, never from hardcoded strings.
#
# STRUCTURE:
#   OMSSettings is the single config object.
#   Every section has its own dataclass (WhatsAppSettings, etc.)
#   for clean grouping and IDE autocomplete.
#
# HOW TO USE:
#   from app.oms.config.settings import get_settings
#   cfg = get_settings()
#   cfg.whatsapp.group_name       → "Nabeau Orders Group"
#   cfg.whatsapp.staff_number     → "2348XXXXXXXXX"
#   cfg.browser.session_dir       → ".sessions/oms_session"
# ================================================================
# ==============================================================

"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.oms.shared.exceptions import ConfigurationException
from app.oms.shared.logger import get_logger

log = get_logger(__name__)

# Module-level singleton — settings loaded once, reused everywhere
_settings_instance: Optional["OMSSettings"] = None


# ==============================================================
# SECTION DATACLASSES
# Each section groups related settings.
# Add new settings here — never scatter them across the codebase.
# ==============================================================

@dataclass
class WhatsAppSettings:
    '''
    Settings for WhatsApp group monitoring.

    group_name:    Exact name of the WhatsApp group to monitor.
                   Must match the group name character for character.

    staff_number:  The WhatsApp number of the staff member whose
                   orders this OMS tracks. Format: 234XXXXXXXXXX
                   (13 digits, no +, no spaces)

    monitor_number: The WhatsApp number logged in on the browser.
                   Usually the business number or your personal number.

    poll_interval: How many seconds to wait between checking for
                   new messages in the group. Default: 15 seconds.
                   Lower = more responsive but more browser activity.
                   Higher = less activity but slower detection.

    message_lookback: How many recent messages to scan when checking
                      for new orders. Default: 20.
    '''
    group_name:       str   = ""
    staff_number:     str   = ""
    monitor_number:   str   = ""
    poll_interval:    int   = 15
    message_lookback: int   = 20


@dataclass
class BrowserSettings:
    '''
    Settings for the Playwright browser used by the OMS.
    Note: This is a SEPARATE browser session from the review automation.
    Each system maintains its own persistent session.

    session_dir:   Where to save the browser login session.
                   Default: ".sessions/oms_session"

    headless:      Run browser without visible window.
                   False during development (you can see what it does).
                   True in production (background operation).

    viewport_w/h:  Browser window dimensions.
                   1280x800 is a common laptop resolution.

    timezone:      Browser timezone. Must match your location.
                   Nigeria: "Africa/Lagos"
    '''
    session_dir: str = ".sessions/oms_session"
    headless:    bool = False
    viewport_w:  int  = 1280
    viewport_h:  int  = 800
    timezone:    str  = "Africa/Lagos"
    locale:      str  = "en-US"


@dataclass
class StorageSettings:
    '''
    Settings for order data storage.
    Starts with SQLite (local file). PostgreSQL-ready via URL swap.

    database_url:  SQLAlchemy connection URL.
                   SQLite:     "sqlite:///data/oms.db"
                   PostgreSQL: "postgresql://user:pass@host/oms"

    orders_file:   Path to save order export files (CSV/Excel).
    '''
    database_url: str = "sqlite:///data/oms.db"
    orders_file:  str = "data/orders_export.xlsx"


@dataclass
class SheetsSettings:
    '''
    Settings for Google Sheets synchronisation.
    Leave spreadsheet_id empty to disable sheets sync.

    spreadsheet_id: The Google Sheets document ID from its URL.
                    Example: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    sheet_name:     Name of the tab/sheet to write orders to.

    credentials_file: Path to the Google service account JSON key file.
    '''
    spreadsheet_id:   str = ""
    sheet_name:       str = "Orders"
    credentials_file: str = "config/google_credentials.json"


@dataclass
class RetrySettings:
    '''
    Settings for retry behaviour across all infrastructure operations.

    max_retries:  Maximum number of retry attempts before giving up.
    retry_delay:  Seconds to wait between retries.
                  Uses exponential backoff: delay * (attempt + 1)
    '''
    max_retries: int   = 3
    retry_delay: float = 5.0


@dataclass
class LoggingSettings:
    '''
    Logging configuration for the OMS.

    log_dir:   Directory where log files are written.
    log_level: Minimum level to log. Options: DEBUG, INFO, WARNING, ERROR
    '''
    log_dir:   str = "logs"
    log_level: str = "INFO"


# ==============================================================
# MAIN SETTINGS CLASS
# ==============================================================

@dataclass
class OMSSettings:
    '''
    Root configuration object for the OMS.
    Every future module reads settings from this — never from
    environment variables or hardcoded values directly.

    Usage:
        from app.oms.config.settings import get_settings
        cfg = get_settings()
        cfg.whatsapp.group_name
        cfg.browser.headless
        cfg.storage.database_url

    To configure the OMS, edit the values in this file or
    set environment variables (see _load_from_env below).
    '''

    # Application identity
    app_name:    str = "Windwhirl OMS"
    environment: str = "development"   # "development" | "production"

    # Section settings — each section is its own typed object
    whatsapp: WhatsAppSettings = field(default_factory=WhatsAppSettings)
    browser:  BrowserSettings  = field(default_factory=BrowserSettings)
    storage:  StorageSettings  = field(default_factory=StorageSettings)
    sheets:   SheetsSettings   = field(default_factory=SheetsSettings)
    retry:    RetrySettings    = field(default_factory=RetrySettings)
    logging:  LoggingSettings  = field(default_factory=LoggingSettings)

    def __post_init__(self):
        '''Load from environment variables after dataclass init.'''
        self._load_from_env()
        log.debug(f"OMSSettings loaded: env={self.environment}")

    def _load_from_env(self):
        '''
        Override settings from environment variables if set.
        This allows production deployment without editing source code.

        Environment variable naming:
            OMS_WHATSAPP_GROUP_NAME
            OMS_WHATSAPP_STAFF_NUMBER
            OMS_BROWSER_HEADLESS
            OMS_STORAGE_DATABASE_URL
            etc.
        '''
        env_map = {
            "OMS_APP_NAME":                 ("app_name",                  str),
            "OMS_ENVIRONMENT":              ("environment",               str),
            "OMS_WHATSAPP_GROUP_NAME":      ("whatsapp.group_name",       str),
            "OMS_WHATSAPP_STAFF_NUMBER":    ("whatsapp.staff_number",     str),
            "OMS_WHATSAPP_MONITOR_NUMBER":  ("whatsapp.monitor_number",   str),
            "OMS_WHATSAPP_POLL_INTERVAL":   ("whatsapp.poll_interval",    int),
            "OMS_BROWSER_HEADLESS":         ("browser.headless",          bool),
            "OMS_BROWSER_SESSION_DIR":      ("browser.session_dir",       str),
            "OMS_BROWSER_TIMEZONE":         ("browser.timezone",          str),
            "OMS_STORAGE_DATABASE_URL":     ("storage.database_url",      str),
            "OMS_SHEETS_SPREADSHEET_ID":    ("sheets.spreadsheet_id",     str),
            "OMS_RETRY_MAX_RETRIES":        ("retry.max_retries",         int),
            "OMS_RETRY_DELAY":              ("retry.retry_delay",         float),
            "OMS_LOGGING_LOG_DIR":          ("logging.log_dir",           str),
            "OMS_LOGGING_LOG_LEVEL":        ("logging.log_level",         str),
        }

        for env_key, (attr_path, cast) in env_map.items():
            value = os.environ.get(env_key)
            if value is not None:
                parts = attr_path.split(".")
                if len(parts) == 1:
                    setattr(self, parts[0], cast(value))
                elif len(parts) == 2:
                    section = getattr(self, parts[0])
                    # Handle bool from string
                    if cast == bool:
                        value = value.lower() in ("true", "1", "yes")
                    else:
                        value = cast(value)
                    setattr(section, parts[1], value)

    def validate(self):
        '''
        Validate that required settings are present.
        Call this at application startup — fail fast if misconfigured.

        Raises:
            ConfigurationException if required settings are missing.
        '''
        errors = []

        if not self.whatsapp.group_name:
            errors.append(
                "whatsapp.group_name is required. "
                "Set OMS_WHATSAPP_GROUP_NAME or edit settings.py."
            )

        if not self.whatsapp.staff_number:
            errors.append(
                "whatsapp.staff_number is required. "
                "Set OMS_WHATSAPP_STAFF_NUMBER or edit settings.py."
            )

        if self.whatsapp.staff_number:
            num = self.whatsapp.staff_number.replace("+", "").replace(" ", "")
            if not num.startswith("234") or len(num) != 13:
                errors.append(
                    f"whatsapp.staff_number must be 13 digits starting with 234. "
                    f"Got: {self.whatsapp.staff_number!r}"
                )

        if errors:
            raise ConfigurationException(
                f"OMS configuration has {len(errors)} error(s):\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

        log.info(
            f"Configuration validated. "
            f"Group: {self.whatsapp.group_name!r}, "
            f"Staff: +{self.whatsapp.staff_number}"
        )

    def is_production(self) -> bool:
        '''True if running in production environment.'''
        return self.environment.lower() == "production"

    def has_sheets(self) -> bool:
        '''True if Google Sheets sync is configured.'''
        return bool(self.sheets.spreadsheet_id)

    def __repr__(self):
        return (
            f"OMSSettings("
            f"env={self.environment!r}, "
            f"group={self.whatsapp.group_name!r}, "
            f"headless={self.browser.headless}"
            f")"
        )


def get_settings() -> OMSSettings:
    '''
    Get the singleton OMSSettings instance.
    Creates it on first call, returns cached instance on subsequent calls.

    Usage:
        from app.oms.config.settings import get_settings
        cfg = get_settings()
    '''
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = OMSSettings()
    return _settings_instance


def reset_settings():
    '''
    Clear the cached settings instance.
    Used in tests to reset state between test cases.
    '''
    global _settings_instance
    _settings_instance = None
"""


# ==============================================================
# ================================================================
#  FILE 6
#  PATH: windwhirl/app/oms/config/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.config.settings import OMSSettings, get_settings, reset_settings

__all__ = ["OMSSettings", "get_settings", "reset_settings"]
"""


# ==============================================================
# ================================================================
#  FILE 7
#  PATH: windwhirl/app/oms/domain/exceptions.py
# ================================================================
# Domain-specific exceptions. These represent business rule failures.
# They inherit from shared base exceptions but carry domain meaning.
# ================================================================
# ==============================================================

"""
from app.oms.shared.exceptions import OMSException, ValidationException


class OrderException(OMSException):
    '''
    Raised when an order-related business rule is violated.
    Examples:
        - Order already exists (duplicate)
        - Order in wrong state for the requested transition
        - Order belongs to a different staff member
    '''
    def __init__(self, message: str, order_id: str = None, context: dict = None):
        ctx = context or {}
        if order_id:
            ctx["order_id"] = order_id
        super().__init__(message, ctx)
        self.order_id = order_id


class DuplicateOrderException(OrderException):
    '''
    Raised when the same order is detected more than once.
    This is a normal business event — not a system error.
    The OMS should log it and skip, not crash.
    '''
    pass


class OrderParseException(ValidationException):
    '''
    Raised when a WhatsApp message cannot be parsed into an order.
    This means the message exists but its content does not match
    the expected order format.
    '''
    def __init__(self, message: str, raw_text: str = None, context: dict = None):
        ctx = context or {}
        if raw_text:
            # Store first 100 chars of the raw message for debugging
            ctx["raw_text_preview"] = raw_text[:100]
        super().__init__(message, context=ctx)
        self.raw_text = raw_text


class GroupNotFoundException(OMSException):
    '''
    Raised when the target WhatsApp group cannot be found.
    Could mean: wrong group name, not a member, group was deleted.
    '''
    def __init__(self, group_name: str):
        super().__init__(
            f"WhatsApp group not found: {group_name!r}",
            context={"group_name": group_name}
        )
        self.group_name = group_name


class StaffNotFoundException(OMSException):
    '''
    Raised when the configured staff number is not in the group.
    '''
    def __init__(self, staff_number: str, group_name: str):
        super().__init__(
            f"Staff number +{staff_number} not found in group {group_name!r}",
            context={"staff_number": staff_number, "group_name": group_name}
        )
"""


# ==============================================================
# ================================================================
#  FILE 8
#  PATH: windwhirl/app/oms/domain/entities.py
# ================================================================
# Business entities — the core data structures of the OMS.
# These are pure Python dataclasses with no framework dependency.
# No database imports. No Playwright. Just data and business rules.
#
# ENTITIES:
#   Order     — a single customer order detected from the group
#   Message   — a raw WhatsApp message from the group
#   Staff     — the staff member this OMS instance tracks
#
# ENUMS:
#   OrderStatus — the lifecycle states an order moves through
# ================================================================
# ==============================================================

"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderStatus(str, Enum):
    '''
    The complete lifecycle of an order in the OMS.

    DETECTED     → Message was found in the group and parsed as an order.
                   Not yet confirmed or assigned.

    CONFIRMED    → Order details verified. Staff has acknowledged it.

    IN_PROGRESS  → Staff is actively handling this order.

    DISPATCHED   → Order has been sent out for delivery.

    DELIVERED    → Order successfully delivered to customer.

    CANCELLED    → Order was cancelled (customer or staff decision).

    FAILED       → Order could not be fulfilled (out of stock, no contact, etc.)

    Using (str, Enum) means the value stores as a plain string in the database
    and in logs — no special serialization needed.
    '''
    DETECTED    = "DETECTED"
    CONFIRMED   = "CONFIRMED"
    IN_PROGRESS = "IN_PROGRESS"
    DISPATCHED  = "DISPATCHED"
    DELIVERED   = "DELIVERED"
    CANCELLED   = "CANCELLED"
    FAILED      = "FAILED"

    def can_transition_to(self, new_status: "OrderStatus") -> bool:
        '''
        Business rule: which status transitions are valid.
        Prevents an order jumping from DETECTED directly to DELIVERED
        without going through the proper steps.

        Valid transitions:
            DETECTED    → CONFIRMED, CANCELLED
            CONFIRMED   → IN_PROGRESS, CANCELLED
            IN_PROGRESS → DISPATCHED, CANCELLED, FAILED
            DISPATCHED  → DELIVERED, FAILED
            DELIVERED   → (terminal — no further transitions)
            CANCELLED   → (terminal — no further transitions)
            FAILED      → CONFIRMED (retry after investigation)
        '''
        valid = {
            OrderStatus.DETECTED:    {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
            OrderStatus.CONFIRMED:   {OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED},
            OrderStatus.IN_PROGRESS: {OrderStatus.DISPATCHED, OrderStatus.CANCELLED, OrderStatus.FAILED},
            OrderStatus.DISPATCHED:  {OrderStatus.DELIVERED, OrderStatus.FAILED},
            OrderStatus.DELIVERED:   set(),
            OrderStatus.CANCELLED:   set(),
            OrderStatus.FAILED:      {OrderStatus.CONFIRMED},
        }
        return new_status in valid.get(self, set())


@dataclass
class RawMessage:
    '''
    A raw WhatsApp message as extracted from the group.
    This is NOT yet an order — it is the raw input before parsing.

    The parser will attempt to convert a RawMessage into an Order.
    If parsing fails, the message is logged and skipped.

    sender_number: WhatsApp number of who sent the message.
                   Format: 234XXXXXXXXXX (13 digits, no +)
    group_name:    The WhatsApp group this message came from.
    text:          The raw text content of the message.
    timestamp:     When the message was sent (from WhatsApp Web UI).
    message_id:    WhatsApp's internal message identifier (if accessible).
                   Used for deduplication.
    '''
    sender_number: str
    group_name:    str
    text:          str
    timestamp:     datetime
    message_id:    str = ""

    def is_from_staff(self, staff_number: str) -> bool:
        '''True if this message was sent by the configured staff member.'''
        # Normalise both numbers for comparison (strip + and spaces)
        clean_sender = self.sender_number.replace("+", "").replace(" ", "")
        clean_staff  = staff_number.replace("+", "").replace(" ", "")
        return clean_sender == clean_staff

    def preview(self, max_chars: int = 60) -> str:
        '''Short preview of message text for logging.'''
        if len(self.text) <= max_chars:
            return self.text
        return self.text[:max_chars] + "..."


@dataclass
class Order:
    '''
    A confirmed order detected from a WhatsApp group message.
    Created by the Parser when it successfully interprets a RawMessage.

    This is the central entity of the OMS — everything else
    (storage, sheets, notifications) revolves around Order objects.

    order_id:       Unique identifier. Generated from message content
                    or assigned by the database on insert.
    staff_number:   The staff member this order is assigned to.
    customer_name:  Customer name from the message (may be partial).
    customer_phone: Customer WhatsApp number (may be empty if not in message).
    items:          List of ordered items as parsed from the message.
                    Each item is a string description e.g. "2x Sadoer Combo Set"
    raw_text:       The original message text this order was parsed from.
                    Kept for audit trail and re-parsing if needed.
    source_message: The RawMessage this order was created from.
    status:         Current order lifecycle state.
    detected_at:    When the order was first detected.
    updated_at:     When the order status last changed.
    notes:          Optional operator notes about this order.
    '''
    order_id:       str
    staff_number:   str
    customer_name:  str
    items:          list[str]
    raw_text:       str
    source_message: RawMessage
    status:         OrderStatus        = OrderStatus.DETECTED
    customer_phone: str                = ""
    detected_at:    datetime           = field(default_factory=datetime.now)
    updated_at:     datetime           = field(default_factory=datetime.now)
    notes:          str                = ""

    def transition_to(self, new_status: OrderStatus) -> None:
        '''
        Move this order to a new status.
        Enforces valid transitions — raises ValueError if the
        transition is not allowed by business rules.

        Args:
            new_status: The status to transition to.

        Raises:
            ValueError: If the transition is not valid.
        '''
        if not self.status.can_transition_to(new_status):
            raise ValueError(
                f"Cannot transition order {self.order_id} "
                f"from {self.status.value} to {new_status.value}. "
                f"This transition is not allowed by business rules."
            )
        self.status     = new_status
        self.updated_at = datetime.now()

    def is_terminal(self) -> bool:
        '''
        True if this order is in a terminal state (no further changes).
        Terminal states: DELIVERED, CANCELLED.
        '''
        return self.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED)

    def item_summary(self) -> str:
        '''Human-readable summary of ordered items.'''
        if not self.items:
            return "(no items parsed)"
        return ", ".join(self.items)

    def __repr__(self):
        return (
            f"Order(id={self.order_id!r}, "
            f"customer={self.customer_name!r}, "
            f"status={self.status.value}, "
            f"items={len(self.items)})"
        )


@dataclass
class Staff:
    '''
    Represents the staff member whose orders this OMS instance tracks.
    One OMS instance = one staff member = one WhatsApp group.

    number:      WhatsApp number. Format: 234XXXXXXXXXX
    display_name: Name as it appears in WhatsApp (optional).
    group_name:  The WhatsApp group this staff member operates in.
    '''
    number:       str
    group_name:   str
    display_name: str = ""

    def __repr__(self):
        name = f" ({self.display_name})" if self.display_name else ""
        return f"Staff(+{self.number}{name}, group={self.group_name!r})"
"""


# ==============================================================
# ================================================================
#  FILE 9
#  PATH: windwhirl/app/oms/domain/interfaces.py
# ================================================================
# Abstract interfaces for all major OMS components.
# These define the CONTRACT that infrastructure must fulfil.
#
# ARCHITECTURAL RULE (from your prompt):
#   Business Logic NEVER imports Infrastructure.
#   Infrastructure NEVER contains business rules.
#
# These interfaces are the boundary.
# Domain code depends on interfaces (defined here).
# Infrastructure code implements interfaces (written in Day 2+).
# Domain never knows what implements these interfaces.
#
# INTERFACES DEFINED:
#   IMessageSource   — reads messages from WhatsApp group
#   IParser          — converts raw messages to Order objects
#   IValidator       — validates parsed orders
#   IDuplicateDetector — detects already-seen orders
#   IAssignmentEngine — decides which staff member gets each order
#   ISessionManager   — manages browser/WhatsApp login state
#   IDOMObserver      — watches the WhatsApp Web DOM for new messages
#   ISheetSynchronizer — syncs order data to Google Sheets
# ================================================================
# ==============================================================

"""
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from app.oms.domain.entities import Order, RawMessage, Staff


class IMessageSource(ABC):
    '''
    Reads messages from a WhatsApp group.
    Implementation: Playwright-based WhatsApp Web reader (Day 2).

    Business code calls get_new_messages() — it never knows
    whether messages come from a browser, an API, or a mock.
    '''

    @abstractmethod
    async def get_new_messages(
        self,
        group_name: str,
        lookback:   int = 20
    ) -> list[RawMessage]:
        '''
        Return new messages from the group since last check.

        Args:
            group_name: Name of the WhatsApp group to read.
            lookback:   How many recent messages to scan.

        Returns:
            List of RawMessage objects, oldest first.
            Empty list if no new messages.
        '''
        pass

    @abstractmethod
    async def is_available(self) -> bool:
        '''
        True if the message source is ready to read messages.
        For WhatsApp Web: True when browser is open and logged in.
        '''
        pass


class IParser(ABC):
    '''
    Converts a RawMessage into an Order.
    Implementation: regex/NLP message parser (Day 3).

    The parser only cares about text content — it does not
    know or care where the message came from.
    '''

    @abstractmethod
    def parse(
        self,
        message:      RawMessage,
        staff_number: str
    ) -> Optional[Order]:
        '''
        Attempt to parse a raw message into an Order.

        Args:
            message:      The raw WhatsApp message to parse.
            staff_number: The staff number to assign the order to.

        Returns:
            An Order object if parsing succeeded.
            None if the message is not an order (ignore it).

        Raises:
            OrderParseException if the message looks like an order
            but has missing/malformed required fields.
        '''
        pass

    @abstractmethod
    def looks_like_order(self, message: RawMessage) -> bool:
        '''
        Quick pre-check: does this message look like an order at all?
        Used to skip non-order messages (greetings, reactions, etc.)
        before attempting full parsing.

        Returns:
            True if the message is likely an order.
            False if it can be safely skipped.
        '''
        pass


class IValidator(ABC):
    '''
    Validates an Order after parsing.
    Implementation: business rule validator (Day 3).

    Validation is separate from parsing so both can evolve
    independently. Parser handles format; validator handles rules.
    '''

    @abstractmethod
    def validate(self, order: Order) -> list[str]:
        '''
        Validate an order against business rules.

        Args:
            order: The order to validate.

        Returns:
            List of validation error messages.
            Empty list means the order is valid.

        Does NOT raise — returns errors for the caller to handle.
        '''
        pass


class IDuplicateDetector(ABC):
    '''
    Detects whether an order has already been processed.
    Implementation: database lookup (Day 4).

    Prevents the same order from being recorded twice if the
    same message is scanned multiple times.
    '''

    @abstractmethod
    async def is_duplicate(self, order: Order) -> bool:
        '''
        True if this order already exists in the system.

        Comparison is based on message_id if available,
        otherwise on content hash (customer + items + timestamp).
        '''
        pass

    @abstractmethod
    async def mark_seen(self, order: Order) -> None:
        '''
        Record this order as seen to prevent future duplicates.
        Called after an order is successfully stored.
        '''
        pass


class IAssignmentEngine(ABC):
    '''
    Determines which staff member an order should be assigned to.
    For now: always assigns to the configured staff member.
    Future: round-robin, load-balancing, skill-based routing.
    '''

    @abstractmethod
    def assign(self, order: Order, available_staff: list[Staff]) -> Staff:
        '''
        Assign an order to a staff member.

        Args:
            order:           The order to assign.
            available_staff: List of staff available to take orders.

        Returns:
            The Staff member who should handle this order.
        '''
        pass


class ISessionManager(ABC):
    '''
    Manages the browser login state for WhatsApp Web.
    Implementation: Playwright persistent context (Day 2).
    '''

    @abstractmethod
    async def start(self) -> None:
        '''
        Start the browser and load WhatsApp Web.
        Handles QR scan on first run, session restore on subsequent runs.
        '''
        pass

    @abstractmethod
    async def stop(self) -> None:
        '''Close the browser cleanly.'''
        pass

    @abstractmethod
    async def is_logged_in(self) -> bool:
        '''True if WhatsApp Web is loaded and user is logged in.'''
        pass


class IDOMObserver(ABC):
    '''
    Watches the WhatsApp Web DOM for new messages.
    Implementation: MutationObserver or polling (Day 2/3).

    The observer runs continuously and emits events when
    new messages appear. The application layer processes them.
    '''

    @abstractmethod
    async def start_observing(self, group_name: str) -> None:
        '''
        Start watching the specified group for new messages.
        Runs until stop_observing() is called.
        '''
        pass

    @abstractmethod
    async def stop_observing(self) -> None:
        '''Stop watching for new messages.'''
        pass


class ISheetSynchronizer(ABC):
    '''
    Syncs order data to Google Sheets.
    Implementation: Google Sheets API (Day 5+).
    Leave unimplemented until needed.
    '''

    @abstractmethod
    async def sync_order(self, order: Order) -> None:
        '''Add or update an order row in the Google Sheet.'''
        pass

    @abstractmethod
    async def sync_all(self, orders: list[Order]) -> None:
        '''Full sync of all orders to the sheet.'''
        pass
"""


# ==============================================================
# ================================================================
#  FILE 10
#  PATH: windwhirl/app/oms/domain/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.domain.entities import Order, OrderStatus, RawMessage, Staff
from app.oms.domain.exceptions import (
    OrderException,
    DuplicateOrderException,
    OrderParseException,
    GroupNotFoundException,
    StaffNotFoundException,
)
from app.oms.domain.interfaces import (
    IMessageSource,
    IParser,
    IValidator,
    IDuplicateDetector,
    IAssignmentEngine,
    ISessionManager,
    IDOMObserver,
    ISheetSynchronizer,
)

__all__ = [
    "Order", "OrderStatus", "RawMessage", "Staff",
    "OrderException", "DuplicateOrderException",
    "OrderParseException", "GroupNotFoundException",
    "StaffNotFoundException",
    "IMessageSource", "IParser", "IValidator",
    "IDuplicateDetector", "IAssignmentEngine",
    "ISessionManager", "IDOMObserver", "ISheetSynchronizer",
]
"""


# ==============================================================
# ================================================================
#  FILE 11
#  PATH: windwhirl/app/oms/events/dispatcher.py
# ================================================================
# Simple event bus. No external dependencies.
# Future events (MessageReceived, OrderAssigned, etc.) plug in here.
#
# HOW IT WORKS:
#   1. Handlers register for event types:
#      dispatcher.on("order.detected", my_handler)
#   2. Components emit events:
#      await dispatcher.emit("order.detected", order=order)
#   3. All registered handlers are called automatically.
#
# WHY AN EVENT BUS:
#   Decouples producers from consumers.
#   The monitor doesn't know about the sheet syncer.
#   The parser doesn't know about notifications.
#   They all just emit events — whatever is listening handles it.
# ================================================================
# ==============================================================

"""
import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

from app.oms.shared.logger import get_logger

log = get_logger(__name__)


# Type alias for event handler functions
# Handlers can be sync or async
EventHandler = Callable[..., Any]


class EventDispatcher:
    '''
    Simple in-process event bus for the OMS.

    Supports both synchronous and asynchronous handlers.
    Events are dispatched in registration order.
    Handler exceptions are caught and logged — they do not
    prevent other handlers from running.

    Usage:
        dispatcher = EventDispatcher()

        # Register a handler
        @dispatcher.on("order.detected")
        async def handle_new_order(order: Order):
            print(f"New order: {order}")

        # Emit an event (all handlers are called)
        await dispatcher.emit("order.detected", order=my_order)

    Event name conventions:
        "order.detected"        → new order found in group
        "order.status_changed"  → order moved to a new status
        "order.duplicate"       → duplicate order skipped
        "message.received"      → raw message received from group
        "browser.connected"     → browser logged in successfully
        "browser.disconnected"  → browser session lost
        "sheets.synced"         → order synced to Google Sheets
    '''

    def __init__(self):
        # Dict mapping event_name → list of handler functions
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._emit_count: dict[str, int] = defaultdict(int)

    def on(self, event_name: str) -> Callable:
        '''
        Decorator to register a handler for an event type.

        Usage:
            @dispatcher.on("order.detected")
            async def my_handler(order: Order):
                ...

        Args:
            event_name: The event type to listen for.

        Returns:
            Decorator that registers the function as a handler.
        '''
        def decorator(handler: EventHandler) -> EventHandler:
            self.register(event_name, handler)
            return handler
        return decorator

    def register(self, event_name: str, handler: EventHandler) -> None:
        '''
        Register a handler function for an event type.

        Args:
            event_name: Event type string e.g. "order.detected"
            handler:    Function to call when event is emitted.
                        Can be sync or async.
        '''
        self._handlers[event_name].append(handler)
        log.debug(
            f"Handler registered: {handler.__name__!r} "
            f"for event {event_name!r}"
        )

    def unregister(self, event_name: str, handler: EventHandler) -> None:
        '''Remove a handler from an event type.'''
        handlers = self._handlers.get(event_name, [])
        if handler in handlers:
            handlers.remove(handler)
            log.debug(
                f"Handler unregistered: {handler.__name__!r} "
                f"for event {event_name!r}"
            )

    async def emit(self, event_name: str, **kwargs) -> None:
        '''
        Emit an event. All registered handlers are called with **kwargs.

        Handler exceptions are caught and logged individually.
        A failing handler does not prevent other handlers from running.

        Args:
            event_name: The event type to emit.
            **kwargs:   Data passed to all handlers as keyword arguments.

        Example:
            await dispatcher.emit("order.detected", order=order, source="group")
        '''
        handlers = self._handlers.get(event_name, [])

        if not handlers:
            log.debug(f"Event emitted with no handlers: {event_name!r}")
            return

        self._emit_count[event_name] += 1
        log.debug(
            f"Emitting {event_name!r} to {len(handlers)} handler(s) "
            f"(emit #{self._emit_count[event_name]})"
        )

        for handler in handlers:
            try:
                result = handler(**kwargs)
                # Support both sync and async handlers
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error(
                    f"Handler {handler.__name__!r} failed on event "
                    f"{event_name!r}: {e}",
                    exc_info=True
                )
                # Continue to next handler — one failure doesn't stop others

    def handler_count(self, event_name: str) -> int:
        '''Number of handlers registered for an event type.'''
        return len(self._handlers.get(event_name, []))

    def emit_count(self, event_name: str) -> int:
        '''How many times an event has been emitted.'''
        return self._emit_count.get(event_name, 0)

    def clear(self, event_name: str = None) -> None:
        '''
        Clear handlers. If event_name given: clear that event only.
        If None: clear all handlers (used in tests).
        '''
        if event_name:
            self._handlers[event_name] = []
        else:
            self._handlers.clear()
            self._emit_count.clear()

    def registered_events(self) -> list[str]:
        '''List of all event types that have at least one handler.'''
        return [e for e, h in self._handlers.items() if h]
"""


# ==============================================================
# ================================================================
#  FILE 12
#  PATH: windwhirl/app/oms/events/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.events.dispatcher import EventDispatcher

# Global singleton event dispatcher
# Import this in any module that emits or listens to events:
#   from app.oms.events import dispatcher
#   dispatcher.on("order.detected")(my_handler)
#   await dispatcher.emit("order.detected", order=order)
dispatcher = EventDispatcher()

__all__ = ["EventDispatcher", "dispatcher"]
"""


# ==============================================================
# ================================================================
#  FILE 13
#  PATH: windwhirl/app/oms/repositories/interfaces.py
# ================================================================
# Repository interfaces — the contract for data persistence.
# Domain code depends on these interfaces.
# Infrastructure provides the implementations (SQLite, Postgres).
# ================================================================
# ==============================================================

"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from app.oms.domain.entities import Order, OrderStatus


class IOrderRepository(ABC):
    '''
    Interface for order persistence.
    Implementation: SQLite repository (Day 4).

    The domain never imports SQLAlchemy or any DB library.
    It only calls these methods — the implementation handles storage.
    '''

    @abstractmethod
    async def save(self, order: Order) -> Order:
        '''
        Persist a new order or update an existing one.
        Returns the saved order (with any DB-assigned fields like id).
        '''
        pass

    @abstractmethod
    async def get_by_id(self, order_id: str) -> Optional[Order]:
        '''
        Return an order by its ID, or None if not found.
        '''
        pass

    @abstractmethod
    async def get_by_status(self, status: OrderStatus) -> list[Order]:
        '''
        Return all orders with the given status.
        '''
        pass

    @abstractmethod
    async def get_by_staff(
        self,
        staff_number: str,
        status: Optional[OrderStatus] = None
    ) -> list[Order]:
        '''
        Return orders assigned to a staff member.
        Optionally filtered by status.
        '''
        pass

    @abstractmethod
    async def get_recent(
        self,
        since: datetime,
        limit: int = 50
    ) -> list[Order]:
        '''
        Return orders detected after a given timestamp.
        Used to check for duplicate orders in a time window.
        '''
        pass

    @abstractmethod
    async def exists(self, order_id: str) -> bool:
        '''True if an order with this ID already exists.'''
        pass

    @abstractmethod
    async def count_by_status(self) -> dict[str, int]:
        '''
        Return count of orders grouped by status.
        Used for reporting and dashboard metrics.
        Example: {"DETECTED": 5, "CONFIRMED": 12, "DELIVERED": 30}
        '''
        pass
"""


# ==============================================================
# ================================================================
#  FILE 14
#  PATH: windwhirl/app/oms/repositories/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.repositories.interfaces import IOrderRepository

__all__ = ["IOrderRepository"]
"""


# ==============================================================
# ================================================================
#  FILE 15
#  PATH: windwhirl/app/oms/application/services.py
# ================================================================
# Application services — coordinates the workflow.
# Does NOT contain business rules (those live in domain).
# Does NOT contain infrastructure code (that lives in infrastructure).
#
# Think of services as the conductor:
#   1. Gets new messages from IMessageSource
#   2. Asks IParser if each message is an order
#   3. Asks IValidator if the order is valid
#   4. Asks IDuplicateDetector if we've seen it before
#   5. Saves via IOrderRepository
#   6. Emits events via EventDispatcher
#   7. Syncs via ISheetSynchronizer
#
# Services receive all dependencies via constructor injection.
# This makes them trivially testable — pass mock implementations.
# ================================================================
# ==============================================================

"""
from typing import Optional

from app.oms.domain.entities import Order, OrderStatus, RawMessage
from app.oms.domain.interfaces import (
    IAssignmentEngine,
    IDuplicateDetector,
    IMessageSource,
    IParser,
    ISheetSynchronizer,
    IValidator,
)
from app.oms.domain.exceptions import OrderParseException
from app.oms.events import dispatcher
from app.oms.repositories.interfaces import IOrderRepository
from app.oms.shared.logger import get_logger

log = get_logger(__name__)


class OrderMonitorService:
    '''
    Coordinates the order detection and processing workflow.

    This is the main application service. It ties together
    message reading, parsing, validation, storage, and events.

    All dependencies are injected — this service never instantiates
    infrastructure classes directly.

    Usage (Day 2+ when implementations exist):
        service = OrderMonitorService(
            message_source=PlaywrightMessageSource(cfg),
            parser=OrderParser(cfg),
            validator=OrderValidator(cfg),
            duplicate_detector=DatabaseDuplicateDetector(repo),
            repository=SQLiteOrderRepository(cfg),
        )
        await service.run_once()  # Check for new orders once
        await service.run_loop()  # Continuous monitoring
    '''

    def __init__(
        self,
        message_source:     IMessageSource,
        parser:             IParser,
        validator:          IValidator,
        duplicate_detector: IDuplicateDetector,
        repository:         IOrderRepository,
        sheet_synchronizer: Optional[ISheetSynchronizer] = None,
    ):
        self._source      = message_source
        self._parser      = parser
        self._validator   = validator
        self._dedup       = duplicate_detector
        self._repo        = repository
        self._sheets      = sheet_synchronizer
        self._running     = False

    async def run_once(
        self,
        group_name:   str,
        staff_number: str,
        lookback:     int = 20
    ) -> list[Order]:
        '''
        Perform one check of the WhatsApp group for new orders.
        Returns the list of newly detected orders (may be empty).

        This is the unit of work — the monitoring loop calls this
        repeatedly on a timer. Testing calls this once directly.

        Args:
            group_name:   WhatsApp group to check.
            staff_number: Staff member whose orders to detect.
            lookback:     How many recent messages to scan.

        Returns:
            List of new Order objects successfully processed.
        '''
        if not await self._source.is_available():
            log.warning("Message source not available — skipping this check.")
            return []

        # Fetch recent messages from the group
        messages = await self._source.get_new_messages(
            group_name=group_name,
            lookback=lookback
        )

        if not messages:
            log.debug("No new messages found.")
            return []

        log.info(f"Checking {len(messages)} message(s) for orders...")

        new_orders = []

        for message in messages:
            order = await self._process_message(message, staff_number)
            if order:
                new_orders.append(order)

        if new_orders:
            log.info(f"Detected {len(new_orders)} new order(s) this cycle.")
        else:
            log.debug("No new orders in this cycle.")

        return new_orders

    async def _process_message(
        self,
        message:      RawMessage,
        staff_number: str
    ) -> Optional[Order]:
        '''
        Process one message through the full pipeline.
        Returns the Order if successful, None if skipped.
        All errors are caught and logged — never propagate.
        '''
        log.debug(f"Processing: {message.preview()!r}")

        try:
            # Quick pre-check — is this even likely an order?
            if not self._parser.looks_like_order(message):
                log.debug("  → Not an order (skipped by pre-check)")
                return None

            # Parse into Order object
            order = self._parser.parse(message, staff_number)
            if order is None:
                log.debug("  → Parsing returned None (not an order)")
                return None

            # Validate business rules
            errors = self._validator.validate(order)
            if errors:
                log.warning(
                    f"  → Order validation failed: {errors}\n"
                    f"     Raw: {message.preview()!r}"
                )
                await dispatcher.emit(
                    "order.validation_failed",
                    order=order,
                    errors=errors,
                    message=message
                )
                return None

            # Check for duplicates
            if await self._dedup.is_duplicate(order):
                log.info(f"  → Duplicate order skipped: {order.order_id!r}")
                await dispatcher.emit("order.duplicate", order=order)
                return None

            # Save to repository
            saved_order = await self._repo.save(order)

            # Mark as seen to prevent future duplicates
            await self._dedup.mark_seen(saved_order)

            # Sync to Google Sheets if configured
            if self._sheets:
                try:
                    await self._sheets.sync_order(saved_order)
                except Exception as e:
                    # Sheets failure should not block order processing
                    log.warning(f"  → Sheets sync failed (non-critical): {e}")

            # Emit success event — other modules can react to this
            await dispatcher.emit(
                "order.detected",
                order=saved_order,
                message=message
            )

            log.info(
                f"  ✓ Order saved: {saved_order.order_id!r} "
                f"— {saved_order.customer_name} "
                f"— {saved_order.item_summary()}"
            )

            return saved_order

        except OrderParseException as e:
            log.warning(f"  → Parse error: {e}")
            await dispatcher.emit("order.parse_error", error=e, message=message)
            return None

        except Exception as e:
            log.error(
                f"  → Unexpected error processing message: {e}",
                exc_info=True
            )
            return None

    async def update_order_status(
        self,
        order_id:   str,
        new_status: OrderStatus
    ) -> Optional[Order]:
        '''
        Transition an order to a new status.
        Validates the transition, saves, and emits an event.

        Returns the updated Order, or None if not found.
        Raises ValueError if the transition is not valid.
        '''
        order = await self._repo.get_by_id(order_id)
        if not order:
            log.warning(f"Order not found for status update: {order_id!r}")
            return None

        old_status = order.status
        order.transition_to(new_status)    # Raises ValueError if invalid
        await self._repo.save(order)

        await dispatcher.emit(
            "order.status_changed",
            order=order,
            old_status=old_status,
            new_status=new_status
        )

        log.info(
            f"Order {order_id!r}: {old_status.value} → {new_status.value}"
        )
        return order
"""


# ==============================================================
# ================================================================
#  FILE 16
#  PATH: windwhirl/app/oms/application/__init__.py
# ================================================================
# ==============================================================

"""
from app.oms.application.services import OrderMonitorService

__all__ = ["OrderMonitorService"]
"""


# ==============================================================
# ================================================================
#  FILE 17
#  PATH: windwhirl/app/oms/infrastructure/__init__.py
# ================================================================
# Infrastructure is intentionally empty today.
# Day 2 adds: PlaywrightSessionManager, PlaywrightMessageSource.
# Day 3 adds: OrderParser, OrderValidator.
# Day 4 adds: SQLiteOrderRepository, DatabaseDuplicateDetector.
# Day 5 adds: GoogleSheetSynchronizer.
# ================================================================
# ==============================================================

"""# Infrastructure implementations will be added in future milestones.
# Day 2: Browser and WhatsApp session management (Playwright)
# Day 3: Message parsing and validation
# Day 4: SQLite storage and duplicate detection
# Day 5: Google Sheets synchronisation
"""


# ==============================================================
# ================================================================
#  FILE 18
#  PATH: windwhirl/app/oms/tests/__init__.py
# ================================================================
# ==============================================================

"""
# OMS test suite.
# Tests will be added as each milestone is implemented.
# Day 1: No tests yet — foundation only.
# Day 2: Browser infrastructure tests.
# Day 3: Parser tests (pure unit tests, no browser needed).
# Day 4: Repository tests (SQLite in-memory).
"""


# ==============================================================
# DAY 1 VERIFICATION
# ==============================================================
# After saving all 18 files, run from windwhirl/ directory:
#
# Test 1 — package imports cleanly:
#   python -c "
#   import sys
#   sys.path.insert(0, '.')
#   from app.oms.config.settings import get_settings
#   cfg = get_settings()
#   print('Settings:', cfg)
#   "
#
# Test 2 — logger works:
#   python -c "
#   import sys
#   sys.path.insert(0, '.')
#   from app.oms.shared.logger import get_logger
#   log = get_logger('test')
#   log.info('Logger OK')
#   log.debug('Debug OK')
#   log.warning('Warning OK')
#   "
#
# Test 3 — event dispatcher works:
#   python -c "
#   import sys, asyncio
#   sys.path.insert(0, '.')
#   from app.oms.events import dispatcher
#
#   results = []
#
#   @dispatcher.on('test.event')
#   async def handler(value):
#       results.append(value)
#
#   asyncio.run(dispatcher.emit('test.event', value=42))
#   assert results == [42], f'Expected [42], got {results}'
#   print('Event dispatcher OK — received:', results)
#   "
#
# Test 4 — domain entities work:
#   python -c "
#   import sys
#   sys.path.insert(0, '.')
#   from app.oms.domain.entities import Order, OrderStatus, RawMessage
#   from datetime import datetime
#
#   msg = RawMessage(
#       sender_number='2348037882259',
#       group_name='Nabeau Orders',
#       text='Customer: Blessing. Item: Sadoer Combo x2. Address: Lagos.',
#       timestamp=datetime.now()
#   )
#   print('RawMessage:', msg.preview())
#   print('OrderStatus transitions valid:', OrderStatus.DETECTED.can_transition_to(OrderStatus.CONFIRMED))
#   print('OrderStatus transitions invalid:', OrderStatus.DELIVERED.can_transition_to(OrderStatus.DETECTED))
#   "
#
# All 4 tests should pass before starting Day 2.
#
# ==============================================================
# WHAT DAY 2 BUILDS
# ==============================================================
# Day 2: Browser Infrastructure
#   - PlaywrightSessionManager (implements ISessionManager)
#   - PlaywrightMessageSource (implements IMessageSource)
#   - Browser lifecycle: start, stop, reconnect
#   - WhatsApp Web login: QR scan, session persistence
#   - Navigate to the target group
#   - Read messages from the group DOM
#   - No parsing, no storage, no business logic
#
# The clean interfaces defined today mean Day 2 is purely
# about getting the browser working — nothing else changes.
# ==============================================================