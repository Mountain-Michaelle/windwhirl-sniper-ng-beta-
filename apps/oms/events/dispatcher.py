import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

from apps.oms.shared.logger import get_logger

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