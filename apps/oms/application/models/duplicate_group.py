from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class DuplicateGroup:
    '''
    A cluster of orders believed to be duplicates of each other.

    canonical_order_id: The first / oldest order in the group.
                        All others are considered duplicates of this.
    member_order_ids:   All order IDs in this group including canonical.
    classification:     The strongest classification among all pairwise comparisons.
    created_at:         When this group was first formed.
    updated_at:         When the group was last modified (member added).
    resolved:           True if a human has reviewed and resolved this group.
    resolution_notes:   What the human decided.
    '''
    canonical_order_id: str
    group_id:           str      = field(default_factory=lambda: str(uuid.uuid4())[:8])
    member_order_ids:   list[str]= field(default_factory=list)
    classification:     str      = "LIKELY_DUPLICATE"
    created_at:         datetime = field(default_factory=datetime.now)
    updated_at:         datetime = field(default_factory=datetime.now)
    resolved:           bool     = False
    resolution_notes:   str      = ""

    def __post_init__(self):
        # Canonical is always a member
        if self.canonical_order_id not in self.member_order_ids:
            self.member_order_ids.insert(0, self.canonical_order_id)

    def add_member(self, order_id: str) -> None:
        '''Add an order to this group if not already present.'''
        if order_id not in self.member_order_ids:
            self.member_order_ids.append(order_id)
            self.updated_at = datetime.now()

    def has_member(self, order_id: str) -> bool:
        return order_id in self.member_order_ids

    @property
    def size(self) -> int:
        return len(self.member_order_ids)

    @property
    def duplicate_count(self) -> int:
        '''Number of duplicates (excluding the canonical).'''
        return max(0, self.size - 1)

    def resolve(self, notes: str) -> None:
        self.resolved          = True
        self.resolution_notes  = notes
        self.updated_at        = datetime.now()

    def __repr__(self):
        return (
            f"DuplicateGroup("
            f"id={self.group_id!r}, "
            f"canonical={self.canonical_order_id!r}, "
            f"members={self.size}, "
            f"resolved={self.resolved})"
        )
