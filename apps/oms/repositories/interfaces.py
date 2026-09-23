from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from apps.oms.domain.entities import Order, OrderStatus


class IOrderRepository(ABC):
    '''
    Interface for order persistence.
    Implementation: SQLite repository (Day 4).

    The domain never imports SQLAlchemy or any DB library.
    It only calls these methods — the implementation handles storage.
    '''

    @abstractmethod
    async def save(self, order: Order) -> Order:
        '''
        Persist a new order or update an existing one.
        Returns the saved order (with any DB-assigned fields like id).
        '''
        pass

    @abstractmethod
    async def get_by_id(self, order_id: str) -> Optional[Order]:
        '''
        Return an order by its ID, or None if not found.
        '''
        pass

    @abstractmethod
    async def get_by_status(self, status: OrderStatus) -> list[Order]:
        '''
        Return all orders with the given status.
        '''
        pass

    @abstractmethod
    async def get_by_staff(
        self,
        staff_number: str,
        status: Optional[OrderStatus] = None
    ) -> list[Order]:
        '''
        Return orders assigned to a staff member.
        Optionally filtered by status.
        '''
        pass

    @abstractmethod
    async def get_recent(
        self,
        since: datetime,
        limit: int = 50
    ) -> list[Order]:
        '''
        Return orders detected after a given timestamp.
        Used to check for duplicate orders in a time window.
        '''
        pass

    @abstractmethod
    async def exists(self, order_id: str) -> bool:
        '''True if an order with this ID already exists.'''
        pass

    @abstractmethod
    async def count_by_status(self) -> dict[str, int]:
        '''
        Return count of orders grouped by status.
        Used for reporting and dashboard metrics.
        Example: {"DETECTED": 5, "CONFIRMED": 12, "DELIVERED": 30}
        '''
        pass