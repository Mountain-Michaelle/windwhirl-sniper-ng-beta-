from __future__ import annotations

import json
from datetime import datetime, date
from typing import Optional

from sqlalchemy.orm import Session

from apps.oms.infrastructure.persistence.schema import OrderRecord
from apps.oms.shared.logger import get_logger
from apps.oms.sync.google_provider import (
    normalize_sniper_action, parse_scheduled_datetime, SCHEDULED_STATUS,
)

log = get_logger(__name__)


class OrderRepository:
    '''
    Persists and retrieves OrderRecord rows.

    Usage:
        repo = OrderRepository(session_factory)
        await repo.save_validated_order(validated_order)
        orders = await repo.get_by_worker("2348XXXXXXXXX")
        orders = await repo.get_today()
        order  = await repo.get_by_id("ORD-001")
    '''

    def __init__(self, session_factory):
        self._sf  = session_factory
        self._log = log

    # ------------------------------------------------------------
    # Sheet → DB safe sync-back (Day 10.6)
    # ------------------------------------------------------------

    async def get_by_row_key(self, row_key: str):
        '''The Day 10.6 lookup — never resolve by the visible sheet id.'''
        with self._sf() as session:
            return (
                session.query(OrderRecord)
                       .filter(OrderRecord.row_key == row_key, OrderRecord.is_archived == False)
                       .one_or_none()
            )

    async def mark_archived(self, order_id: str) -> None:
        '''Soft delete only. Real hard-delete is a separate, deliberate
        admin-only operation and is never reachable from the sheet.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return
            record.is_archived = True
            record.archived_at = datetime.now()
            session.commit()
            self._log.info(f"OrderRepository: order {order_id!r} archived (soft delete).")

    async def list_active_row_keys(self) -> list[str]:
        with self._sf() as session:
            rows = (
                session.query(OrderRecord.row_key)
                       .filter(OrderRecord.row_key.isnot(None), OrderRecord.is_archived == False)
                       .all()
            )
            return [r[0] for r in rows]

    async def apply_sheet_edits(self, order_id: str, sheet_row: dict) -> list[str]:
        '''
        Maps editable sheet columns onto the DB record. ONLY the columns
        in WORKER_EDITABLE_COLUMNS are ever applied — reference fields
        (delivery_address, assigned_worker, etc.) are read-only in the
        sheet now (protected ranges) and are never taken from sheet_row
        even if present, since a worker should never be able to change
        them from here.

        Returns a list of field names that were IGNORED because their
        sheet value didn't pass validation — the caller uses this to
        add a note to the row's status. Currently:
          - "sniper_action"    → value not in ALLOWED_SNIPER_ACTIONS
          - "scheduled_date"   → sniper_action is "Scheduled" but no
                                  valid date+time was given (business
                                  rule: a Scheduled order must always
                                  carry a real schedule, every time the
                                  status is set — not just once)
        '''
        # Only genuinely worker-editable free-text fields. Everything
        # else (delivery_address, assigned_worker, etc.) is reference
        # data now and is intentionally NOT read from sheet_row here.
        PLAIN_EDITABLE_FIELDS = {
            "customer_name":   "customer_name",
            "phone_number":    "phone_number",
            "whatsapp_number": "whatsapp_number",
            "comments":        "comments",   # free text — nullable, no validation
        }

        ignored_fields: list[str] = []

        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return ignored_fields

            for sheet_field, db_field in PLAIN_EDITABLE_FIELDS.items():
                if sheet_field in sheet_row:
                    setattr(record, db_field, sheet_row[sheet_field] or None)

            # sniper_action: must match ALLOWED_SNIPER_ACTIONS (case-insensitive)
            # or be blank. Anything else is silently ignored — the sheet cell
            # is left as the worker typed it, but the DB is never polluted.
            new_sniper_action = record.sniper_action
            if "sniper_action" in sheet_row:
                raw = sheet_row["sniper_action"]
                if not str(raw or "").strip():
                    new_sniper_action = None   # explicitly cleared — valid
                else:
                    canonical = normalize_sniper_action(raw)
                    if canonical is None:
                        ignored_fields.append("sniper_action")
                    else:
                        new_sniper_action = canonical

            # scheduled_date / scheduled_time: only meaningful together,
            # and only enforced when the (possibly just-set) status is
            # "Scheduled". This check runs every single time — if the
            # worker changes the status back to "Scheduled" again later
            # with a stale/blank date, it's caught again, not just once.
            if new_sniper_action == SCHEDULED_STATUS:
                scheduled_at = parse_scheduled_datetime(
                    sheet_row.get("scheduled_date"), sheet_row.get("scheduled_time"),
                )
                if scheduled_at is None:
                    # Reject the status change itself — a "Scheduled" order
                    # with no real date/time is not accepted. Keep whatever
                    # sniper_action the record already had.
                    ignored_fields.append("scheduled_date")
                else:
                    record.sniper_action = new_sniper_action
                    record.scheduled_at  = scheduled_at
            else:
                record.sniper_action = new_sniper_action
                record.scheduled_at  = None   # any non-Scheduled status clears it,
                                               # even if stale date/time cells
                                               # linger from a bypassed paste edit

            record.updated_at = datetime.now()
            session.commit()

        return ignored_fields

    async def ensure_row_key(self, order_id: str, generate_fn) -> str:
        '''Assigns a row_key on first sync-out if the record doesn't have one yet.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return ""
            if not record.row_key:
                record.row_key = generate_fn()
                session.commit()
            return record.row_key

    # ------------------------------------------------------------
    # DB → Sheet outbound sync bookkeeping (Day 10.5)
    # Referenced by SynchronizationWorker / SyncService — were missing
    # from this file entirely, which is why outbound sync couldn't work.
    # ------------------------------------------------------------

    async def update_google_row_id(self, order_id: str, google_row_id: int) -> None:
        '''Save the Google Sheets row number after a successful append.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return
            record.google_row_id   = google_row_id
            record.sync_status     = "SYNCED"
            record.last_sync_time  = datetime.now()
            record.last_sync_error = None
            session.commit()
            self._log.debug(
                f"OrderRepository: order {order_id!r} → google_row_id={google_row_id}"
            )

    async def update_sync_status(self, order_id: str, status: str, error: str = "") -> None:
        '''Update sync_status and optionally the last_sync_error.
        Called for SYNCED / FAILED / RETRYING / HALTED transitions.'''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return
            record.sync_status     = status
            record.last_sync_time  = datetime.now()
            record.last_sync_error = error or None
            session.commit()

    async def get_unsynced(self, limit: int = 100) -> list[OrderRecord]:
        '''
        Return orders where sync_status is PENDING or RETRYING.
        Used on startup to re-queue outbound sync jobs that didn't
        finish before the last restart. Archived orders are excluded —
        no point re-queueing a soft-deleted order for outbound sync.
        '''
        with self._sf() as session:
            return (
                session.query(OrderRecord)
                       .filter(
                           OrderRecord.sync_status.in_(["PENDING", "RETRYING"]),
                           OrderRecord.is_archived == False,
                       )
                       .order_by(OrderRecord.created_at.asc())
                       .limit(limit)
                       .all()
            )

    async def emit_persisted(self, order_id: str) -> None:
        '''
        Emit the "order.persisted" event after saving. Called at the
        end of save_validated_order() so SyncService picks up newly
        created orders automatically and queues them for outbound sync.
        '''
        from apps.oms.events import dispatcher
        await dispatcher.emit("order.persisted", order_id=order_id)

    # ------------------------------------------------------------
    # Core order persistence
    # ------------------------------------------------------------

    async def save_validated_order(self, validated_order) -> str:
        '''
        Persist or update an order from a ValidatedOrder.
        Upserts by order_id — safe to call multiple times.

        Args:
            validated_order: ValidatedOrder from Day 8.

        Returns:
            The order_id that was saved.
        '''
        parsed = validated_order.parsed_order
        report = validated_order.report

        with self._sf() as session:
            existing = session.get(OrderRecord, parsed.order_id)

            if existing:
                record = existing
            else:
                record = OrderRecord(order_id=parsed.order_id)
                session.add(record)

            # Populate from ParsedOrder
            record.parsed_id        = parsed.parsed_id
            record.customer_name    = parsed.customer_name
            record.phone_number     = parsed.phone_number
            record.whatsapp_number  = parsed.whatsapp_number
            record.delivery_address = parsed.delivery_address
            record.delivery_request = parsed.delivery_request
            record.order_date_raw   = parsed.order_date_raw
            record.campaign         = parsed.campaign
            record.customer_question= parsed.customer_question
            record.raw_text         = parsed.raw_text
            record.validated_at     = parsed.parsed_at

            # Package fields
            if parsed.package:
                record.package_name = parsed.package.name
                record.package_desc = parsed.package.description
                record.price_raw    = parsed.package.price_raw
                record.price_value  = parsed.package.price_value

            # Validation summary
            record.is_valid         = report.is_valid
            record.quality_score    = report.quality_score
            record.validation_flags = ",".join(report.flag_values())
            record.validation_errors = json.dumps(report.error_codes())
            record.missing_fields   = ",".join(
                getattr(parsed, 'missing_fields', [])
            )

            session.commit()
            self._log.info(
                f"OrderRepository: saved order {parsed.order_id!r} "
                f"(valid={report.is_valid}, quality={report.quality_score:.0%})"
            )

        # Outside the session — notify the sync layer a new/updated
        # order is ready to be pushed out to Google Sheets.
        await self.emit_persisted(parsed.order_id)

        return parsed.order_id

    async def update_assignment(
        self,
        order_id:      str,
        worker_number: str,
        assigned_at:   datetime = None,
    ) -> None:
        '''
        Update an order's assigned worker and status.
        Called when "assignment.resolved" event fires.
        '''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                self._log.warning(
                    f"OrderRepository: cannot update assignment — "
                    f"order {order_id!r} not found"
                )
                return

            record.worker_number    = worker_number
            record.assignment_status = "ASSIGNED"
            record.assigned_at      = assigned_at or datetime.now()
            session.commit()

            self._log.info(
                f"OrderRepository: order {order_id!r} assigned to "
                f"+{worker_number}"
            )

    async def update_duplicate_status(
        self,
        order_id:    str,
        status:      str,
        group_id:    str = "",
    ) -> None:
        '''
        Update duplicate detection status on an order.
        Called when "duplicate.confirmed" or "duplicate.likely" fires.
        '''
        with self._sf() as session:
            record = session.get(OrderRecord, order_id)
            if not record:
                return

            record.duplicate_status   = status
            record.duplicate_group_id = group_id or None
            session.commit()

    async def get_by_id(self, order_id: str) -> Optional[OrderRecord]:
        '''Retrieve one order by ID.'''
        with self._sf() as session:
            return session.get(OrderRecord, order_id)

    async def get_by_worker(
        self,
        worker_number: str,
        status: str = None,
        limit:  int = 200,
    ) -> list[OrderRecord]:
        '''
        Retrieve orders assigned to a worker.
        Optionally filtered by assignment_status.
        '''
        with self._sf() as session:
            query = session.query(OrderRecord).filter(
                OrderRecord.worker_number == worker_number
            )
            if status:
                query = query.filter(OrderRecord.assignment_status == status)

            return (
                query.order_by(OrderRecord.created_at.desc())
                     .limit(limit)
                     .all()
            )

    async def get_today(self, worker_number: str = None) -> list[OrderRecord]:
        '''
        Retrieve all orders created today.
        Optionally filtered by worker.
        '''
        today_start = datetime.combine(date.today(), datetime.min.time())

        with self._sf() as session:
            query = session.query(OrderRecord).filter(
                OrderRecord.created_at >= today_start
            )
            if worker_number:
                query = query.filter(
                    OrderRecord.worker_number == worker_number
                )
            return (
                query.order_by(OrderRecord.created_at.asc())
                     .all()
            )

    async def get_pending(self, worker_number: str = None) -> list[OrderRecord]:
        '''Retrieve orders with PENDING assignment status.'''
        with self._sf() as session:
            query = session.query(OrderRecord).filter(
                OrderRecord.assignment_status == "PENDING"
            )
            if worker_number:
                query = query.filter(
                    OrderRecord.worker_number == worker_number
                )
            return (
                query.order_by(OrderRecord.created_at.asc())
                     .all()
            )

    async def get_in_window(
        self,
        since:         datetime,
        exclude_id:    str = "",
    ) -> list[OrderRecord]:
        '''
        Retrieve orders created after a given timestamp.
        Used by DbDuplicateStore for candidate queries.
        Excludes a given order_id (the new order being checked).
        '''
        with self._sf() as session:
            query = session.query(OrderRecord).filter(
                OrderRecord.created_at >= since
            )
            if exclude_id:
                query = query.filter(
                    OrderRecord.order_id != exclude_id
                )
            return (
                query.order_by(OrderRecord.created_at.asc())
                     .all()
            )

    async def count_by_status(self) -> dict:
        '''Count orders grouped by assignment_status.'''
        with self._sf() as session:
            from sqlalchemy import func as sqlfunc
            rows = (
                session.query(
                    OrderRecord.assignment_status,
                    sqlfunc.count(OrderRecord.order_id)
                )
                .group_by(OrderRecord.assignment_status)
                .all()
            )
            return {status: count for status, count in rows}

    async def get_by_phone(
        self,
        phone: str,
        since: datetime = None,
    ) -> list[OrderRecord]:
        '''
        Retrieve orders by phone number.
        Used for returning customer detection.
        '''
        with self._sf() as session:
            query = session.query(OrderRecord).filter(
                (OrderRecord.phone_number == phone) |
                (OrderRecord.whatsapp_number == phone)
            )
            if since:
                query = query.filter(OrderRecord.created_at >= since)
            return query.order_by(OrderRecord.created_at.desc()).all()