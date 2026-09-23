from apps.oms.domain.entities import Order, Staff
from apps.oms.domain.interfaces import IAssignmentEngine
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class SingleStaffAssignmentEngine(IAssignmentEngine):
    '''
    Assignment engine for single-staff OMS instances.
    Always assigns every order to the one configured staff member.

    This is the correct implementation for Nabeau Store's current
    setup where one OMS instance monitors one staff member's orders.

    Usage:
        engine = SingleStaffAssignmentEngine(staff)
        assigned = engine.assign(order, available_staff=[staff])
    '''

    def __init__(self, staff: Staff):
        '''
        Args:
            staff: The Staff member to assign all orders to.
        '''
        self._staff = staff

    def assign(self, order: Order, available_staff: list[Staff]) -> Staff:
        '''
        Always returns the configured staff member.
        available_staff list is accepted but not used in this
        implementation (kept for interface compatibility).

        Args:
            order:           The order to assign (used for logging).
            available_staff: Ignored in single-staff mode.

        Returns:
            The configured Staff member.
        '''
        log.debug(
            f"Assigning order {order.order_id!r} to "
            f"staff +{self._staff.number}"
        )
        return self._staff