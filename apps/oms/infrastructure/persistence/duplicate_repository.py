from __future__ import annotations

from typing import Optional

from apps.oms.infrastructure.persistence.schema import (
    DuplicateGroupRecord, DuplicateMemberRecord
)
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class DuplicateRepository:
    '''
    Persists and retrieves duplicate detection results.

    Usage:
        repo = DuplicateRepository(session_factory)
        await repo.save_group(duplicate_group)
        group = await repo.get_group(group_id)
        groups = await repo.get_groups_for_order(order_id)
    '''

    def __init__(self, session_factory):
        self._sf  = session_factory
        self._log = log

    async def save_group(self, group) -> str:
        '''
        Persist or update a DuplicateGroup.
        Upserts by group_id. Adds new members without removing existing.

        Args:
            group: DuplicateGroup from Day 9.

        Returns:
            The group_id saved.
        '''
        with self._sf() as session:
            existing = session.get(DuplicateGroupRecord, group.group_id)

            if existing:
                group_record = existing
            else:
                group_record = DuplicateGroupRecord(
                    group_id           =group.group_id,
                    canonical_order_id =group.canonical_order_id,
                    classification     =group.classification,
                )
                session.add(group_record)
                session.flush()

            # Add any new members not already in the DB
            existing_order_ids = {
                m.order_id for m in
                session.query(DuplicateMemberRecord)
                       .filter(DuplicateMemberRecord.group_id == group.group_id)
                       .all()
            }

            for order_id in group.member_order_ids:
                if order_id not in existing_order_ids:
                    member = DuplicateMemberRecord(
                        group_id     =group.group_id,
                        order_id     =order_id,
                        is_canonical =(order_id == group.canonical_order_id),
                    )
                    session.add(member)

            session.commit()
            self._log.info(
                f"DuplicateRepository: saved group {group.group_id!r} "
                f"({len(group.member_order_ids)} members)"
            )

        return group.group_id

    async def get_group(self, group_id: str) -> Optional[DuplicateGroupRecord]:
        '''Retrieve a group by ID.'''
        with self._sf() as session:
            return session.get(DuplicateGroupRecord, group_id)

    async def get_groups_for_order(
        self,
        order_id: str
    ) -> list[DuplicateGroupRecord]:
        '''All groups that contain a given order.'''
        with self._sf() as session:
            member_records = (
                session.query(DuplicateMemberRecord)
                       .filter(DuplicateMemberRecord.order_id == order_id)
                       .all()
            )
            group_ids = [m.group_id for m in member_records]
            if not group_ids:
                return []

            return (
                session.query(DuplicateGroupRecord)
                       .filter(DuplicateGroupRecord.group_id.in_(group_ids))
                       .all()
            )

    async def get_unresolved_groups(self) -> list[DuplicateGroupRecord]:
        '''All groups not yet reviewed by a human.'''
        with self._sf() as session:
            return (
                session.query(DuplicateGroupRecord)
                       .filter(DuplicateGroupRecord.resolved == False)
                       .order_by(DuplicateGroupRecord.created_at.desc())
                       .all()
            )
