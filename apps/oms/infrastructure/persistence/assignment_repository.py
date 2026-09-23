from __future__ import annotations

from typing import Optional

from apps.oms.infrastructure.persistence.schema import AssignmentRecord
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class AssignmentRepository:
    '''
    Append-only persistence for assignment history.
    Every assignment event → one new row. Never updates.

    Usage:
        repo = AssignmentRepository(session_factory)
        await repo.save(resolved_assignment)
        history = await repo.for_order("ORD-001")
        latest  = await repo.latest_for_order("ORD-001")
    '''

    def __init__(self, session_factory):
        self._sf  = session_factory
        self._log = log

    async def save(self, resolution) -> str:
        '''
        Persist a ResolvedAssignment as an AssignmentRecord.
        Creates a new row — never updates existing rows.

        Args:
            resolution: ResolvedAssignment from Day 6.

        Returns:
            The history_id of the saved record.
        '''
        with self._sf() as session:
            record = AssignmentRecord(
                history_id     =resolution.history_id,
                order_id       =resolution.order_id,
                worker_number  =resolution.worker_number,
                worker_name    =resolution.worker_name,
                rule           =resolution.rule.value
                                if hasattr(resolution.rule, 'value')
                                else str(resolution.rule),
                status         =resolution.status.value
                                if hasattr(resolution.status, 'value')
                                else str(resolution.status),
                window_id      =resolution.window_id,
                previous_worker=resolution.previous_worker,
                notes          =resolution.notes,
                resolved_at    =resolution.resolved_at,
            )
            session.add(record)
            session.commit()

            self._log.info(
                f"AssignmentRepository: saved {record}"
            )

        return resolution.history_id

    async def for_order(self, order_id: str) -> list[AssignmentRecord]:
        '''All assignment records for an order, oldest first.'''
        with self._sf() as session:
            return (
                session.query(AssignmentRecord)
                       .filter(AssignmentRecord.order_id == order_id)
                       .order_by(AssignmentRecord.created_at.asc())
                       .all()
            )

    async def latest_for_order(self, order_id: str) -> Optional[AssignmentRecord]:
        '''Most recent assignment record for an order.'''
        with self._sf() as session:
            return (
                session.query(AssignmentRecord)
                       .filter(AssignmentRecord.order_id == order_id)
                       .order_by(AssignmentRecord.created_at.desc())
                       .first()
            )

    async def for_worker(
        self,
        worker_number: str,
        limit: int = 500,
    ) -> list[AssignmentRecord]:
        '''All assignments ever made to a worker.'''
        with self._sf() as session:
            return (
                session.query(AssignmentRecord)
                       .filter(AssignmentRecord.worker_number == worker_number)
                       .order_by(AssignmentRecord.created_at.desc())
                       .limit(limit)
                       .all()
            )
