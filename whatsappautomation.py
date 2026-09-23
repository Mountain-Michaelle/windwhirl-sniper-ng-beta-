# ==============================================================
# WHATSAPP REVIEW AUTOMATION SYSTEM — whatsapp_automation.py
# ==============================================================
# Single-file implementation. All modules in one place.
# Use Ctrl+F / Cmd+F to navigate between sections.
#
# SECTION MAP:
#   0  — All imports
#   1  — Logging setup
#   2  — User configuration (EDIT THIS SECTION)
#   3  — Config loader (AppConfig class)
#   4  — Database models & layer
#   5  — Data reader (Excel → clean customer dicts)
#   6  — Message templates (Jinja2 strings)
#   7  — Message builder (renders templates per customer)
#   8  — Abstract sender interface
#   9  — Playwright sender (WhatsApp Web automation, 8 stealth layers)
#   10 — Reporter (daily report + email)
#   11 — Scheduler (APScheduler, 6 sessions/day)
#   12 — Folder/file setup utilities
#   13 — CLI command functions
#   14 — Main entry point
#
# HOW TO RUN:
#   uv venv
#   source .venv/bin/activate     (Mac/Linux)
#   .venv\Scripts\activate         (Windows)
#   uv pip install -r requirements.txt
#   playwright install chromium
#   python whatsapp_automation.py --setup
#   python whatsapp_automation.py --preview
#   python whatsapp_automation.py --dry-run
#   python whatsapp_automation.py --run --now --count 3
#   python whatsapp_automation.py --run
#
# requirements.txt:
#   playwright==1.45.0
#   pandas==2.2.2
#   openpyxl==3.1.4
#   sqlalchemy==2.0.31
#   apscheduler==3.10.4
#   jinja2==3.1.4
#   pillow==10.4.0
#   pyyaml==6.0.1
#   python-dotenv==1.0.1
#   aiofiles==23.2.1
# ==============================================================


# ==============================================================
# SECTION 0: ALL IMPORTS
# ==============================================================
# Every import the system needs — all in one place at the top.
# If any import fails: activate your venv and run:
#   uv pip install -r requirements.txt
# playwright import is deferred to the sender class to keep
# startup fast for non-browser commands like --preview.
# ==============================================================

import asyncio
import logging
import logging.handlers
import random
import re
import signal
import smtplib
import sys
import argparse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd
from jinja2 import Environment, Undefined
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Integer, String, Text, create_engine, func
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ==============================================================
# SECTION 1: LOGGING SETUP
# ==============================================================
# Must run before anything else. Logs go to:
#   - Console (INFO level — what the user sees in real time)
#   - logs/automation.log (DEBUG level — full detail for troubleshooting)
# Rotating file: max 5MB, keeps 3 backups.
# All other classes use: self._log = logging.getLogger(self.__class__.__name__)
# ==============================================================

def setup_logging():
    """
    Configure application-wide logging.
    Call once at startup before any other code runs.
    """
    Path("logs").mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console: show INFO and above — clean output for the user
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S"
    ))

    # File: DEBUG and above — full trace for troubleshooting
    file_h = logging.handlers.RotatingFileHandler(
        "logs/automation.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
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

logger = logging.getLogger(__name__)


# ==============================================================
# SECTION 2: USER CONFIGURATION
# ==============================================================
# THIS IS THE ONLY SECTION YOU NEED TO EDIT.
# Search for "EDIT ME" to find fields you must change.
#
# To adapt this tool for another business:
#   Change target_product, country_code, excel_filename,
#   discount_offer, and smtp_* fields only.
# Everything else is handled automatically.
# ==============================================================

CONFIG = {
    # ── PRODUCT TARGETING ──────────────────────────────────────
    # Only customers whose 'Product' column contains this keyword
    # (case-insensitive) will be messaged. All others are skipped.
    "target_product": "sadoer",  # EDIT ME — change for a different product

    # ── SEND ORDER ─────────────────────────────────────────────
    # "recent_first" → most recent buyers first (safest: they remember you)
    # "oldest_first" → oldest buyers first
    # "random"       → shuffled order
    "send_order": "recent_first",

    # ── DAILY LIMIT ────────────────────────────────────────────
    # Start at 50 for first run. Increase to 100 after verifying success.
    # Never exceed 200 — stay safely under WhatsApp detection thresholds.
    "daily_limit": 50,

    # ── SESSION SCHEDULE ───────────────────────────────────────
    # 6 sessions spread across a natural working day.
    # Counts must add up to daily_limit.
    # Adjust times to your actual working hours.
    "session_schedule": [
        {"time": "08:15", "count": 8},
        {"time": "09:45", "count": 8},
        {"time": "11:30", "count": 8},
        {"time": "13:15", "count": 8},
        {"time": "15:30", "count": 9},
        {"time": "17:00", "count": 9},
    ],

    # ── HUMAN-LIKE DELAYS (all values in seconds) ──────────────
    # The system picks a RANDOM value within each range every time.
    # NEVER set min == max — perfectly uniform delays look like a bot.
    "delays": {
        "between_messages_min": 55,    # Minimum gap between messages
        "between_messages_max": 110,   # Maximum gap between messages
        "after_burst_min": 240,        # Longer pause after every burst (4 min)
        "after_burst_max": 480,        # Longer pause after every burst (8 min)
        "burst_size": 4,               # How many messages before a burst pause
        "jitter_min": -20,             # Random seconds subtracted from delay
        "jitter_max": 30,              # Random seconds added to delay
        "long_pause_enabled": True,    # One random long break per session
        "long_pause_min": 480,         # 8 minutes minimum
        "long_pause_max": 900,         # 15 minutes maximum
        "pre_type_min": 3,             # Wait before typing (simulates reading)
        "pre_type_max": 8,
        "type_speed_min": 40,          # Milliseconds per character typed
        "type_speed_max": 100,
    },

    # ── MESSAGE SETTINGS ───────────────────────────────────────
    "image_path": None,                # Set to "data/image.jpg" to send image+caption
    "review_link": "",                 # Optional review URL. Leave "" if none.
    "discount_offer": "10% off your next order",  # EDIT ME

    # ── EMAIL REPORT SETTINGS ──────────────────────────────────
    # Leave smtp_password as "" to skip email reports entirely.
    # Use a Gmail App Password (not your normal Gmail password):
    # myaccount.google.com → Security → App Passwords
    "smtp_email":    "youremail@gmail.com",  # EDIT ME
    "smtp_password": "",                      # EDIT ME
    "smtp_to":       "youremail@gmail.com",  # EDIT ME

    # ── DATABASE ───────────────────────────────────────────────
    # SQLite: runs locally, zero setup needed.
    # To switch to PostgreSQL later: change this URL only. Nothing else changes.
    "database_url": "sqlite:///data/automation.db",

    # ── PHONE NORMALIZATION ────────────────────────────────────
    # Nigeria = 234. Ghana = 233. UK = 44. Kenya = 254.
    "country_code": "234",  # EDIT ME if selling to businesses in other countries

    # ── EXCEL FILE ─────────────────────────────────────────────
    "excel_filename": "customers.xlsx",  # EDIT ME if your file has a different name
}


# ==============================================================
# SECTION 3: CONFIG LOADER
# ==============================================================
# Wraps CONFIG dict in a typed object. Validates on load.
# All other sections receive an AppConfig instance — none
# of them read CONFIG directly. This keeps the system portable:
# swap CONFIG for a YAML loader later without touching other sections.
# ==============================================================

class DelayConfig:
    """Typed wrapper for the delays sub-dict in CONFIG."""
    def __init__(self, d: dict):
        self.between_messages_min = d["between_messages_min"]
        self.between_messages_max = d["between_messages_max"]
        self.after_burst_min      = d["after_burst_min"]
        self.after_burst_max      = d["after_burst_max"]
        self.burst_size           = d["burst_size"]
        self.jitter_min           = d["jitter_min"]
        self.jitter_max           = d["jitter_max"]
        self.long_pause_enabled   = d["long_pause_enabled"]
        self.long_pause_min       = d["long_pause_min"]
        self.long_pause_max       = d["long_pause_max"]
        self.pre_type_min         = d["pre_type_min"]
        self.pre_type_max         = d["pre_type_max"]
        self.type_speed_min       = d["type_speed_min"]
        self.type_speed_max       = d["type_speed_max"]


class AppConfig:
    """
    Application configuration object. Built from the CONFIG dict above.
    All sections import this — never CONFIG directly.

    Usage:
        cfg = AppConfig()
        cfg.target_product          → "sadoer"
        cfg.delays.burst_size       → 4
        cfg.excel_path()            → Path("data/customers.xlsx")
        cfg.has_email()             → True or False
        cfg.session_jobs()          → [{"hour": 8, "minute": 15, "count": 8}, ...]
        cfg.total_daily_count()     → 50
    """

    def __init__(self, raw: dict = None):
        raw = raw or CONFIG
        self._validate(raw)

        self.target_product   = str(raw["target_product"]).lower().strip()
        self.send_order       = str(raw["send_order"])
        self.daily_limit      = int(raw["daily_limit"])
        self.session_schedule = raw["session_schedule"]
        self.delays           = DelayConfig(raw["delays"])
        self.image_path       = raw.get("image_path")
        self.review_link      = str(raw.get("review_link", ""))
        self.discount_offer   = str(raw["discount_offer"])
        self.smtp_email       = str(raw.get("smtp_email", ""))
        self.smtp_password    = str(raw.get("smtp_password", ""))
        self.smtp_to          = str(raw.get("smtp_to", ""))
        self.database_url     = str(raw["database_url"])
        self.country_code     = str(raw["country_code"])
        self.excel_filename   = str(raw["excel_filename"])

    def _validate(self, raw: dict):
        """Fail fast with a clear message if config is incomplete."""
        required = [
            "target_product", "daily_limit", "session_schedule",
            "delays", "discount_offer", "database_url",
            "country_code", "excel_filename"
        ]
        for key in required:
            if key not in raw:
                raise ValueError(
                    f"Missing required config field: '{key}'. "
                    f"Check the CONFIG dict in Section 2."
                )
        if not isinstance(raw["daily_limit"], int) or raw["daily_limit"] < 1:
            raise ValueError("daily_limit must be a positive integer.")
        if not isinstance(raw["session_schedule"], list) or not raw["session_schedule"]:
            raise ValueError("session_schedule must be a non-empty list.")

    def excel_path(self) -> Path:
        """Full path to the Excel file."""
        return Path("data") / self.excel_filename

    def has_email(self) -> bool:
        """True only if both smtp_email and smtp_password are set."""
        return bool(self.smtp_email.strip() and self.smtp_password.strip())

    def session_jobs(self) -> list:
        """
        Parse session_schedule time strings into APScheduler-ready dicts.
        "08:15" → {"hour": 8, "minute": 15, "count": 8}
        """
        jobs = []
        for s in self.session_schedule:
            h, m = s["time"].split(":")
            jobs.append({"hour": int(h), "minute": int(m), "count": int(s["count"])})
        return jobs

    def total_daily_count(self) -> int:
        """Sum of all session counts."""
        return sum(s["count"] for s in self.session_schedule)

    def __repr__(self):
        return (
            f"AppConfig(product={self.target_product!r}, "
            f"limit={self.daily_limit}, "
            f"email={'configured' if self.has_email() else 'not configured'})"
        )


# ==============================================================
# SECTION 4: DATABASE MODELS & LAYER
# ==============================================================
# Two SQLAlchemy ORM tables:
#   customers  — one row per imported customer
#   send_log   — one row per send attempt (tracks retries & status)
#
# ALL database operations go through the Database class.
# No other section reads or writes the DB directly.
#
# SQLite by default. To switch to PostgreSQL:
#   Change database_url in CONFIG — nothing else in this section changes.
# ==============================================================

class Base(DeclarativeBase):
    """SQLAlchemy declarative base. All ORM models inherit from this."""
    pass


class SendStatus(str, Enum):
    """
    All valid status values for a send_log row.
    Using Enum prevents typos from silently creating wrong status strings.
    """
    PENDING        = "PENDING"         # Imported, waiting to be sent
    SENT           = "SENT"            # Confirmed delivered — never resend
    FAILED         = "FAILED"          # Error occurred — eligible for retry
    FAILED_FINAL   = "FAILED_FINAL"    # Max retries reached — give up
    INVALID_NUMBER = "INVALID_NUMBER"  # Not on WhatsApp — never retry
    RETRYING       = "RETRYING"        # Currently being retried


class Customer(Base):
    """
    One row per customer from the Excel file.
    order_id is the unique deduplication key — same person never imported twice.
    """
    __tablename__ = "customers"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    order_id         = Column(String, unique=True, nullable=False)   # Excel 'Order ID'
    customer_name    = Column(String, nullable=False)                 # Full name
    first_name       = Column(String, nullable=False)                 # For "Hi {name}!"
    raw_phone        = Column(String)                                  # Original value
    normalized_phone = Column(String)                                  # E.164: 234XXXXXXXXXX
    product_raw      = Column(String)                                  # Original from Excel
    product_clean    = Column(String)                                  # HTML stripped
    order_date       = Column(DateTime)                                # For sorting
    phone_valid      = Column(Boolean, default=True)                   # False if unresolvable
    imported_at      = Column(DateTime, default=func.now())

    send_logs = relationship("SendLog", back_populates="customer")


class SendLog(Base):
    """
    One row per send attempt per customer.
    A customer can have multiple rows here (initial attempt + retries).
    The status column is the authoritative record of what happened.
    """
    __tablename__ = "send_log"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    customer_id     = Column(Integer, ForeignKey("customers.id"), nullable=False)
    order_id        = Column(String, nullable=False)   # Denormalized for fast queries
    status          = Column(String, nullable=False, default=SendStatus.PENDING)
    message_text    = Column(Text)                     # Exact message that was sent
    template_used   = Column(String)                   # 'A' or 'B' — for A/B analytics
    image_sent      = Column(Boolean, default=False)
    attempt_count   = Column(Integer, default=0)
    sent_at         = Column(DateTime)
    error_message   = Column(Text)
    screenshot_path = Column(String)
    created_at      = Column(DateTime, default=func.now())
    updated_at      = Column(DateTime, onupdate=func.now())

    customer = relationship("Customer", back_populates="send_logs")


class Database:
    """
    All database operations for the automation system.
    Works with SQLite (local) and PostgreSQL (hosted) via SQLAlchemy.

    Usage:
        db = Database("sqlite:///data/automation.db")
        db.init()                                        # Create tables
        db.upsert_customer({...})                        # Insert/update customer
        batch = db.get_pending(limit=8)                  # Fetch next batch
        db.mark_sent(order_id, message, template, path)  # After successful send
        db.mark_failed(order_id, error)                  # After failed send
        db.mark_invalid(order_id)                        # Not on WhatsApp
    """

    def __init__(self, database_url: str):
        self._engine         = create_engine(database_url, echo=False)
        self._SessionFactory = sessionmaker(bind=self._engine)
        self._log            = logging.getLogger(self.__class__.__name__)

    def init(self):
        """Create tables if they don't exist. Safe to call multiple times."""
        Base.metadata.create_all(self._engine)
        self._log.info("Database ready.")

    def _session(self) -> Session:
        """Return a new database session. Always use in a with-block."""
        return self._SessionFactory()

    def upsert_customer(self, data: dict):
        """
        Insert a new customer or update if order_id already exists.
        On new insert: also creates an initial PENDING send_log entry.
        """
        with self._session() as session:
            existing = session.query(Customer).filter_by(
                order_id=data["order_id"]
            ).first()

            if existing:
                # Update existing record with fresh data from Excel
                for k, v in data.items():
                    if hasattr(existing, k) and k != "id":
                        setattr(existing, k, v)
            else:
                # New customer — insert and create PENDING log entry
                customer = Customer(**{
                    k: v for k, v in data.items()
                    if hasattr(Customer, k) and k != "id"
                })
                session.add(customer)
                session.flush()  # Need customer.id before creating send_log

                log_entry = SendLog(
                    customer_id=customer.id,
                    order_id=customer.order_id,
                    status=SendStatus.PENDING,
                )
                session.add(log_entry)

            session.commit()

    def already_sent(self, order_id: str) -> bool:
        """
        True if this order_id has any SENT entry.
        Core deduplication check — prevents sending twice to same person.
        """
        with self._session() as session:
            return session.query(SendLog).filter_by(
                order_id=order_id,
                status=SendStatus.SENT
            ).first() is not None

    def get_pending(self, limit: int, order: str = "recent_first") -> list:
        """
        Fetch the next batch of customers ready to message.
        Excludes already-sent, invalid phones, and invalid numbers.
        Returns plain dicts (not ORM objects) for thread safety.
        """
        with self._session() as session:
            q = (
                session.query(Customer, SendLog)
                .join(SendLog, Customer.id == SendLog.customer_id)
                .filter(SendLog.status == SendStatus.PENDING)
                .filter(Customer.phone_valid == True)
                .filter(Customer.normalized_phone.isnot(None))
            )

            if order == "recent_first":
                q = q.order_by(Customer.order_date.desc())
            elif order == "oldest_first":
                q = q.order_by(Customer.order_date.asc())

            # Fetch extra for shuffle — limit after
            rows = q.limit(limit * 3).all()

            if order == "random":
                random.shuffle(rows)

            result = []
            for customer, log in rows[:limit]:
                result.append({
                    "order_id":         customer.order_id,
                    "customer_name":    customer.customer_name,
                    "first_name":       customer.first_name,
                    "normalized_phone": customer.normalized_phone,
                    "product_clean":    customer.product_clean,
                    "order_date":       customer.order_date,
                    "attempt_count":    log.attempt_count,
                })

            return result

    def mark_sent(self, order_id: str, message_text: str,
                  template_used: str, screenshot_path: str = ""):
        """Update send_log to SENT with full delivery details."""
        with self._session() as session:
            log = (
                session.query(SendLog)
                .filter(SendLog.order_id == order_id)
                .filter(SendLog.status.in_([SendStatus.PENDING, SendStatus.RETRYING]))
                .first()
            )
            if log:
                log.status          = SendStatus.SENT
                log.message_text    = message_text
                log.template_used   = template_used
                log.sent_at         = datetime.now()
                log.screenshot_path = screenshot_path
                log.attempt_count  += 1
                log.updated_at      = datetime.now()
                session.commit()
                self._log.debug(f"Marked SENT: {order_id}")

    def mark_failed(self, order_id: str, error_message: str):
        """
        Increment attempt_count and mark FAILED.
        If attempt_count reaches 2: mark FAILED_FINAL (no more retries).
        """
        with self._session() as session:
            log = (
                session.query(SendLog)
                .filter(SendLog.order_id == order_id)
                .filter(SendLog.status.in_([SendStatus.PENDING, SendStatus.RETRYING]))
                .first()
            )
            if log:
                log.attempt_count += 1
                log.error_message  = error_message
                log.updated_at     = datetime.now()
                log.status = (
                    SendStatus.FAILED_FINAL
                    if log.attempt_count >= 2
                    else SendStatus.FAILED
                )
                session.commit()
                self._log.warning(f"Marked {log.status}: {order_id} — {error_message}")

    def mark_invalid(self, order_id: str):
        """Mark as INVALID_NUMBER — never retry."""
        with self._session() as session:
            log = session.query(SendLog).filter_by(order_id=order_id).first()
            if log:
                log.status     = SendStatus.INVALID_NUMBER
                log.updated_at = datetime.now()
                session.commit()
                self._log.info(f"Marked INVALID_NUMBER: {order_id}")

    def get_retry_eligible(self) -> list:
        """Return customers with FAILED status and fewer than 2 attempts."""
        with self._session() as session:
            rows = (
                session.query(Customer, SendLog)
                .join(SendLog, Customer.id == SendLog.customer_id)
                .filter(SendLog.status == SendStatus.FAILED)
                .filter(SendLog.attempt_count < 2)
                .all()
            )
            return [
                {
                    "order_id":      c.order_id,
                    "first_name":    c.first_name,
                    "phone":         c.normalized_phone,
                    "attempt_count": l.attempt_count,
                }
                for c, l in rows
            ]

    def get_daily_summary(self) -> dict:
        """
        Return counts per status for today's report.
        Also includes A/B template breakdown and detail lists for failed/invalid.
        """
        with self._session() as session:
            today_start = datetime.combine(date.today(), datetime.min.time())

            # Count every status
            status_rows = session.query(
                SendLog.status, func.count(SendLog.id)
            ).group_by(SendLog.status).all()

            summary = {s: 0 for s in SendStatus}
            for status, count in status_rows:
                summary[status] = count

            # Template A vs B breakdown for today's sent messages
            tmpl_rows = session.query(
                SendLog.template_used, func.count(SendLog.id)
            ).filter(
                SendLog.status == SendStatus.SENT,
                SendLog.sent_at >= today_start
            ).group_by(SendLog.template_used).all()

            summary["template_A"] = 0
            summary["template_B"] = 0
            for tmpl, count in tmpl_rows:
                if tmpl == "A":
                    summary["template_A"] = count
                elif tmpl == "B":
                    summary["template_B"] = count

            # Failed details
            failed_rows = session.query(Customer, SendLog).join(
                SendLog, Customer.id == SendLog.customer_id
            ).filter(SendLog.status == SendStatus.FAILED).all()

            summary["failed_details"] = [
                {
                    "name":          c.customer_name,
                    "phone":         c.normalized_phone or c.raw_phone,
                    "error_message": l.error_message or "Unknown",
                }
                for c, l in failed_rows
            ]

            # Invalid number details
            invalid_rows = session.query(Customer, SendLog).join(
                SendLog, Customer.id == SendLog.customer_id
            ).filter(SendLog.status == SendStatus.INVALID_NUMBER).all()

            summary["invalid_details"] = [
                {"name": c.customer_name, "phone": c.raw_phone or "Unknown"}
                for c, l in invalid_rows
            ]

            return summary

    def get_stats(self) -> dict:
        """Quick statistics for the --preview command."""
        with self._session() as session:
            return {
                "total":          session.query(Customer).count(),
                "pending":        session.query(SendLog).filter_by(status=SendStatus.PENDING).count(),
                "sent":           session.query(SendLog).filter_by(status=SendStatus.SENT).count(),
                "invalid":        session.query(SendLog).filter_by(status=SendStatus.INVALID_NUMBER).count(),
                "invalid_phones": session.query(Customer).filter_by(phone_valid=False).count(),
            }

    def reset_failed(self) -> int:
        """Reset all FAILED (not FAILED_FINAL) entries back to PENDING for retry."""
        with self._session() as session:
            rows = session.query(SendLog).filter_by(status=SendStatus.FAILED).all()
            for r in rows:
                r.status = SendStatus.PENDING
            session.commit()
            return len(rows)

    def get_sample_customers(self, limit: int = 5) -> list:
        """Return first N customer first names — for --preview display only."""
        with self._session() as session:
            return [c.first_name for c in session.query(Customer).limit(limit).all()]


# ==============================================================
# SECTION 5: DATA READER
# ==============================================================
# Reads the Excel file, filters by product keyword, normalizes
# Nigerian phone numbers to E.164, cleans customer names.
# Returns a list of dicts ready for db.upsert_customer().
#
# Handles all the messy formats in the real Excel file:
#   - Float phone numbers (WhatsApp Number column)
#   - "+0" prefixed phones (wrong format but common)
#   - HTML in product names (<br>, \n, &amp;)
#   - All-caps names, honorifics, single-word names
#
# After --setup, always run --preview to confirm the count.
# ==============================================================

class DataReader:
    """
    Reads and processes the customer Excel file.

    Filtering: rows where product column contains target_product keyword.
    Phone norm: all formats → Nigerian E.164 (234XXXXXXXXXX = 13 digits).
    Name clean: extract first name, strip honorifics, title case.
    """

    # Honorifics to strip from the front of names (case-insensitive)
    HONORIFICS = {
        "alhaja", "alhaji", "chief", "pastor", "barr", "engr",
        "prof", "rev", "miss", "mrs", "mr", "ms", "dr"
    }

    # Strip HTML tags and whitespace noise from product column
    HTML_RE = re.compile(
        r"<br\s*/?>|&amp;|&nbsp;|&lt;|&gt;|\r\n|\r|\n",
        re.IGNORECASE
    )

    def __init__(self, country_code: str = "234"):
        self._cc  = country_code
        self._log = logging.getLogger(self.__class__.__name__)

    def read_and_filter(self, excel_path: Path, target_product: str) -> list:
        """
        Main entry point. Read Excel, filter, clean, return list of customer dicts.
        Each dict has keys matching the Customer ORM model columns.
        Never raises on a bad row — logs the error and continues.
        """
        self._log.info(f"Reading: {excel_path}")

        if not excel_path.exists():
            raise FileNotFoundError(
                f"Excel file not found at: {excel_path}\n"
                "Drop your file into the data/ folder."
            )

        # Read twice:
        #   df_str: all columns as strings (prevents phone numbers becoming floats)
        #   df_raw: original types (needed for WhatsApp Number float and Order Date)
        df_str = pd.read_excel(excel_path, dtype=str, engine="openpyxl")
        df_raw = pd.read_excel(excel_path, engine="openpyxl")

        self._log.info(f"Rows: {len(df_str)} | Columns: {df_str.columns.tolist()}")

        customers       = []
        skipped_product = 0
        invalid_phones  = 0

        for idx in range(len(df_str)):
            try:
                row     = df_str.iloc[idx]
                row_raw = df_raw.iloc[idx]

                # ── Filter by product ──────────────────────────────
                product_raw   = str(row.get("Product", "") or "")
                product_clean = self._strip_html(product_raw)

                if target_product.lower() not in product_clean.lower():
                    skipped_product += 1
                    self._log.debug(
                        f"Row {idx}: skipped — product='{product_clean[:40]}'"
                    )
                    continue

                # ── Order ID ───────────────────────────────────────
                order_id = str(row.get("Order ID", "") or "").strip()
                if not order_id:
                    self._log.warning(f"Row {idx}: missing Order ID — skipping")
                    continue

                # ── Name ───────────────────────────────────────────
                full_name  = str(row.get("Name", "") or "").strip()
                first_name = self._clean_name(full_name)

                # ── Phones ─────────────────────────────────────────
                # WhatsApp Number is stored as a float in the raw read
                wa_raw  = row_raw.get("WhatsApp Number") if "WhatsApp Number" in row_raw.index else None
                std_raw = str(row.get("Phone Number", "") or "").strip()

                norm_wa,  wa_ok  = self._normalize(wa_raw,  is_float=True)
                norm_std, std_ok = self._normalize(std_raw, is_float=False)

                # Priority: WhatsApp Number > Phone Number > invalid
                if wa_ok:
                    normalized_phone = norm_wa
                    raw_phone        = str(wa_raw)
                    phone_valid      = True
                elif std_ok:
                    normalized_phone = norm_std
                    raw_phone        = std_raw
                    phone_valid      = True
                else:
                    normalized_phone = None
                    raw_phone        = std_raw or str(wa_raw or "")
                    phone_valid      = False
                    invalid_phones  += 1
                    self._log.warning(
                        f"Row {idx} ({first_name}): phone unresolvable — "
                        f"WA={wa_raw}, STD={std_raw}"
                    )

                # ── Order Date ─────────────────────────────────────
                order_date = None
                raw_date   = row_raw.get("Order Date") if "Order Date" in row_raw.index else None
                if raw_date is not None and not pd.isnull(raw_date):
                    try:
                        order_date = pd.to_datetime(raw_date).to_pydatetime()
                    except Exception:
                        pass  # Non-critical — sorting still works without it

                customers.append({
                    "order_id":         order_id,
                    "customer_name":    full_name,
                    "first_name":       first_name,
                    "raw_phone":        raw_phone,
                    "normalized_phone": normalized_phone,
                    "product_raw":      product_raw,
                    "product_clean":    product_clean,
                    "order_date":       order_date,
                    "phone_valid":      phone_valid,
                })

            except Exception as e:
                # A single bad row must never crash the whole import
                self._log.error(f"Row {idx}: unexpected error — {e}", exc_info=True)
                continue

        self._log.info(
            f"Import done: {len(customers)} matched '{target_product}', "
            f"{skipped_product} skipped (other products), "
            f"{invalid_phones} invalid phones."
        )
        return customers

    def _strip_html(self, text: str) -> str:
        """Remove HTML tags and whitespace noise from product name strings."""
        cleaned = self.HTML_RE.sub(" ", text)
        return " ".join(cleaned.split()).strip()

    def _normalize(self, raw, is_float: bool = False) -> tuple:
        """
        Normalize a phone number to Nigerian E.164: 234XXXXXXXXXX (13 digits).
        Returns: (normalized_string_or_None, is_valid_bool)

        Handles all formats found in the real Excel file:
          8068526757.0   (float)  → "2348068526757"
          +08038365784            → "2348038365784"
          +2348053968527          → "2348053968527"
          08130571075             → "2348130571075"
          2.348077e+12  (float)   → int conversion first
        """
        cc = self._cc

        # ── Handle float/NaN values (WhatsApp Number column) ──────
        if is_float:
            if raw is None:
                return None, False
            try:
                if pd.isnull(raw):
                    return None, False
            except (TypeError, ValueError):
                pass
            try:
                raw = str(int(float(raw)))
            except (ValueError, TypeError, OverflowError):
                return None, False
        else:
            if not raw or str(raw).strip() in ("", "nan", "None", "NaN"):
                return None, False
            raw = str(raw).strip()

        # ── Strip all formatting characters, note the leading + ───
        has_plus = raw.startswith("+")
        digits   = re.sub(r"[^\d]", "", raw)  # Digits only

        # ── Normalize rules applied in order ──────────────────────

        # Rule 1: "+0XXXXXXXXX" — strip the leading zero
        if has_plus and digits.startswith("0"):
            digits = digits[1:]

        # Rule 2: starts with "0" (local format, no +) — strip zero, add CC
        if digits.startswith("0") and not digits.startswith(cc):
            digits = digits[1:]

        # Rule 3: 10-digit number (no leading zero, no CC) — add CC
        if len(digits) == 10 and not digits.startswith(cc):
            digits = cc + digits

        # Rule 4: still 10 digits (after zero strip hit a 9-digit) — add CC
        if len(digits) == 10:
            digits = cc + digits

        # ── Validate: must be exactly 13 digits starting with CC ──
        if len(digits) == 13 and digits.startswith(cc):
            return digits, True

        self._log.debug(
            f"Phone normalization failed: raw={raw!r} → digits={digits!r} "
            f"(length={len(digits)}, expected 13)"
        )
        return None, False

    def _clean_name(self, full_name: str) -> str:
        """
        Extract first name for the 'Hi {first_name}!' greeting.

        Steps:
          1. Strip whitespace
          2. Remove leading honorifics (Mr, Mrs, Dr, Alhaja, Chief, etc.)
          3. Take the first remaining word
          4. Title case
          5. Fall back to original full name if nothing remains
        """
        if not full_name:
            return "Customer"

        parts = full_name.strip().split()

        # Strip honorifics from the front
        while parts and parts[0].lower().rstrip(".") in self.HONORIFICS:
            parts.pop(0)

        if not parts:
            return full_name.strip().title()

        return parts[0].strip().title()


# ==============================================================
# SECTION 6: MESSAGE TEMPLATES
# ==============================================================
# Two Jinja2 template strings. Defined here so the system is
# truly single-file with no external template folder needed.
#
# TEMPLATE A — "Results Check-In"
#   Opens with curiosity about their skin results.
#   Never uses "review" in the opener — avoids corporate feel.
#   Voice note mention lowers the effort barrier significantly.
#
# TEMPLATE B — "Honest Feedback"
#   Leads with "not looking for a perfect 5-star review".
#   This removes pressure and paradoxically gets more replies.
#   Acknowledges the product might not have worked for everyone.
#
# Both templates:
#   - Use first_name (personalized greeting and sign-off)
#   - Put discount offer LAST (not leading with a bribe)
#   - Are short-paragraph, WhatsApp-appropriate length
#   - Suggest voice note (most customers find it easier than typing)
#
# Variables: {{ first_name }}, {{ discount_offer }}, {{ review_link }}
# ==============================================================

TEMPLATE_A = """\
Hi {{ first_name }}! 👋

It's been a few months since you got your Sadoer Collagen Combo — just wanted to check in and see how it's been going for you? 😊

Have you noticed any changes to your skin since using it?

Your honest experience means a lot — even a quick voice note works perfectly 🎙️
{% if review_link %}
You can also drop your thoughts here: {{ review_link }}
{% endif %}
As a little thank you, I'll send you {{ discount_offer }} on your next order once you share 🎁

Looking forward to hearing from you, {{ first_name }}! 🙏"""


TEMPLATE_B = """\
Hi {{ first_name }} 🙏

Quick one — you ordered your Sadoer Collagen Combo a few months back, and I'd genuinely love to know what you actually think of it.

I'm not looking for a perfect 5-star review — just your real experience. Did it work for you? What did you notice?

Even a short voice note is totally fine 🎙️
{% if review_link %}
Or share here: {{ review_link }}
{% endif %}
Everyone who shares gets {{ discount_offer }} — but honestly, your feedback is what shapes how we improve for you and others 💬

Thank you, {{ first_name }}!"""


# ==============================================================
# SECTION 7: MESSAGE BUILDER
# ==============================================================
# Renders the correct template per customer.
# Randomly assigns A or B — prevents 96 identical messages
# (which is a WhatsApp spam signal AND lowers reply rates).
# Tracks last template used so the same one is never chosen
# more than twice in a row.
# ==============================================================

class MessageBuilder:
    """
    Renders personalized WhatsApp messages from Jinja2 templates.
    Alternates randomly between Template A and B across customers.

    Usage:
        builder = MessageBuilder(cfg)
        message, label = builder.build(customer_dict)
        # label is 'A' or 'B' — saved in DB for reply-rate analytics

        both = builder.preview(customer_dict)
        # {"A": "...", "B": "..."} — used by --dry-run command
    """

    def __init__(self, cfg: AppConfig):
        self._cfg  = cfg
        self._log  = logging.getLogger(self.__class__.__name__)
        self._last = None  # Tracks last template to prevent long runs of same template

        env = Environment(undefined=Undefined)
        self._templates = {
            "A": env.from_string(TEMPLATE_A),
            "B": env.from_string(TEMPLATE_B),
        }

    def _render(self, label: str, customer: dict) -> str:
        """Render one template with customer data and config values."""
        rendered = self._templates[label].render(
            first_name    =customer.get("first_name", "Customer"),
            discount_offer=self._cfg.discount_offer,
            review_link   =self._cfg.review_link,
        )
        # Collapse 3+ consecutive blank lines into 2 (clean output)
        return re.sub(r"\n{3,}", "\n\n", rendered).strip()

    def build(self, customer: dict) -> tuple:
        """
        Choose a template, render it, return (message_string, label).
        If same template was used last time, flip to the other one.
        This ensures variation without being a perfect alternation.
        """
        choice = random.choice(["A", "B"])
        if choice == self._last:
            choice = "B" if choice == "A" else "A"
        self._last = choice

        msg = self._render(choice, customer)
        self._log.debug(
            f"Template {choice} → {customer.get('first_name')} "
            f"({len(msg)} chars)"
        )
        return msg, choice

    def preview(self, customer: dict) -> dict:
        """Render both templates for one customer. Used by --dry-run."""
        return {"A": self._render("A", customer), "B": self._render("B", customer)}


# ==============================================================
# SECTION 8: ABSTRACT SENDER INTERFACE
# ==============================================================
# Defines WHAT a WhatsApp sender must do.
# Does NOT define HOW — that's Section 9 (Playwright).
#
# WHY THIS ARCHITECTURE MATTERS:
# Sections 11 and 13 only talk to this interface.
# To switch from WhatsApp Web to the official API:
#   1. Write a new class that extends WhatsAppSender
#   2. Change one line in Section 13 (cmd_run)
#   3. Nothing else in the system needs to change
#
# SendResult is the standard return type from all implementations.
# ==============================================================

@dataclass
class SendResult:
    """Standard result object returned by every sender implementation."""
    success:         bool
    status:          str           # 'SENT' | 'FAILED' | 'INVALID_NUMBER'
    error_message:   str = ""      # Empty on success
    screenshot_path: str = ""      # Path to screenshot proof, if taken
    timestamp:       datetime = field(default_factory=datetime.now)


class WhatsAppSender(ABC):
    """
    Abstract base class for all WhatsApp sender implementations.

    Current: PlaywrightSender (Section 9) — WhatsApp Web browser automation
    Future:  CloudAPISender — official WhatsApp Business Cloud API

    Swap method:
        class CloudAPISender(WhatsAppSender):
            async def connect(self): ...   # authenticate with API
            async def send_text(self, ...): ...  # POST to /messages endpoint
            # etc.
        Then in cmd_run(): sender = CloudAPISender(cfg)
    """

    @abstractmethod
    async def connect(self) -> bool:
        """Open browser / authenticate. Return True if ready to send."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection cleanly. Never delete .sessions/ folder."""
        pass

    @abstractmethod
    async def send_text(self, phone: str, message: str, order_id: str) -> SendResult:
        """Send text message. order_id used for screenshot filename."""
        pass

    @abstractmethod
    async def send_image(self, phone: str, image_path: str,
                         caption: str, order_id: str) -> SendResult:
        """Send image with caption text."""
        pass

    @abstractmethod
    async def is_connected(self) -> bool:
        """Return True if session is still alive (not expired)."""
        pass


# ==============================================================
# SECTION 9: PLAYWRIGHT SENDER — STEALTH IMPLEMENTATION
# ==============================================================
# Controls a real Chromium browser to automate WhatsApp Web.
# Every action is built to look like a human being at the keyboard.
#
# 8 STEALTH LAYERS:
#   1. Typing: page.type() char-by-char — never page.fill()
#      page.fill() generates no keyboard events → instantly detectable
#
#   2. Pre-type pause: wait 3-8s after chat loads before touching input
#      Simulates: user arrives, reads chat, then starts typing
#
#   3. Human mouse: move to random position first, then to input
#      Never teleport directly to the input box
#
#   4. URL: no ?text= parameter — navigate clean, type manually
#      ?text= pre-fills text which humans don't do
#
#   5. Viewport: 1280×800, headless=False always
#      WhatsApp Web behaves differently in headless mode
#
#   6. Emoji: inject via JavaScript, not page.type()
#      page.type() garbles multi-byte emoji on some systems
#
#   7. Scroll: occasionally scroll up then back down before typing
#      Simulates reading old messages before replying
#
#   8. Tab rotation: navigate home every 8-12 messages
#      Resets cumulative session-level behaviour patterns
#
# SESSION PERSISTENCE:
#   .sessions/whatsapp_session/ stores cookies and localStorage.
#   First run: QR appears, scan with phone, session saved.
#   Every run after: session loads silently, no QR needed.
# ==============================================================

class PlaywrightSender(WhatsAppSender):
    """
    WhatsApp Web automation via Playwright async API.
    Implements all 8 stealth behaviours.

    First run: browser opens → scan QR with phone → session saved
    All later runs: session loads automatically, no QR needed
    """

    # CSS selectors for WhatsApp Web UI elements.
    # Update these if WhatsApp changes their HTML structure.
    SEL = {
        "chat_list":     'div[aria-label="Chat list"]',
        "qr_code":       'canvas[aria-label="Scan me!"], div[data-ref]',
        "msg_input":     'div[aria-label="Type a message"]',
        "sent_tick":     'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"]',
        "attach_btn":    'span[data-icon="attach-menu-plus"]',
        "caption_input": 'div[aria-label="Add a caption"]',
    }

    # Text patterns that indicate a number is not on WhatsApp
    INVALID_TEXTS = [
        "phone number shared via url is invalid",
        "not on whatsapp",
        "invalid phone number",
    ]

    def __init__(self, cfg: AppConfig):
        self._cfg       = cfg
        self._log       = logging.getLogger(self.__class__.__name__)
        self._sess_path = Path(".sessions") / "whatsapp_session"
        self._ss_path   = Path("screenshots")
        self._pw        = None   # Playwright instance
        self._ctx       = None   # Browser context
        self._page      = None   # Active page

        # STEALTH 8: Track messages per tab for rotation
        self._msgs_on_tab  = 0
        self._rotate_after = random.randint(8, 12)

    async def connect(self) -> bool:
        """
        Launch browser with persistent session.
        Waits for chat list (session loaded) or QR code (first run).
        STEALTH 5: headless=False, viewport 1280×800.
        """
        from playwright.async_api import async_playwright

        self._log.info("Launching browser...")
        self._sess_path.mkdir(parents=True, exist_ok=True)
        self._ss_path.mkdir(exist_ok=True)

        self._pw = await async_playwright().start()

        # Persistent context = browser remembers login between runs
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._sess_path),
            headless=False,                                   # STEALTH 5
            viewport={"width": 1280, "height": 800},          # STEALTH 5
            locale="en-US",
            timezone_id="Africa/Lagos",
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # Hide Playwright flag
                "--disable-infobars",
            ],
        )

        self._page = (
            self._ctx.pages[0]
            if self._ctx.pages
            else await self._ctx.new_page()
        )

        self._log.info("Navigating to WhatsApp Web...")
        await self._page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

        try:
            # Wait for either chat list (good) or QR code (need to scan)
            await self._page.wait_for_selector(
                f"{self.SEL['chat_list']}, {self.SEL['qr_code']}",
                timeout=30_000
            )

            # Which one appeared?
            chat_list = await self._page.query_selector(self.SEL["chat_list"])

            if chat_list:
                self._log.info("✅ Session loaded — no QR scan needed.")
                return True

            # QR code appeared — need user to scan
            print("\n" + "="*55)
            print("  📱 SCAN QR CODE TO LOG IN")
            print("="*55)
            print("  1. Open WhatsApp on your phone")
            print("  2. Tap Menu (⋮) → Linked Devices → Link a Device")
            print("  3. Scan the QR code shown in the browser window")
            print("  You have 2 minutes to scan.")
            print("="*55 + "\n")

            # Wait up to 2 minutes for the scan to complete
            await self._page.wait_for_selector(
                self.SEL["chat_list"],
                timeout=120_000
            )
            self._log.info("✅ QR scanned. Session saved for future runs.")
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
        IMPORTANT: Do not delete .sessions/ — it contains the saved login.
        """
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
            self._log.info("Browser closed.")
        except Exception as e:
            self._log.error(f"Disconnect error: {e}")

    async def is_connected(self) -> bool:
        """Check if the WhatsApp session is still alive and showing the chat list."""
        try:
            if not self._page:
                return False
            el = await self._page.query_selector(self.SEL["chat_list"])
            return el is not None
        except Exception:
            return False

    async def _rotate_tab(self):
        """
        STEALTH 8: Navigate home and pause after N messages.
        Resets session-level browsing patterns that could be fingerprinted.
        N is randomized per cycle so rotation is not at a predictable interval.
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
            await asyncio.sleep(random.uniform(3, 6))
            self._msgs_on_tab  = 0
            self._rotate_after = random.randint(8, 12)
            self._log.info(f"Rotated. Next rotation after {self._rotate_after} messages.")

    async def _mouse_human_to(self, target_x: int, target_y: int):
        """
        STEALTH 3: Move mouse via a random intermediate point.
        Never teleport directly to a UI element from the current position.
        Random pause at the intermediate point simulates natural movement.
        """
        mid_x = random.randint(150, 1100)
        mid_y = random.randint(80, 650)
        await self._page.mouse.move(mid_x, mid_y)
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await self._page.mouse.move(target_x, target_y)
        await asyncio.sleep(random.uniform(0.1, 0.3))

    async def _type_human(self, selector: str, message: str):
        """
        STEALTH 1 + STEALTH 6: Type message as a human would.

        Text segments: typed character by character with random delays.
        Emoji segments: injected via JavaScript (page.type mangles multi-byte emoji).
        Long messages: occasional thinking-pause mid-message.

        This is the most critical stealth method. WhatsApp Web monitors
        keyboard event timing. page.fill() sends no events — detectable.
        page.type() with random delay is behaviorally identical to a human.
        """
        # Regex to identify emoji characters (multi-byte Unicode)
        emoji_re = re.compile(
            "["
            "\U0001F600-\U0001F64F"   # Emoticons
            "\U0001F300-\U0001F5FF"   # Symbols & pictographs
            "\U0001F680-\U0001F6FF"   # Transport & map
            "\U0001F1E0-\U0001F1FF"   # Flags
            "\U00002702-\U000027B0"   # Dingbats
            "\U000024C2-\U0001F251"
            "\U0001f926-\U0001f937"
            "\U00010000-\U0010ffff"
            "\u2640-\u2642"
            "\u2600-\u2B55"
            "\u200d\u23cf\u23e9\u231a\ufe0f\u3030"
            "]+",
            flags=re.UNICODE
        )

        # Split message into alternating text/emoji segments
        text_parts  = emoji_re.split(message)
        emoji_parts = emoji_re.findall(message)

        parts = []
        for i, text in enumerate(text_parts):
            if text:
                parts.append(("text", text))
            if i < len(emoji_parts):
                parts.append(("emoji", emoji_parts[i]))

        delays    = self._cfg.delays
        char_count = 0

        for kind, content in parts:
            if kind == "text":
                for char in content:
                    await self._page.type(
                        selector,
                        char,
                        delay=random.uniform(delays.type_speed_min, delays.type_speed_max)
                    )
                    char_count += 1

                    # STEALTH 1: Thinking pause mid-message for longer messages
                    if len(message) > 80 and char_count % random.randint(40, 60) == 0:
                        await asyncio.sleep(random.uniform(0.8, 2.0))

            elif kind == "emoji":
                # STEALTH 6: Inject emoji via JavaScript to prevent garbled output
                try:
                    await self._page.evaluate(
                        """(args) => {
                            const el = document.querySelector(args.selector);
                            if (!el) return;
                            const sel = window.getSelection();
                            if (!sel.rangeCount) return;
                            const range = sel.getRangeAt(0);
                            const node  = document.createTextNode(args.emoji);
                            range.insertNode(node);
                            range.setStartAfter(node);
                            range.setEndAfter(node);
                            sel.removeAllRanges();
                            sel.addRange(range);
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                        }""",
                        {"selector": selector, "emoji": content}
                    )
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                except Exception:
                    # Fallback: try direct type (may garble, but better than crashing)
                    await self._page.type(selector, content, delay=50)

    async def _is_invalid_number(self) -> bool:
        """Check page text for WhatsApp 'not registered' error messages."""
        try:
            body = (await self._page.inner_text("body")).lower()
            return any(pattern in body for pattern in self.INVALID_TEXTS)
        except Exception:
            return False

    async def _screenshot(self, order_id: str) -> str:
        """Take and save a screenshot. Returns path string or empty string on failure."""
        try:
            ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_id = re.sub(r"[^\w\-]", "_", str(order_id))
            path    = self._ss_path / f"{safe_id}_{ts}.png"
            await self._page.screenshot(path=str(path), full_page=False)
            self._log.debug(f"Screenshot: {path}")
            return str(path)
        except Exception as e:
            self._log.warning(f"Screenshot failed: {e}")
            return ""

    async def send_text(self, phone: str, message: str, order_id: str) -> SendResult:
        """
        Send a text message to one phone number.
        Applies all 8 stealth techniques in this exact flow:
          1. Tab rotation check      (STEALTH 8)
          2. Navigate clean URL      (STEALTH 4)
          3. Check invalid number
          4. Pre-typing pause        (STEALTH 2)
          5. Random scroll           (STEALTH 7)
          6. Human mouse to input    (STEALTH 3)
          7. Type message            (STEALTH 1 + 6)
          8. Send, confirm, screenshot
        """
        self._log.info(f"→ Sending to +{phone} [{order_id}]")

        try:
            # STEALTH 8: Rotate tab if threshold reached
            await self._rotate_tab()

            # STEALTH 4: Navigate WITHOUT pre-filling text in URL
            await self._page.goto(
                f"https://web.whatsapp.com/send?phone={phone}",
                wait_until="domcontentloaded",
                timeout=20_000
            )

            # Wait for chat to load (up to 15 seconds)
            try:
                await self._page.wait_for_selector(
                    self.SEL["msg_input"],
                    timeout=15_000
                )
            except Exception:
                if await self._is_invalid_number():
                    self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )
                raise

            # Check again after load (modal can appear after chat opens)
            if await self._is_invalid_number():
                self._log.info(f"  ✗ Not on WhatsApp: +{phone}")
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            # STEALTH 2: Pre-typing pause — simulate reading the chat
            pre = random.uniform(
                self._cfg.delays.pre_type_min,
                self._cfg.delays.pre_type_max
            )
            self._log.debug(f"  Pre-type pause: {pre:.1f}s")
            await asyncio.sleep(pre)

            # STEALTH 7: Occasionally scroll up then back (reading old messages)
            if random.choice([True, False]):
                scroll_px = random.uniform(100, 400)
                await self._page.evaluate(f"window.scrollBy(0, -{scroll_px})")
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await self._page.evaluate("window.scrollBy(0, 10000)")
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # STEALTH 3: Human mouse movement before clicking input
            el = await self._page.query_selector(self.SEL["msg_input"])
            if el:
                box = await el.bounding_box()
                if box:
                    cx = int(box["x"] + box["width"] / 2)
                    cy = int(box["y"] + box["height"] / 2)
                    await self._mouse_human_to(cx, cy)

            await self._page.click(self.SEL["msg_input"])
            await asyncio.sleep(random.uniform(0.2, 0.5))

            # STEALTH 1 + 6: Type message character by character
            await self._type_human(self.SEL["msg_input"], message)
            await asyncio.sleep(random.uniform(0.3, 0.8))  # Pause after finishing

            # Send with Enter key
            await self._page.keyboard.press("Enter")
            self._log.debug("  Message sent. Waiting for tick...")

            # Wait for confirmation tick (single or double = delivered)
            try:
                await self._page.wait_for_selector(
                    self.SEL["sent_tick"],
                    timeout=15_000
                )
                self._log.info(f"  ✓ Delivered: +{phone}")
            except Exception:
                # Tick timed out — message likely sent but unconfirmed
                self._log.warning(
                    f"  ⚠ Tick timeout for +{phone} — "
                    "message likely sent (unconfirmed)"
                )

            ss = await self._screenshot(order_id)
            self._msgs_on_tab += 1

            return SendResult(success=True, status="SENT", screenshot_path=ss)

        except Exception as e:
            self._log.error(f"  ✗ Failed for +{phone}: {e}", exc_info=True)
            return SendResult(success=False, status="FAILED", error_message=str(e))

    async def send_image(self, phone: str, image_path: str,
                         caption: str, order_id: str) -> SendResult:
        """
        Send an image with caption. Used when cfg.image_path is set.
        Clicks attachment button, uploads file, types caption, sends.
        """
        self._log.info(f"→ Sending image to +{phone} [{order_id}]")

        try:
            await self._rotate_tab()

            await self._page.goto(
                f"https://web.whatsapp.com/send?phone={phone}",
                wait_until="domcontentloaded",
                timeout=20_000
            )
            await self._page.wait_for_selector(self.SEL["msg_input"], timeout=15_000)

            if await self._is_invalid_number():
                return SendResult(
                    success=False,
                    status="INVALID_NUMBER",
                    error_message="Phone not registered on WhatsApp"
                )

            # Click the attachment (paperclip) button
            await self._page.click(self.SEL["attach_btn"])
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Upload via file chooser
            async with self._page.expect_file_chooser() as fc_info:
                await self._page.click('input[accept*="image"]')
            fc = await fc_info.value
            await fc.set_files(image_path)

            # Wait for image preview to appear
            await self._page.wait_for_selector(
                'div[data-testid="media-editor"]',
                timeout=10_000
            )
            await asyncio.sleep(1.0)

            # Type caption
            try:
                await self._page.click(self.SEL["caption_input"])
                await self._type_human(self.SEL["caption_input"], caption)
            except Exception:
                self._log.warning("Caption input not found — sending without caption")

            await self._page.keyboard.press("Enter")
            await asyncio.sleep(2.0)

            ss = await self._screenshot(order_id)
            self._msgs_on_tab += 1

            return SendResult(success=True, status="SENT", screenshot_path=ss)

        except Exception as e:
            self._log.error(f"Image send failed for +{phone}: {e}", exc_info=True)
            return SendResult(success=False, status="FAILED", error_message=str(e))


# ==============================================================
# SECTION 10: REPORTER
# ==============================================================
# Generates a plain text daily report from DB summary data.
# Saves to reports/daily_report_YYYY-MM-DD.txt.
# Optionally emails it via Gmail SMTP (if configured).
#
# IMPORTANT: email failure NEVER crashes the system.
# It logs the error and returns False — the automation continues.
# ==============================================================

class Reporter:
    """
    Generates daily reports and emails them.

    Usage:
        reporter = Reporter()
        text = reporter.generate_report(db)    # Always works
        reporter.send_email(text, cfg)          # Only if SMTP configured
    """

    def __init__(self):
        self._log = logging.getLogger(self.__class__.__name__)

    def generate_report(self, db: Database) -> str:
        """
        Build a plain text daily summary from DB data.
        Saves to reports/ and returns the string.
        """
        s         = db.get_daily_summary()
        today_str = date.today().strftime("%Y-%m-%d")

        lines = [
            f"WhatsApp Automation Report — {today_str}",
            "=" * 50,
            "",
            "SUMMARY",
            f"  Sent:            {s.get(SendStatus.SENT, 0)}",
            f"  Failed:          {s.get(SendStatus.FAILED, 0)}",
            f"  Failed (final):  {s.get(SendStatus.FAILED_FINAL, 0)}",
            f"  Invalid number:  {s.get(SendStatus.INVALID_NUMBER, 0)}",
            f"  Pending:         {s.get(SendStatus.PENDING, 0)}",
            "",
            "TEMPLATE PERFORMANCE",
            f"  Template A:  {s.get('template_A', 0)} messages",
            f"  Template B:  {s.get('template_B', 0)} messages",
            "",
        ]

        # Failed details
        failed = s.get("failed_details", [])
        if failed:
            lines.append("FAILED (eligible for retry — run --reset-failed)")
            for item in failed:
                lines.append(
                    f"  {item['name']} — +{item['phone']} — {item['error_message']}"
                )
        else:
            lines.append("FAILED: None 🎉")

        lines.append("")

        # Invalid number details
        invalid = s.get("invalid_details", [])
        if invalid:
            lines.append("INVALID NUMBERS (not on WhatsApp)")
            for item in invalid:
                lines.append(f"  {item['name']} — {item['phone']}")
        else:
            lines.append("INVALID NUMBERS: None")

        lines += [
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        report_text = "\n".join(lines)

        # Save to file
        Path("reports").mkdir(exist_ok=True)
        report_file = Path("reports") / f"daily_report_{today_str}.txt"
        report_file.write_text(report_text, encoding="utf-8")
        self._log.info(f"Report saved: {report_file}")

        return report_text

    def send_email(self, report_text: str, cfg: AppConfig) -> bool:
        """
        Email the daily report via Gmail SMTP with STARTTLS.
        Returns True if sent, False if failed.
        NEVER raises — email failure must not crash the automation.
        """
        if not cfg.has_email():
            self._log.info("Email not configured — skipping.")
            return False

        try:
            today_str   = date.today().strftime("%Y-%m-%d")
            report_file = Path("reports") / f"daily_report_{today_str}.txt"

            msg              = MIMEMultipart()
            msg["From"]      = cfg.smtp_email
            msg["To"]        = cfg.smtp_to
            msg["Subject"]   = f"WhatsApp Automation Report — {today_str}"
            msg.attach(MIMEText(report_text, "plain"))

            # Attach the saved report file
            if report_file.exists():
                with open(report_file, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f"attachment; filename={report_file.name}"
                    )
                    msg.attach(part)

            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(cfg.smtp_email, cfg.smtp_password)
                smtp.sendmail(cfg.smtp_email, cfg.smtp_to, msg.as_string())

            self._log.info(f"Report emailed to {cfg.smtp_to}")
            return True

        except Exception as e:
            # Log the error — do NOT re-raise
            self._log.error(f"Email failed: {e}")
            return False


# ==============================================================
# SECTION 11: SCHEDULER
# ==============================================================
# The orchestration layer — runs the full sending day.
# Uses APScheduler to trigger 6 sessions at configured times.
# Each session: fetch batch → send each → apply delays → update DB.
#
# Human-like delay pattern per session:
#   Send 4 msgs → burst pause (4-8 min)
#   Between every msg: 55-110s + random jitter
#   Once randomly per session: 8-15 min long pause
#   Minimum delay: 30 seconds always enforced
#
# After last session: triggers Reporter for end-of-day summary.
# ==============================================================

class Scheduler:
    """
    Runs the full sending day. Connects every module together.

    AppConfig  → timing, delay settings, daily limit
    Database   → fetch pending, record results
    Sender     → WhatsApp Web automation
    Builder    → personalized message per customer
    Reporter   → end-of-day summary
    """

    def __init__(self, cfg: AppConfig, db: Database, sender: WhatsAppSender,
                 builder: MessageBuilder, reporter: Reporter):
        self._cfg      = cfg
        self._db       = db
        self._sender   = sender
        self._builder  = builder
        self._reporter = reporter
        self._apscheduler = AsyncIOScheduler()
        self._log      = logging.getLogger(self.__class__.__name__)
        self._last_idx = len(cfg.session_schedule) - 1  # Index of final session

    async def run_session(self, session_idx: int, session_count: int):
        """
        Execute one sending session.
        Called by APScheduler at the scheduled time, or by run_now() for testing.

        Args:
            session_idx:   Which session number (0-based). Used to detect last.
            session_count: How many messages to send.
        """
        self._log.info(
            f"\n{'='*50}\n"
            f"Session {session_idx + 1} — {session_count} messages\n"
            f"{'='*50}"
        )

        # Verify connection before starting — reconnect if session expired
        if not await self._sender.is_connected():
            self._log.warning("Session disconnected — attempting reconnect...")
            try:
                await self._sender.connect()
            except Exception as e:
                self._log.error(
                    f"Reconnect failed: {e}\n"
                    f"Skipping session {session_idx + 1}."
                )
                return

        # Fetch next batch
        customers = self._db.get_pending(
            limit=session_count,
            order=self._cfg.send_order
        )

        if not customers:
            self._log.info("No pending customers for this session.")
            if session_idx == self._last_idx:
                await self._end_of_day()
            return

        self._log.info(f"Fetched {len(customers)} customers.")

        d = self._cfg.delays
        sent_n    = failed_n    = invalid_n = 0
        long_used = False  # One long pause max per session

        for i, customer in enumerate(customers):
            order_id = customer["order_id"]
            phone    = customer["normalized_phone"]
            name     = customer["first_name"]
            is_last  = (i == len(customers) - 1)

            # Deduplication: check again just before sending
            if self._db.already_sent(order_id):
                self._log.info(f"  Skip (already sent): {name}")
                continue

            # Build personalized message
            message, template = self._builder.build(customer)
            self._log.info(
                f"  [{i+1}/{len(customers)}] {name} — "
                f"Template {template} — +{phone}"
            )

            # Send (image or text depending on config)
            if self._cfg.image_path and Path(self._cfg.image_path).exists():
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

            # Update DB based on result
            if result.status == "SENT":
                self._db.mark_sent(order_id, message, template, result.screenshot_path)
                sent_n += 1
            elif result.status == "INVALID_NUMBER":
                self._db.mark_invalid(order_id)
                invalid_n += 1
            else:
                self._db.mark_failed(order_id, result.error_message)
                failed_n += 1

            # Skip delays after the last message in this session
            if is_last:
                continue

            # Base delay between messages with jitter
            base  = random.uniform(d.between_messages_min, d.between_messages_max)
            jitter = random.uniform(d.jitter_min, d.jitter_max)
            delay  = max(30, base + jitter)  # Never below 30 seconds
            self._log.info(f"  Waiting {delay:.0f}s...")
            await asyncio.sleep(delay)

            # Burst pause after every burst_size messages
            if (i + 1) % d.burst_size == 0:
                burst = random.uniform(d.after_burst_min, d.after_burst_max)
                self._log.info(
                    f"  Burst pause ({d.burst_size} msgs) — "
                    f"waiting {burst:.0f}s ({burst/60:.1f} min)..."
                )
                await asyncio.sleep(burst)

            # Random long pause once per session (30% chance, not near end)
            if (
                d.long_pause_enabled
                and not long_used
                and random.random() < 0.30
                and i < len(customers) - 3
            ):
                lp = random.uniform(d.long_pause_min, d.long_pause_max)
                self._log.info(
                    f"  Long break — waiting {lp:.0f}s ({lp/60:.1f} min)..."
                )
                await asyncio.sleep(lp)
                long_used = True

        self._log.info(
            f"Session {session_idx + 1} done: "
            f"{sent_n} sent, {failed_n} failed, {invalid_n} invalid."
        )

        # End-of-day tasks after final session
        if session_idx == self._last_idx:
            await self._end_of_day()

    async def _end_of_day(self):
        """Generate and send the daily report after the last session."""
        self._log.info("All sessions complete. Generating report...")
        report = self._reporter.generate_report(self._db)
        print("\n" + report)

        if self._cfg.has_email():
            self._reporter.send_email(report, self._cfg)

        retries = self._db.get_retry_eligible()
        if retries:
            self._log.info(
                f"{len(retries)} messages eligible for retry. "
                "Run --reset-failed to queue them for next run."
            )

    def start(self):
        """
        Register all sessions as APScheduler cron jobs and start.
        Prints the day's schedule. Blocks until all sessions done or Ctrl+C.
        """
        jobs = self._cfg.session_jobs()

        print("\n" + "="*50)
        print("TODAY'S SCHEDULE")
        print("="*50)
        for i, job in enumerate(jobs):
            print(
                f"  Session {i+1}: "
                f"{job['hour']:02d}:{job['minute']:02d} — "
                f"{job['count']} messages"
            )
        print(f"\n  Total: {self._cfg.total_daily_count()} messages")
        print("="*50)
        print("\n  Keep this window open.")
        print("  Do NOT open WhatsApp Web manually while running.")
        print("  Press Ctrl+C to stop.\n")

        for i, job in enumerate(jobs):
            self._apscheduler.add_job(
                self.run_session,
                trigger="cron",
                hour=job["hour"],
                minute=job["minute"],
                args=[i, job["count"]],
                id=f"session_{i}",
            )

        self._apscheduler.start()

        loop = asyncio.get_event_loop()
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            self._log.info("Stopped by user.")
        finally:
            self._apscheduler.shutdown()

    async def run_now(self, count: int):
        """Run one immediate session for testing (skips APScheduler)."""
        self._log.info(f"Immediate test session: {count} messages")
        await self.run_session(session_idx=0, session_count=count)


# ==============================================================
# SECTION 12: FOLDER AND FILE SETUP UTILITIES
# ==============================================================
# Create required directories and generate helper files.
# Called by --setup and as a silent pre-check for every command.
# ==============================================================

def create_directories():
    """
    Ensure all required directories exist.
    Uses exist_ok=True — safe to call multiple times.
    """
    for d in ["data", ".sessions", "logs", "reports", "screenshots"]:
        Path(d).mkdir(parents=True, exist_ok=True)


def write_requirements_txt():
    """
    Write requirements.txt to the project root.
    Install with: uv pip install -r requirements.txt
    NOTE: uv does not handle Playwright browser binaries.
    After uv install, separately run: playwright install chromium
    """
    content = (
        "# WhatsApp Review Automation — Python dependencies\n"
        "# Install with: uv pip install -r requirements.txt\n"
        "# Then run:     playwright install chromium\n\n"
        "playwright==1.45.0\n"
        "pandas==2.2.2\n"
        "openpyxl==3.1.4\n"
        "sqlalchemy==2.0.31\n"
        "apscheduler==3.10.4\n"
        "jinja2==3.1.4\n"
        "pillow==10.4.0\n"
        "pyyaml==6.0.1\n"
        "python-dotenv==1.0.1\n"
        "aiofiles==23.2.1\n"
    )
    Path("requirements.txt").write_text(content, encoding="utf-8")
    logger.info("requirements.txt written.")


def write_gitignore():
    """
    Write .gitignore to protect sensitive files.
    .sessions/ contains your WhatsApp login — never share it.
    """
    content = (
        "# WhatsApp login cookies — NEVER share or commit\n"
        ".sessions/\n\n"
        "# Virtual environment\n"
        ".venv/\nvenv/\n\n"
        "# Database contains customer data\n"
        "data/*.db\n\n"
        "# Logs and reports\n"
        "logs/\nreports/\nscreenshots/\n\n"
        "# Python cache\n"
        "__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\n\n"
        "# OS files\n"
        ".DS_Store\nThumbs.db\n"
    )
    Path(".gitignore").write_text(content, encoding="utf-8")
    logger.info(".gitignore written.")


# ==============================================================
# SECTION 13: CLI COMMAND FUNCTIONS
# ==============================================================
# One function per CLI command. All user interaction lives here.
# main() in Section 14 parses arguments and calls these functions.
#
# Commands:
#   --setup          First-time: import Excel, open browser, scan QR
#   --preview        Show stats and schedule (no browser, no sending)
#   --dry-run        Preview + print sample messages (no sending)
#   --run            Start the full 6-session scheduled day
#   --run --now      One immediate test session (default: 3 messages)
#   --run --now --count N  Immediate session with N messages
#   --report         Generate today's report and email it
#   --reset-failed   Reset FAILED entries back to PENDING for retry
# ==============================================================

async def cmd_setup(cfg: AppConfig):
    """
    --setup: First-time initialization.
    Creates all directories, imports Excel to DB, opens browser for QR scan.
    Run this once before ever running --run.
    """
    print("\n" + "="*55)
    print("  WHATSAPP AUTOMATION — SETUP")
    print("="*55)

    create_directories()
    write_requirements_txt()
    write_gitignore()
    print("✅ Directories and config files created.")

    # Initialize database tables
    db = Database(cfg.database_url)
    db.init()
    print("✅ Database initialized.")

    # Import Excel file
    excel_path = cfg.excel_path()
    if not excel_path.exists():
        print(f"\n❌ Excel file not found: {excel_path}")
        print(f"   Drop your file into the data/ folder as '{cfg.excel_filename}'")
        print("   Then run --setup again.")
        return

    reader    = DataReader(cfg.country_code)
    customers = reader.read_and_filter(excel_path, cfg.target_product)

    if not customers:
        print(
            f"\n⚠️  No customers matched product keyword: '{cfg.target_product}'\n"
            "   Check your Excel file and the target_product setting in CONFIG."
        )
        return

    for c in customers:
        db.upsert_customer(c)

    print(f"✅ {len(customers)} customers imported from {cfg.excel_filename}.")

    # Open browser for WhatsApp login
    print("\n" + "="*55)
    print("  Opening browser for WhatsApp login...")
    print("="*55)

    sender = PlaywrightSender(cfg)
    try:
        await sender.connect()
        print("\n✅ WhatsApp connected. Session saved.")
        print("   Future runs will load automatically — no QR scan needed.")
        print("\n✅ Setup complete. Run --preview to verify, then --dry-run.")
    finally:
        await sender.disconnect()


def cmd_preview(cfg: AppConfig):
    """
    --preview: Show customer stats and today's schedule.
    No browser, no sending. Safe to run at any time.
    """
    db = Database(cfg.database_url)
    db.init()

    stats   = db.get_stats()
    samples = db.get_sample_customers(5)

    print("\n" + "="*50)
    print("  WHATSAPP AUTOMATION — PREVIEW")
    print("="*50)
    print(f"  Target product:      {cfg.target_product}")
    print(f"  Daily limit:         {cfg.daily_limit}")
    print(f"  Send order:          {cfg.send_order}")
    print()
    print(f"  Total customers:     {stats['total']}")
    print(f"  Pending (to send):   {stats['pending']}")
    print(f"  Already sent:        {stats['sent']}")
    print(f"  Invalid numbers:     {stats['invalid']}")
    print(f"  Bad phone numbers:   {stats['invalid_phones']}")

    if samples:
        print(f"\n  Sample first names:  {', '.join(samples)}")

    print("\n  TODAY'S SCHEDULE")
    print("  " + "-"*30)
    for i, job in enumerate(cfg.session_jobs()):
        print(
            f"  Session {i+1}: "
            f"{job['hour']:02d}:{job['minute']:02d} — "
            f"{job['count']} messages"
        )
    print(f"\n  Total planned today: {cfg.total_daily_count()} messages")
    print()


def cmd_dry_run(cfg: AppConfig):
    """
    --dry-run: Preview + show both message templates for first 3 customers.
    Zero messages sent. No browser opened.
    Always run this after --setup to confirm messages look right.
    """
    cmd_preview(cfg)

    db      = Database(cfg.database_url)
    db.init()
    pending = db.get_pending(limit=3, order=cfg.send_order)

    if not pending:
        print("  No pending customers to preview messages for.")
        return

    builder = MessageBuilder(cfg)

    print("="*50)
    print("  DRY RUN — SAMPLE MESSAGES (nothing is being sent)")
    print("="*50)

    for customer in pending:
        print(f"\n{'─'*45}")
        print(
            f"  Customer: {customer['first_name']}  |  "
            f"Order: {customer['order_id']}"
        )
        print(f"{'─'*45}")

        both = builder.preview(customer)

        print("\n  [TEMPLATE A — Results Check-In]\n")
        for line in both["A"].split("\n"):
            print(f"  {line}")

        print("\n  [TEMPLATE B — Honest Feedback]\n")
        for line in both["B"].split("\n"):
            print(f"  {line}")

    print("\n" + "="*50)
    print("  DRY RUN COMPLETE — Zero messages were sent.")
    print("="*50 + "\n")


async def cmd_run(cfg: AppConfig, run_now: bool = False, count: int = 3):
    """
    --run:           Start the full 6-session scheduled day.
    --run --now:     Run one immediate test session (default 3 messages).
    --run --now --count N: Immediate session with N messages.
    """
    if cfg.daily_limit > 200:
        print(
            f"\n⚠️  WARNING: daily_limit={cfg.daily_limit} "
            f"exceeds the recommended maximum of 200.\n"
        )

    db       = Database(cfg.database_url)
    db.init()
    builder  = MessageBuilder(cfg)
    reporter = Reporter()
    sender   = PlaywrightSender(cfg)

    try:
        print("Connecting to WhatsApp Web...")
        connected = await sender.connect()
        if not connected:
            print("❌ Could not connect. Run --setup to scan QR code.")
            return

        scheduler = Scheduler(cfg, db, sender, builder, reporter)

        if run_now:
            await scheduler.run_now(count)
        else:
            scheduler.start()

    except KeyboardInterrupt:
        print("\n⚠️  Interrupted.")
    except Exception as e:
        logger.error(f"Run error: {e}", exc_info=True)
        print(f"\n❌ Error: {e}\nSee logs/automation.log for details.")
    finally:
        print("Closing browser...")
        await sender.disconnect()


def cmd_report(cfg: AppConfig):
    """
    --report: Generate today's report and optionally email it.
    No messages are sent.
    """
    db       = Database(cfg.database_url)
    db.init()
    reporter = Reporter()

    report = reporter.generate_report(db)
    print("\n" + report)

    if cfg.has_email():
        ok = reporter.send_email(report, cfg)
        if ok:
            print(f"\n✅ Report emailed to {cfg.smtp_to}")
        else:
            print("\n❌ Email failed — check logs/automation.log")
    else:
        print("\n(Email not configured — report saved to reports/ only)")


def cmd_reset_failed(cfg: AppConfig):
    """
    --reset-failed: Reset FAILED entries (not FAILED_FINAL) back to PENDING.
    Run this the next day to retry failed messages.
    """
    db    = Database(cfg.database_url)
    db.init()
    count = db.reset_failed()
    print(f"\n✅ {count} messages reset to PENDING for next --run.")


# ==============================================================
# SECTION 14: MAIN ENTRY POINT
# ==============================================================
# Parses arguments, runs startup checks, dispatches to commands.
# This is the ONLY section that executes when you run the file.
#
# Startup checks run before every command:
#   - All directories created silently if missing
#   - Config validated — clear error if anything is wrong
#   - For --run: warnings shown if limit is dangerously high
# ==============================================================

def main():
    """Entry point. Parse args, validate config, dispatch command."""

    # Logging must be set up first — before any other code runs
    setup_logging()
    log = logging.getLogger("main")

    # ── Argument parser ─────────────────────────────────────────
    parser = argparse.ArgumentParser(
        prog="python whatsapp_automation.py",
        description="WhatsApp Review Automation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage examples:
  python whatsapp_automation.py --setup
  python whatsapp_automation.py --preview
  python whatsapp_automation.py --dry-run
  python whatsapp_automation.py --run --now --count 3
  python whatsapp_automation.py --run
  python whatsapp_automation.py --report
  python whatsapp_automation.py --reset-failed
        """
    )

    parser.add_argument("--setup",        action="store_true", help="First-time setup")
    parser.add_argument("--preview",      action="store_true", help="Show stats and schedule")
    parser.add_argument("--dry-run",      action="store_true", help="Preview messages without sending")
    parser.add_argument("--run",          action="store_true", help="Start sending")
    parser.add_argument("--now",          action="store_true", help="Run immediately (with --run)")
    parser.add_argument("--count",        type=int, default=3, help="Messages for --now (default: 3)")
    parser.add_argument("--report",       action="store_true", help="Generate today's report")
    parser.add_argument("--reset-failed", action="store_true", help="Reset failed to pending")

    args = parser.parse_args()

    # Show help if no command given
    if not any([args.setup, args.preview, args.dry_run,
                args.run, args.report, args.reset_failed]):
        parser.print_help()
        sys.exit(0)

    # ── Pre-flight checks ───────────────────────────────────────
    create_directories()  # Silently ensure all folders exist

    try:
        cfg = AppConfig()
    except ValueError as e:
        print(f"\n❌ Configuration error: {e}")
        print("   Edit the CONFIG dict in Section 2 of this file.")
        sys.exit(1)

    # ── Signal handlers for clean shutdown ─────────────────────
    def _shutdown(sig, frame):
        log.info("Shutdown signal received.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Dispatch command ────────────────────────────────────────
    try:
        if args.setup:
            asyncio.run(cmd_setup(cfg))

        elif args.preview:
            cmd_preview(cfg)

        elif args.dry_run:
            cmd_dry_run(cfg)

        elif args.run:
            asyncio.run(cmd_run(cfg, run_now=args.now, count=args.count))

        elif args.report:
            cmd_report(cfg)

        elif args.reset_failed:
            cmd_reset_failed(cfg)

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        sys.exit(0)
    except Exception as e:
        log.error(f"Unexpected error: {e}", exc_info=True)
        print(f"\n❌ Error: {e}")
        print("   See logs/automation.log for full details.")
        sys.exit(1)


# ── Standard Python entry point guard ─────────────────────────
# main() only runs when the file is executed directly.
# Importing this file for testing won't trigger it.
if __name__ == "__main__":
    main()


# ==============================================================
# END OF FILE
# ==============================================================
# SECTION SUMMARY:
#   0  Imports           — all standard + third-party imports
#   1  Logging           — console + rotating file handler
#   2  CONFIG dict       — EDIT THIS to configure the system
#   3  AppConfig         — typed config loader + validation
#   4  Database          — SQLAlchemy ORM, all DB queries
#   5  DataReader        — Excel import, phone normalization, name cleaning
#   6  Templates         — Jinja2 message templates A and B
#   7  MessageBuilder    — renders templates, random A/B assignment
#   8  WhatsAppSender    — abstract interface (swap for API later)
#   9  PlaywrightSender  — WhatsApp Web automation, 8 stealth layers
#   10 Reporter          — daily report + Gmail email
#   11 Scheduler         — APScheduler, 6 sessions, human delays
#   12 Setup utilities   — create dirs, write requirements.txt, .gitignore
#   13 CLI commands      — setup, preview, dry-run, run, report, reset-failed
#   14 main()            — arg parsing, pre-flight checks, dispatch
#
# TO EXTEND:
#   New template:        Add TEMPLATE_C string, update MessageBuilder.build()
#   New country:         Change country_code in CONFIG Section 2
#   New product target:  Change target_product in CONFIG Section 2
#   Switch to API:       Write CloudAPISender(WhatsAppSender), update cmd_run()
#   Add more sessions:   Edit session_schedule in CONFIG Section 2
# ==============================================================