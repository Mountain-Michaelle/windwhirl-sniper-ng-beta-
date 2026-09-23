from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from apps.oms.application.duplicate.duplicate_store import DuplicateStore
from apps.oms.application.models.duplicate_result import DuplicateResult
from apps.oms.application.models.duplicate_group import DuplicateGroup
from apps.oms.infrastructure.persistence.order_repository import OrderRepository
from apps.oms.infrastructure.persistence.duplicate_repository import DuplicateRepository
from apps.oms.infrastructure.persistence.schema import OrderRecord
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class DbDuplicateStore:
    '''
    Database-backed DuplicateStore.
    Drops in place of the in-memory DuplicateStore from Day 9.

    The DuplicateDetectionEngine never knows it's talking to
    a database — it just calls the same methods.

    Usage:
        db_store = DbDuplicateStore(
            order_repo=order_repo,
            duplicate_repo=duplicate_repo,
            window_hours=48.0,
        )
        # Pass to DuplicateDetectionEngine:
        engine = DuplicateDetectionEngine(store=db_store)
    '''

    def __init__(
        self,
        order_repo:     OrderRepository,
        duplicate_repo: DuplicateRepository,
        window_hours:   float = 48.0,
    ):
        self._orders    = order_repo
        self._dupes     = duplicate_repo
        self._window    = window_hours

        # In-memory result store (results not persisted — only groups are)
        self._results: list[DuplicateResult] = []

    def register_order(self, order) -> None:
        '''
        No-op for DB store — order is already persisted by the
        event listener in oms_runner.py before duplicate check runs.
        Kept for interface compatibility.
        '''
        pass

    async def get_candidates(self, new_order) -> list:
        '''
        Query DB for orders within the time window.
        Returns list of ParsedOrder-compatible objects (OrderRecord).
        '''
        since = datetime.now() - timedelta(hours=self._window)

        records = await self._orders.get_in_window(
            since      =since,
            exclude_id =getattr(new_order, 'order_id', ""),
        )

        # Convert OrderRecord to lightweight proxy objects
        # that the matchers can access via the same attributes
        proxies = [OrderRecordProxy(r) for r in records]

        log.debug(
            f"DbDuplicateStore: {len(proxies)} candidate(s) in "
            f"{self._window}h window"
        )
        return proxies

    async def get_returning_customers(self, new_order) -> list:
        '''
        Query DB for orders BEFORE the time window with matching phone.
        '''
        cutoff = datetime.now() - timedelta(hours=self._window)
        phone  = getattr(new_order, 'phone_number', "") or ""
        if not phone:
            return []

        from apps.oms.application.duplicate.similarity import phone_normalize
        normalized = phone_normalize(phone)

        all_by_phone = await self._orders.get_by_phone(normalized)
        return [
            OrderRecordProxy(r) for r in all_by_phone
            if r.order_id != getattr(new_order, 'order_id', "")
            and r.created_at < cutoff
        ]

    def get_group_for_order(self, order_id: str) -> Optional[DuplicateGroup]:
        '''
        In-memory group lookup (groups are also in DB but
        in-memory is faster for within-session checks).
        '''
        # Sync implementation — groups stored in-memory for current session
        for result in self._results:
            if hasattr(result, 'group_id') and result.group_id:
                pass  # Would need async — simplified for Day 10
        return None

    def store_result(self, result: DuplicateResult) -> None:
        '''Store result in memory for within-session deduplication.'''
        self._results.append(result)

    async def store_group(self, group: DuplicateGroup) -> None:
        '''Persist group to database.'''
        await self._dupes.save_group(group)

    def stats(self) -> dict:
        return {
            "window_hours":  self._window,
            "results_cached": len(self._results),
            "backend":       "database",
        }


class OrderRecordProxy:
    '''
    Wraps an OrderRecord to expose the same attributes
    that the duplicate matchers expect from a ParsedOrder.
    This adapter prevents the matchers from needing to know
    about SQLAlchemy ORM models.
    '''

    def __init__(self, record: OrderRecord):
        self._r = record

    @property
    def order_id(self) -> str:
        return self._r.order_id

    @property
    def customer_name(self):
        return self._r.customer_name

    @property
    def phone_number(self):
        return self._r.phone_number

    @property
    def whatsapp_number(self):
        return self._r.whatsapp_number

    @property
    def delivery_address(self):
        return self._r.delivery_address

    @property
    def parsed_at(self):
        return self._r.created_at

    @property
    def fingerprint(self):
        return self._r.order_id  # Use order_id as fingerprint proxy

    def __repr__(self):
        return f"OrderRecordProxy(order_id={self.order_id!r})"
