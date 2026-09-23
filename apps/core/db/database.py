
import logging
from datetime import datetime, date
from enum import Enum

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Integer, String, Text, create_engine, func
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

logger = logging.getLogger(__name__)


# ==============================================================
# SEND STATUS — All valid states for a send attempt
# ==============================================================
# Using an Enum prevents typos from silently creating wrong
# status strings. "SENTT" would be caught immediately.
# Using (str, Enum) lets SQLAlchemy store the string value
# directly in the database without extra conversion.
# ==============================================================

class SendStatus(str, Enum):
    PENDING        = "PENDING"
    # Customer imported from Excel. Waiting to be messaged.
    # Initial state for every new customer.

    SENT           = "SENT"
    # Message confirmed delivered via WhatsApp tick.
    # This customer will NEVER be messaged again.
    # Deduplication check: already_sent() looks for this status.

    FAILED         = "FAILED"
    # Send attempt failed with an error.
    # Eligible for retry if attempt_count < 2.
    # Becomes FAILED_FINAL after 2 failed attempts.

    FAILED_FINAL   = "FAILED_FINAL"
    # Max retries reached. No more attempts will be made.
    # Appears in the daily report for manual follow-up.

    INVALID_NUMBER = "INVALID_NUMBER"
    # WhatsApp confirmed this number is not registered.
    # Never retry — different from FAILED (network error etc.)

    RETRYING       = "RETRYING"
    # Currently mid-retry. Used to prevent duplicate retries
    # if the scheduler runs while a retry is in progress.


# ==============================================================
# ORM BASE — Parent class for all database models
# ==============================================================

class Base(DeclarativeBase):
    """
    SQLAlchemy declarative base.
    All ORM models (Customer, SendLog) inherit from this.
    Base.metadata.create_all(engine) creates all tables at once.
    """
    pass


# ==============================================================
# CUSTOMER MODEL — One row per imported customer
# ==============================================================

class Customer(Base):
    """
    Represents one customer from the Excel import.

    order_id is the unique deduplication key.
    If the same Excel is imported twice, existing customers are
    updated rather than duplicated (upsert logic in Database class).

    Relationship to SendLog:
        One customer → many send attempts (retries)
        Access via: customer.send_logs
    """
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identification ─────────────────────────────────────────
    order_id = Column(String, unique=True, nullable=False)
    # From Excel 'Order ID' column. Unique — used for deduplication.
    # If this is missing from a row, the row is skipped entirely.

    # ── Name fields ────────────────────────────────────────────
    customer_name = Column(String, nullable=False)
    # Full name exactly as it appears in the Excel file.
    # Example: "TITILAYO ADELEYE" or "Mrs Blessing Okafor"

    first_name = Column(String, nullable=False)
    # Cleaned first name used in the greeting: "Hi {first_name}!"
    # Honorifics stripped, title-cased. "Mrs Blessing Okafor" → "Blessing"

    # ── Phone fields ───────────────────────────────────────────
    raw_phone = Column(String)
    # The phone number exactly as found in the Excel file.
    # Could be "+08038365784", "8068526757.0", "+2348053968527", etc.
    # Kept for reference and troubleshooting.

    normalized_phone = Column(String)
    # Phone normalized to E.164 format: 234XXXXXXXXXX (13 digits, no +)
    # This is the value passed to WhatsApp Web URL: ?phone=234XXXXXXXXXX
    # None if normalization failed.

    phone_valid = Column(Boolean, default=True)
    # False if we could not normalize the phone to a valid format.
    # Customers with phone_valid=False are skipped during sending.

    # ── Product fields ─────────────────────────────────────────
    product_raw = Column(String)
    # Original product string from Excel, including any HTML tags.
    # Example: "Sadoer Collagen Combo Set<br>Color: Natural"

    product_clean = Column(String)
    # HTML stripped, whitespace collapsed, trimmed.
    # Example: "Sadoer Collagen Combo Set Color: Natural"

    # ── Date fields ────────────────────────────────────────────
    order_date = Column(DateTime)
    # From Excel 'Order Date' column. Used for send_order="recent_first".
    # None if column is missing or unparseable (non-critical).

    imported_at = Column(DateTime, default=func.now())
    # When this customer was imported into the database.

    # ── Relationship ───────────────────────────────────────────
    send_logs = relationship("SendLog", back_populates="customer")
    # Access all send attempts for a customer: customer.send_logs


# ==============================================================
# SEND LOG MODEL — One row per send attempt
# ==============================================================

class SendLog(Base):
    """
    Tracks every send attempt for every customer.

    A customer starts with one PENDING row.
    Each attempt (initial + retries) updates this row's status.
    Up to 2 failed attempts before status becomes FAILED_FINAL.

    The status column is the authoritative record of what happened.
    Use db.get_daily_summary() to count by status for reporting.
    """
    __tablename__ = "send_log"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Foreign key ────────────────────────────────────────────
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    # Links to the customers table.

    order_id = Column(String, nullable=False)
    # Denormalized copy of customer.order_id.
    # Allows fast queries like already_sent(order_id) without a JOIN.

    # ── Status ─────────────────────────────────────────────────
    status = Column(String, nullable=False, default=SendStatus.PENDING)
    # Current state of this send attempt. See SendStatus enum above.

    # ── Message details ────────────────────────────────────────
    message_text = Column(Text)
    # The exact message string that was sent.
    # Saved after sending so we can audit what was delivered.

    template_used = Column(String)
    # Which template was used: 'A' or 'B'.
    # Saved so we can compare reply rates between templates in reports.

    image_sent = Column(Boolean, default=False)
    # True if an image was included with this send.

    # ── Retry tracking ─────────────────────────────────────────
    attempt_count = Column(Integer, default=0)
    # How many send attempts have been made for this customer.
    # 0 = never attempted. 1 = tried once. 2 = tried twice (max).
    # When attempt_count >= 2 and still failing → FAILED_FINAL.

    # ── Outcome fields ─────────────────────────────────────────
    sent_at = Column(DateTime)
    # Timestamp of successful delivery. None if not yet sent.

    error_message = Column(Text)
    # Error details if status is FAILED or FAILED_FINAL.
    # Example: "Timeout waiting for tick confirmation"

    screenshot_path = Column(String)
    # Path to screenshot taken after delivery confirmation.
    # Example: "screenshots/ORD001_20250624_081523.png"

    # ── Timestamps ─────────────────────────────────────────────
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    # ── Relationship ───────────────────────────────────────────
    customer = relationship("Customer", back_populates="send_logs")


# ==============================================================
# DATABASE CLASS — All DB operations in one place
# ==============================================================

class Database:
    """
    All database read/write operations for the automation system.

    Works with SQLite (local) and PostgreSQL (hosted).
    Switch between them by changing database_url in config.py.
    Nothing in this class needs to change.

    Usage from other modules:
        from src.database import Database
        from src.config import AppConfig

        cfg = AppConfig()
        db  = Database(cfg.database_url)
        db.init()                              # Create tables once

        db.upsert_customer({...})              # Import from Excel
        batch = db.get_pending(limit=8)        # Fetch next batch to send
        db.mark_sent(order_id, msg, tmpl, ss)  # After successful send
        db.mark_failed(order_id, error)        # After failed send
        db.mark_invalid(order_id)              # Not on WhatsApp
        db.already_sent(order_id)              # Dedup check → True/False
    """

    def __init__(self, database_url: str):
        # Create SQLAlchemy engine — connection pool, dialect, etc.
        # echo=False means SQL statements are not printed to console.
        # Set echo=True temporarily if you need to debug SQL queries.
        self._engine = create_engine(database_url, echo=False)

        # Session factory — each DB operation gets a fresh session
        self._SessionFactory = sessionmaker(bind=self._engine)

        self._log = logging.getLogger(self.__class__.__name__)

    def init(self):
        """
        Create all database tables if they don't already exist.
        Safe to call multiple times — won't overwrite existing data.
        Call this once at startup before any other DB operations.
        """
        Base.metadata.create_all(self._engine)
        self._log.info("Database tables ready.")

    def _session(self) -> Session:
        """
        Returns a new database session.
        Always use inside a 'with' block so it closes automatically:
            with self._session() as session:
                ...
        """
        return self._SessionFactory()

    # ── IMPORT ─────────────────────────────────────────────────

    def upsert_customer(self, data: dict):
        """
        Insert a new customer or update if order_id already exists.
        This allows re-importing the Excel file without creating duplicates.

        On new insert: also creates an initial PENDING send_log entry
        so the customer appears in get_pending() immediately.

        Args:
            data: dict with keys matching Customer column names.
                  Produced by DataReader.read_and_filter()
        """
        with self._session() as session:
            existing = session.query(Customer).filter_by(
                order_id=data["order_id"]
            ).first()

            if existing:
                # Customer already exists — update their fields
                # (in case phone number was corrected in a new Excel export)
                for key, value in data.items():
                    if hasattr(existing, key) and key != "id":
                        setattr(existing, key, value)
                self._log.debug(f"Updated existing customer: {data['order_id']}")
            else:
                # New customer — insert into customers table
                customer = Customer(**{
                    k: v for k, v in data.items()
                    if hasattr(Customer, k) and k != "id"
                })
                session.add(customer)
                session.flush()  # Get customer.id assigned before creating send_log

                # Create the initial PENDING send_log entry
                log_entry = SendLog(
                    customer_id=customer.id,
                    order_id=customer.order_id,
                    status=SendStatus.PENDING,
                )
                session.add(log_entry)
                self._log.debug(f"Inserted new customer: {data['order_id']}")

            session.commit()

    # ── DEDUPLICATION ──────────────────────────────────────────

    def already_sent(self, order_id: str) -> bool:
        """
        Returns True if this order_id has any send_log entry with SENT status.
        This is the core deduplication check — called before every send.

        WHY THIS MATTERS:
        The scheduler checks this just before sending each message.
        If two sessions somehow both fetch the same customer (race condition),
        only the first one to mark SENT will actually deliver the message.
        The second will see already_sent()=True and skip it.

        Args:
            order_id: The customer's order ID to check.

        Returns:
            True  → this customer has already been messaged, skip them
            False → safe to proceed with sending
        """
        with self._session() as session:
            return session.query(SendLog).filter_by(
                order_id=order_id,
                status=SendStatus.SENT
            ).first() is not None

    # ── FETCH PENDING ──────────────────────────────────────────

    def get_pending(self, limit: int, order: str = "recent_first") -> list:
        """
        Fetch the next batch of customers ready to message.

        Excludes:
          - Customers already sent (status = SENT)
          - Customers with invalid/unresolvable phones (phone_valid = False)
          - Customers with no normalized phone

        Returns plain dicts, not ORM objects.
        WHY DICTS: ORM objects are tied to their session. Returning them
        and using them outside the session causes DetachedInstanceError.
        Dicts are safe to pass between functions and modules.

        Args:
            limit: Max number of customers to return (one session's batch).
            order: "recent_first" | "oldest_first" | "random"

        Returns:
            List of customer dicts with keys:
            order_id, first_name, normalized_phone, product_clean,
            order_date, attempt_count
        """
        with self._session() as session:
            query = (
                session.query(Customer, SendLog)
                .join(SendLog, Customer.id == SendLog.customer_id)
                .filter(SendLog.status == SendStatus.PENDING)
                .filter(Customer.phone_valid == True)
                .filter(Customer.normalized_phone.isnot(None))
            )

            # Apply ordering
            if order == "recent_first":
                # Most recent buyers first — they remember your brand best
                query = query.order_by(Customer.order_date.desc())
            elif order == "oldest_first":
                query = query.order_by(Customer.order_date.asc())
            # "random": no ORDER BY — we'll shuffle after fetching

            # Fetch extra records to allow proper shuffle if needed
            fetch_limit = limit if order != "random" else limit * 3
            rows = query.limit(fetch_limit).all()

            if order == "random":
                import random
                random.shuffle(rows)

            # Convert ORM objects to plain dicts
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

    # ── STATUS UPDATES ─────────────────────────────────────────

    def mark_sent(self, order_id: str, message_text: str,
                  template_used: str, screenshot_path: str = ""):
        """
        Update send_log to SENT after a confirmed delivery.

        Called by the Scheduler after sender.send_text() returns
        a result with status='SENT'.

        Args:
            order_id:        The customer's order ID.
            message_text:    The exact message that was delivered.
            template_used:   'A' or 'B' — which template was used.
            screenshot_path: Path to proof screenshot (optional).
        """
        with self._session() as session:
            # Find the active log entry (PENDING or RETRYING)
            log = (
                session.query(SendLog)
                .filter(SendLog.order_id == order_id)
                .filter(SendLog.status.in_([
                    SendStatus.PENDING,
                    SendStatus.RETRYING
                ]))
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
            else:
                self._log.warning(
                    f"mark_sent called for {order_id} but no PENDING/RETRYING log found."
                )

    def mark_failed(self, order_id: str, error_message: str):
        """
        Record a failed send attempt. Increments attempt_count.
        If attempt_count reaches 2: marks as FAILED_FINAL (no more retries).
        If attempt_count is still 1: marks as FAILED (retry tomorrow).

        Called by the Scheduler after sender.send_text() returns
        a result with status='FAILED'.

        Args:
            order_id:      The customer's order ID.
            error_message: What went wrong (saved for the daily report).
        """
        with self._session() as session:
            log = (
                session.query(SendLog)
                .filter(SendLog.order_id == order_id)
                .filter(SendLog.status.in_([
                    SendStatus.PENDING,
                    SendStatus.RETRYING
                ]))
                .first()
            )

            if log:
                log.attempt_count += 1
                log.error_message  = error_message
                log.updated_at     = datetime.now()

                # Determine next status based on attempt count
                if log.attempt_count >= 2:
                    log.status = SendStatus.FAILED_FINAL
                    self._log.warning(
                        f"FAILED_FINAL: {order_id} after {log.attempt_count} attempts"
                        f" — {error_message}"
                    )
                else:
                    log.status = SendStatus.FAILED
                    self._log.warning(
                        f"FAILED: {order_id} (attempt {log.attempt_count}/2)"
                        f" — {error_message}"
                    )

                session.commit()

    def reset_failed(self) -> int:
        """
        Reset retryable failures back to PENDING.

        FAILED → PENDING
        FAILED_FINAL → untouched
        SENT → untouched
        INVALID_NUMBER → untouched
        """

        with self._session() as session:
            rows = (
                session.query(SendLog)
                .filter(SendLog.status == SendStatus.FAILED)
                .all()
            )

            for row in rows:
                row.status = SendStatus.PENDING
                row.updated_at = datetime.now()

            count = len(rows)

            session.commit()

            self._log.info(
                f"Reset {count} FAILED entries back to PENDING."
            )

            return count

    def mark_invalid(self, order_id: str):
        """
        Mark a customer as INVALID_NUMBER — their phone is not on WhatsApp.
        Unlike FAILED, this status is never retried.

        Called by the Scheduler when the browser detects WhatsApp's
        'not on WhatsApp' or 'invalid phone number' modal.

        Args:
            order_id: The customer's order ID.
        """
        with self._session() as session:
            log = session.query(SendLog).filter_by(order_id=order_id).first()
            if log:
                log.status     = SendStatus.INVALID_NUMBER
                log.updated_at = datetime.now()
                session.commit()
                self._log.info(f"INVALID_NUMBER: {order_id}")

    # ── RETRY ──────────────────────────────────────────────────

    def get_retry_eligible(self) -> list:
        """
        Returns customers whose last attempt FAILED and have fewer than 2 attempts.
        Called at end-of-day to report how many can be retried tomorrow.

        To retry them: run --reset-failed command (calls reset_failed() below)
        """
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

    # def reset_failed(self) -> int:
    #     """
    #     Resets all FAILED (not FAILED_FINAL) send_log entries back to PENDING.
    #     Called by the --reset-failed CLI command so these customers
    #     are picked up in the next day's sending run.

    #     Returns:
    #         Number of records reset.
    #     """
    #     with self._session() as session:
    #         rows = session.query(SendLog).filter_by(
    #             status=SendStatus.SENT
    #         ).all()
    #         for row in rows:
    #             row.status = SendStatus.FAILED
    #         session.commit()
    #         count = len(rows)
    #         self._log.info(f"Reset {count} FAILED entries to PENDING.")
    #         return count

    # ── REPORTING ──────────────────────────────────────────────

    def get_daily_summary(self) -> dict:
        """
        Returns counts per status and detail lists for the daily report.
        Called by Reporter.generate_report() at end of day.

        Returns dict with:
            {
                "SENT": 45,
                "FAILED": 2,
                "INVALID_NUMBER": 3,
                ...
                "template_A": 23,   # Sent using Template A today
                "template_B": 22,   # Sent using Template B today
                "failed_details": [{"name": ..., "phone": ..., "error": ...}],
                "invalid_details": [{"name": ..., "phone": ...}],
            }
        """
        with self._session() as session:
            today_start = datetime.combine(date.today(), datetime.min.time())

            # Count by status (all time, not just today)
            status_counts = session.query(
                SendLog.status,
                func.count(SendLog.id)
            ).group_by(SendLog.status).all()

            summary = {s.value: 0 for s in SendStatus}
            for status, count in status_counts:
                summary[status] = count

            # Template A vs B for today's sent messages
            template_counts = session.query(
                SendLog.template_used,
                func.count(SendLog.id)
            ).filter(
                SendLog.status == SendStatus.SENT,
                SendLog.sent_at >= today_start
            ).group_by(SendLog.template_used).all()

            summary["template_A"] = 0
            summary["template_B"] = 0
            for template, count in template_counts:
                if template == "A":
                    summary["template_A"] = count
                elif template == "B":
                    summary["template_B"] = count

            # Detailed list of failed customers for the report
            failed_rows = (
                session.query(Customer, SendLog)
                .join(SendLog, Customer.id == SendLog.customer_id)
                .filter(SendLog.status == SendStatus.FAILED)
                .all()
            )
            summary["failed_details"] = [
                {
                    "name":  c.customer_name,
                    "phone": c.normalized_phone or c.raw_phone or "Unknown",
                    "error": l.error_message or "Unknown error",
                }
                for c, l in failed_rows
            ]

            # Detailed list of invalid numbers for the report
            invalid_rows = (
                session.query(Customer, SendLog)
                .join(SendLog, Customer.id == SendLog.customer_id)
                .filter(SendLog.status == SendStatus.INVALID_NUMBER)
                .all()
            )
            summary["invalid_details"] = [
                {
                    "name":  c.customer_name,
                    "phone": c.raw_phone or "Unknown",
                }
                for c, l in invalid_rows
            ]

            return summary

    def get_stats(self) -> dict:
        """
        Quick statistics for the --preview CLI command.
        Lighter than get_daily_summary() — no detail lists.

        Returns:
            {
                "total":          96,  # Total customers imported
                "pending":        50,  # Waiting to be sent
                "sent":           40,  # Successfully delivered
                "invalid":         3,  # Not on WhatsApp
                "invalid_phones":  3,  # Could not normalize phone number
            }
        """
        with self._session() as session:
            return {
                "total": session.query(Customer).count(),
                "pending": session.query(SendLog).filter_by(
                    status=SendStatus.PENDING
                ).count(),
                "sent": session.query(SendLog).filter_by(
                    status=SendStatus.SENT
                ).count(),
                "invalid": session.query(SendLog).filter_by(
                    status=SendStatus.INVALID_NUMBER
                ).count(),
                "invalid_phones": session.query(Customer).filter_by(
                    phone_valid=False
                ).count(),
            }

    def get_sample_customers(self, limit: int = 5) -> list:
        """
        Returns first N customer first names for --preview display.
        Phone numbers are intentionally NOT included in this output.
        """
        with self._session() as session:
            return [
                c.first_name
                for c in session.query(Customer).limit(limit).all()
            ]
            
        
    def get_all_statuses(self) -> dict:
            """
            Returns a dict of every customer's send status.
            Used by ExcelReporter to populate the Status/Comment column.

            Returns:
                {
                    "ORD001": {
                        "status": "SENT",
                        "error_message": ""
                    },
                    "ORD002": {
                        "status": "FAILED",
                        "error_message": "Timeout waiting for tick"
                    },
                    ...
                }
            """
            with self._session() as session:
                rows = (
                    session.query(Customer, SendLog)
                    .join(SendLog, Customer.id == SendLog.customer_id)
                    .all()
                )

                result = {}
                for customer, log in rows:
                    result[customer.order_id] = {
                        "status":        log.status,
                        "error_message": log.error_message or "",
                    }
                return result
            