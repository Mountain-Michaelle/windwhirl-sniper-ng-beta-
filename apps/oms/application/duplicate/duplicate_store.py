from __future__ import annotations

from datetime import datetime
from typing import Optional

from apps.oms.application.models.duplicate_group import DuplicateGroup
from apps.oms.application.models.duplicate_result import DuplicateResult
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class DuplicateStore:
    '''
    In-memory registry of orders and duplicate detection results.

    Stores:
        Processed order snapshots (for comparison)
        All DuplicateResult pairwise comparisons
        All DuplicateGroup clusters

    Day 10 will provide a SQLite-backed implementation
    via IOrderRepository — same interface, persistent storage.

    Usage:
        store = DuplicateStore(window_hours=48)
        store.register_order(parsed_order)
        candidates = store.get_candidates(new_order)
        store.store_result(duplicate_result)
        store.store_group(group)
    '''

    def __init__(self, window_hours: float = 48.0):
        '''
        Args:
            window_hours: Only orders within this time window are
                          considered as potential duplicates.
                          Default: 48 hours.
        '''
        self._window_hours = window_hours
        self._orders:  list           = []   # List of ParsedOrder snapshots
        self._results: list[DuplicateResult] = []
        self._groups:  list[DuplicateGroup]  = []

    def register_order(self, order) -> None:
        '''
        Register a new order for future comparison.
        Called after an order passes validation.

        Args:
            order: ParsedOrder to register.
        '''
        self._orders.append(order)
        log.debug(
            f"DuplicateStore: registered order {order.order_id!r} "
            f"(total: {len(self._orders)})"
        )

    def get_candidates(self, new_order) -> list:
        '''
        Return existing orders within the time window.
        These are the orders to compare the new order against.

        Excludes the new order itself (by order_id).

        Args:
            new_order: ParsedOrder being checked.

        Returns:
            List of ParsedOrder objects within the time window.
        '''
        now            = datetime.now()
        cutoff         = now.timestamp() - (self._window_hours * 3600)
        candidates     = []

        for order in self._orders:
            if order.order_id == new_order.order_id:
                continue

            parsed_at  = getattr(order, 'parsed_at', now)
            order_time = parsed_at.timestamp() if hasattr(parsed_at, 'timestamp') else cutoff

            if order_time >= cutoff:
                candidates.append(order)

        log.debug(
            f"DuplicateStore: {len(candidates)} candidate(s) in window "
            f"({self._window_hours}h) for order {new_order.order_id!r}"
        )
        return candidates

    def get_returning_customers(self, new_order) -> list:
        '''
        Return orders OUTSIDE the time window with matching phone.
        These are classified as RETURNING_CUSTOMER, never duplicate.

        Args:
            new_order: ParsedOrder being checked.

        Returns:
            List of orders outside window with same phone.
        '''
        now        = datetime.now()
        cutoff     = now.timestamp() - (self._window_hours * 3600)
        returning  = []

        for order in self._orders:
            if order.order_id == new_order.order_id:
                continue

            parsed_at  = getattr(order, 'parsed_at', now)
            order_time = parsed_at.timestamp() if hasattr(parsed_at, 'timestamp') else cutoff

            if order_time < cutoff:
                returning.append(order)

        return returning

    def store_result(self, result: DuplicateResult) -> None:
        '''Store a pairwise comparison result.'''
        self._results.append(result)
        log.debug(f"DuplicateStore: stored result {result.summary()}")

    def store_group(self, group: DuplicateGroup) -> None:
        '''Store or update a duplicate group.'''
        existing = self.get_group_for_order(group.canonical_order_id)
        if existing:
            # Update existing group with new members
            for oid in group.member_order_ids:
                existing.add_member(oid)
        else:
            self._groups.append(group)
            log.info(
                f"DuplicateStore: new group {group.group_id!r} "
                f"({group.size} members)"
            )

    def get_group_for_order(self, order_id: str) -> Optional[DuplicateGroup]:
        '''Return the DuplicateGroup containing this order, or None.'''
        for group in self._groups:
            if group.has_member(order_id):
                return group
        return None

    def results_for_order(self, order_id: str) -> list[DuplicateResult]:
        '''All pairwise results involving a given order.'''
        return [
            r for r in self._results
            if r.order_id_a == order_id or r.order_id_b == order_id
        ]

    def stats(self) -> dict:
        return {
            "total_orders":   len(self._orders),
            "total_results":  len(self._results),
            "total_groups":   len(self._groups),
            "window_hours":   self._window_hours,
            "duplicates":     sum(1 for r in self._results if r.is_duplicate),
        }
