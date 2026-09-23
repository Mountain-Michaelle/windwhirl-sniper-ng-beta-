from __future__ import annotations

from datetime import datetime
from typing import Optional

from apps.oms.application.duplicate.phone_matcher import PhoneMatcher
from apps.oms.application.duplicate.name_matcher import NameMatcher
from apps.oms.application.duplicate.address_matcher import AddressMatcher
from apps.oms.application.duplicate.fingerprint_matcher import FingerprintMatcher
from apps.oms.application.duplicate.duplicate_store import DuplicateStore
from apps.oms.application.models.duplicate_result import (
    DuplicateResult, DuplicateClassification, DimensionScore
)
from apps.oms.application.models.duplicate_group import DuplicateGroup
from apps.oms.application.models.validation_report import ValidationFlag
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


# ── Score thresholds ─────────────────────────────────────────────
THRESHOLD_CONFIRMED = 0.85
THRESHOLD_LIKELY    = 0.60
THRESHOLD_POSSIBLE  = 0.35


class DuplicateDetectionEngine:
    '''
    Detects duplicate orders by comparing new orders against existing ones.

    For each new ValidatedOrder:
        1. Register the new order in the store
        2. Get all candidates within the time window
        3. Compare against each candidate using all four matchers
        4. Classify each comparison
        5. Group confirmed/likely duplicates
        6. Set ValidationFlag.DUPLICATE_PENDING on duplicates
        7. Emit events
        8. Return all DuplicateResult for this order

    Usage:
        engine = DuplicateDetectionEngine(window_hours=48)
        results = await engine.check(validated_order)
    '''

    def __init__(self, window_hours: float = 48.0):
        '''
        Args:
            window_hours: Time window for duplicate detection.
                          Orders older than this are RETURNING_CUSTOMER.
        '''
        self._store   = DuplicateStore(window_hours=window_hours)
        self._phone   = PhoneMatcher()
        self._name    = NameMatcher()
        self._address = AddressMatcher()
        self._finger  = FingerprintMatcher()
        self._window  = window_hours

    async def check(self, validated_order) -> list[DuplicateResult]:
        '''
        Check a ValidatedOrder for duplicates against existing orders.

        Args:
            validated_order: ValidatedOrder from Day 8.

        Returns:
            List of DuplicateResult — one per candidate compared.
            Empty if no candidates in window or no duplicates found.
        '''
        parsed = validated_order.parsed_order
        log.info(
            f"DuplicateDetection: checking order {parsed.order_id!r} "
            f"(window={self._window}h)"
        )

        # Register this order for future comparisons
        self._store.register_order(parsed)

        # Get candidates to compare against
        candidates = self._store.get_candidates(parsed)

        if not candidates:
            log.debug(
                f"DuplicateDetection: no candidates in window — "
                f"order {parsed.order_id!r} is UNIQUE"
            )
            await dispatcher.emit(
                "duplicate.check.unique",
                order_id   =parsed.order_id,
                candidates =0,
            )
            return []

        results = []

        for candidate in candidates:
            result = await self._compare(parsed, candidate)
            results.append(result)
            self._store.store_result(result)

            if result.is_duplicate:
                await self._handle_duplicate(validated_order, result)

        # Check outside window — returning customer detection
        await self._check_returning_customers(parsed)

        # Summary log
        duplicates = [r for r in results if r.is_duplicate]
        if duplicates:
            log.warning(
                f"DuplicateDetection: {len(duplicates)} duplicate(s) found "
                f"for order {parsed.order_id!r}"
            )
        else:
            log.info(
                f"DuplicateDetection: order {parsed.order_id!r} is UNIQUE "
                f"({len(candidates)} candidate(s) checked)"
            )

        await dispatcher.emit(
            "duplicate.check.complete",
            order_id          =parsed.order_id,
            candidates_checked=len(candidates),
            duplicates_found  =len(duplicates),
            results           =[r.summary() for r in results],
        )

        return results

    async def _compare(self, order_a, order_b) -> DuplicateResult:
        '''
        Compare two ParsedOrders across all four dimensions.
        Returns a single DuplicateResult with weighted final score
        and classification.
        '''
        parsed_at_a = getattr(order_a, 'parsed_at', datetime.now())
        parsed_at_b = getattr(order_b, 'parsed_at', datetime.now())

        hours_apart   = abs(
            (parsed_at_a - parsed_at_b).total_seconds()
        ) / 3600
        within_window = hours_apart <= self._window

        # ── Fingerprint check first (fast exit) ──────────────────
        fp_score = self._finger.compare(order_a, order_b)
        if fp_score.matched:
            # Exact same message — immediately CONFIRMED
            return DuplicateResult(
                order_id_a    =order_a.order_id,
                order_id_b    =order_b.order_id,
                classification=DuplicateClassification.CONFIRMED_DUPLICATE,
                final_score   =1.0,
                dimensions    =[fp_score],
                hours_apart   =hours_apart,
                within_window =within_window,
            )

        # ── Run all matchers ──────────────────────────────────────
        phone_score   = self._phone.compare(order_a, order_b)
        name_score    = self._name.compare(order_a, order_b)
        address_score = self._address.compare(order_a, order_b)

        dimensions = [fp_score, phone_score, name_score, address_score]

        # ── Compute weighted final score ──────────────────────────
        # Weights: phone=0.60, name=0.25, address=0.15
        # (fp_score excluded from weighted sum — handled separately)
        final_score = (
            phone_score.score   * phone_score.weight
            + name_score.score  * name_score.weight
            + address_score.score * address_score.weight
        )
        final_score = round(final_score, 4)

        # ── Classify ──────────────────────────────────────────────
        if not within_window:
            classification = DuplicateClassification.UNIQUE
        elif final_score >= THRESHOLD_CONFIRMED:
            classification = DuplicateClassification.CONFIRMED_DUPLICATE
        elif final_score >= THRESHOLD_LIKELY:
            classification = DuplicateClassification.LIKELY_DUPLICATE
        elif final_score >= THRESHOLD_POSSIBLE:
            classification = DuplicateClassification.POSSIBLE_DUPLICATE
        else:
            classification = DuplicateClassification.UNIQUE

        log.debug(
            f"  Compared {order_a.order_id!r} vs {order_b.order_id!r}: "
            f"score={final_score:.2f} → {classification.value} | "
            f"phone={phone_score.score:.2f} "
            f"name={name_score.score:.2f} "
            f"addr={address_score.score:.2f}"
        )

        return DuplicateResult(
            order_id_a    =order_a.order_id,
            order_id_b    =order_b.order_id,
            classification=classification,
            final_score   =final_score,
            dimensions    =dimensions,
            hours_apart   =hours_apart,
            within_window =within_window,
        )

    async def _handle_duplicate(
        self,
        validated_order,
        result: DuplicateResult,
    ) -> None:
        '''
        Handle a confirmed or likely duplicate.
        Creates or updates DuplicateGroup.
        Sets ValidationFlag.DUPLICATE_PENDING on the validated order.
        Emits events.
        '''
        parsed_order = validated_order.parsed_order
        order_id_a   = result.order_id_a
        order_id_b   = result.order_id_b

        # Find or create the duplicate group
        existing_group = (
            self._store.get_group_for_order(order_id_b)
            or self._store.get_group_for_order(order_id_a)
        )

        if existing_group:
            existing_group.add_member(order_id_a)
            existing_group.add_member(order_id_b)
            result.group_id = existing_group.group_id
            self._store.store_group(existing_group)
        else:
            # canonical = older order (order_id_b is already in store)
            group = DuplicateGroup(
                canonical_order_id=order_id_b,
                classification    =result.classification.value,
            )
            group.add_member(order_id_a)
            result.group_id = group.group_id
            self._store.store_group(group)

        # Set DUPLICATE_PENDING flag on the validation report
        if hasattr(validated_order, 'report') and validated_order.report:
            validated_order.report.add_flag(ValidationFlag.DUPLICATE_PENDING)

        # Emit event
        event_name = (
            "duplicate.confirmed"
            if result.classification == DuplicateClassification.CONFIRMED_DUPLICATE
            else "duplicate.likely"
        )

        await dispatcher.emit(
            event_name,
            order_id_a    =order_id_a,
            order_id_b    =order_id_b,
            classification=result.classification.value,
            final_score   =result.final_score,
            matched_on    =result.matched_dimensions(),
            hours_apart   =result.hours_apart,
            group_id      =result.group_id,
        )

        log.warning(
            f"{'🔴' if result.classification == DuplicateClassification.CONFIRMED_DUPLICATE else '🟡'} "
            f"Duplicate {result.classification.value}: "
            f"{order_id_a!r} matches {order_id_b!r} "
            f"(score={result.final_score:.2f}, "
            f"matched={result.matched_dimensions()})"
        )

    async def _check_returning_customers(self, parsed_order) -> None:
        '''
        Check orders outside the time window for phone matches.
        These are RETURNING_CUSTOMER — not duplicates.
        Emits an informational event.
        '''
        old_orders = self._store.get_returning_customers(parsed_order)
        if not old_orders:
            return

        for old_order in old_orders:
            phone_score = self._phone.compare(parsed_order, old_order)
            if phone_score.matched:
                log.info(
                    f"Returning customer detected: "
                    f"order {parsed_order.order_id!r} "
                    f"matches +{phone_score.detail} "
                    f"from order {old_order.order_id!r} "
                    f"(outside {self._window}h window)"
                )
                await dispatcher.emit(
                    "duplicate.returning_customer",
                    new_order_id =parsed_order.order_id,
                    old_order_id =old_order.order_id,
                    customer     =parsed_order.customer_name,
                )

    def stats(self) -> dict:
        return self._store.stats()
