from typing import Optional

from apps.oms.domain.entities import Order, OrderStatus, RawMessage
from apps.oms.domain.interfaces import (
    IAssignmentEngine,
    IDuplicateDetector,
    IMessageSource,
    IParser,
    ISheetSynchronizer,
    IValidator,
)
from apps.oms.domain.exceptions import OrderParseException
from apps.oms.events import dispatcher
from apps.oms.repositories.interfaces import IOrderRepository
from apps.oms.shared.logger import get_logger

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