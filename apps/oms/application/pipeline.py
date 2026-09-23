from typing import Optional

from apps.oms.application.classifier import MessageClassifier, MessageClass
from apps.oms.application.parser import OrderParser
from apps.oms.application.validator import OrderValidator
from apps.oms.application.assignment_engine import SingleStaffAssignmentEngine
from apps.oms.domain.entities import Order, Staff
from apps.oms.infrastructure.browser.raw_message import RawMessage
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger
from apps.oms.application.assignment_state_engine import AssignmentStateEngine

log = get_logger(__name__)


class MessagePipeline:
    '''
    Processes a RawMessage through all four pipeline stages.
    Called for every "message.received" event from the DOM observer.

    Responsibilities:
        - Coordinate stages 1-4
        - Emit events at each stage
        - Log outcomes at every decision point
        - Never raise — all errors are caught and logged

    Usage:
        pipeline = MessagePipeline(
            classifier=MessageClassifier(),
            parser=OrderParser(),
            validator=OrderValidator(),
            assignment_engine=SingleStaffAssignmentEngine(staff),
            staff=staff,
        )

        @dispatcher.on("message.received")
        async def handle_message(message: RawMessage, **kwargs):
            await pipeline.process(message)
    '''

    def __init__( 
        self,
        classifier:        MessageClassifier,
        parser:            OrderParser,
        validator:         OrderValidator,
        assignment_engine: SingleStaffAssignmentEngine,
        staff:             Staff,
        state_engine:       AssignmentStateEngine, 
    ):

        self._state_engine = state_engine        
        self._classifier = classifier
        self._parser     = parser
        self._validator  = validator
        self._assigner   = assignment_engine
        self._staff      = staff
        self._stats = {
            "total":       0,
            "classified":  0,
            "parsed":      0,
            "validated":   0,
            "assigned":    0,
            "skipped":     0,
            "errors":      0,
        }

    async def process(self, message: RawMessage) -> Optional[Order]:
        '''
        Process one message through the full pipeline.

        Returns the final Order if all stages succeed.
        Returns None if the message is not an order or any stage fails.
        Never raises — all exceptions are caught and logged.

        Args:
            message: The RawMessage from the DOM observer.

        Returns:
            Completed Order entity, or None.
        '''
        self._stats["total"] += 1

        try:
            return await self._run(message)
        except Exception as e:
            self._stats["errors"] += 1
            log.error(
                f"Pipeline error for message {message.fingerprint[:8]!r}: {e}",
                exc_info=True
            )
            return None

    async def _run(self, message: RawMessage) -> Optional[Order]:
        '''Internal pipeline runner. May raise — caller catches.'''

        # ── Stage 1: Classify ────────────────────────────────────
        result = self._classifier.classify(message)

        self._stats["classified"] += 1
        await dispatcher.emit(
            "message.classified",
            message   =message,
            cls       =result.message_class.value,
            confidence=result.confidence,
            reasoning =result.reasoning,
        )

        if result.message_class == MessageClass.SYSTEM:
            log.debug(f"Skipping system message: {message.preview(40)!r}")
            self._stats["skipped"] += 1
            return None

        if result.message_class == MessageClass.UNKNOWN:
            log.debug(
                f"Unknown message type (confidence={result.confidence:.2f}): "
                f"{message.preview(40)!r}"
            )
            self._stats["skipped"] += 1
            return None

        if result.message_class != MessageClass.ORDER:
            # ASSIGNMENT and STATUS will be handled in future milestones
            log.debug(
                f"Message classified as {result.message_class.value} "
                f"— not yet handled (future milestone)"
            )
            self._stats["skipped"] += 1
            return None
        
        if result.message_class == MessageClass.ASSIGNMENT:
            state = await self._state_engine.observe_assignment(
                raw_text=message.raw_text,
                message =message,
            )
            log.info(
                f"Assignment observed. State v{state.state_version}: "
                f"pending={state.pending_count}, "
                f"worker=+{state.current_worker or 'none'}"
            )
            return None    

        if not result.is_confident:
            log.info(
                f"Low confidence ORDER classification "
                f"({result.confidence:.2f}) — skipping: "
                f"{message.preview(40)!r}"
            )
            self._stats["skipped"] += 1
            return None

        log.info(
            f"ORDER detected (confidence={result.confidence:.2f}): "
            f"{message.preview(50)!r}"
        )

        # ── Stage 2: Parse ───────────────────────────────────────
        order = self._parser.parse(message, self._staff.number)

        if order is None:
            log.info(
                f"Parser returned None — message looks like order "
                f"but could not extract fields: {message.preview(50)!r}"
            )
            await dispatcher.emit(
                "order.parse_failed",
                message=message,
                reason ="Parser returned None"
            )
            self._stats["skipped"] += 1
            return None

        # Attach the source message reference
        order.source_message = message
        self._stats["parsed"] += 1

        await dispatcher.emit(
            "order.parsed",
            order   =order,
            message =message,
        )

        log.info(
            f"Order parsed: {order.order_id!r}\n"
            f"  Customer: {order.customer}\n"
            f"  Items:    {order.item_summary()}\n"
            f"  Address:  {order.customer.address or '(none)'}"
        )

        # ── Stage 3: Validate ────────────────────────────────────
        errors = self._validator.validate(order)

        if errors:
            log.warning(
                f"Order {order.order_id!r} failed validation:\n"
                + "\n".join(f"  • {e}" for e in errors)
            )
            await dispatcher.emit(
                "order.invalid",
                order  =order,
                errors =errors,
            )
            self._stats["skipped"] += 1
            return None

        self._stats["validated"] += 1
        await dispatcher.emit("order.validated", order=order)

        # ── Stage 4: Assign ──────────────────────────────────────
        assigned_staff = self._assigner.assign(order, [self._staff])
        order.staff_number = assigned_staff.number

        self._stats["assigned"] += 1
        await dispatcher.emit(
            "order.assigned",
            order =order,
            staff =assigned_staff,
        )

        # ── Final: order.detected ────────────────────────────────
        
        
        # This is the terminal event — downstream (Day 5 storage,
        # Day 6 notifications) listens to this event.
        state = await self._state_engine.observe_order(order, message)
        log.info(
            f"Order observed by state engine. "
            f"State v{state.state_version}: "
            f"pending={state.pending_count}"
        )        
        await dispatcher.emit("order.detected", order=order, source="pipeline")
        return order

    def stats(self) -> dict:
        '''Return pipeline processing statistics.'''
        return dict(self._stats)