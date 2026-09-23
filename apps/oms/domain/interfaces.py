from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from apps.oms.domain.entities import Order, RawMessage, Staff


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