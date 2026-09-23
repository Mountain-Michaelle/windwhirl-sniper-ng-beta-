from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class WorkerContextEntry:
    '''
    One worker context state at a point in time.
    Stored in the WorkerTimeline — never mutated after creation.

    worker_number:   Phone number of the mentioned worker.
    display_name:    Name as mentioned in the WhatsApp message.
    mentioned_at:    When this context became active.
    raw_message_id:  Source message fingerprint for traceability.
    window_id:       Which assignment window was active at the time.
    '''
    worker_number:  str
    display_name:   str
    raw_message_id: str
    window_id:      str
    mentioned_at:   datetime = field(default_factory=datetime.now)

    def __repr__(self):
        return (
            f"WorkerContextEntry("
            f"+{self.worker_number}, "
            f"name={self.display_name!r}, "
            f"at={self.mentioned_at.strftime('%H:%M:%S')})"
        )


class CurrentWorkerContext:
    '''
    Tracks the currently active worker context.

    Mutable — changes every time a new worker is mentioned.
    The WorkerTimeline records every historical change.
    This class only holds the CURRENT state.

    Remember: this is NOT ownership.
    It is simply the most recently mentioned worker.
    '''

    def __init__(self):
        self._current: Optional[WorkerContextEntry] = None

    @property
    def active(self) -> Optional[WorkerContextEntry]:
        '''The current worker context entry, or None if none set.'''
        return self._current

    @property
    def worker_number(self) -> str:
        '''Current worker phone number, or empty string.'''
        return self._current.worker_number if self._current else ""

    @property
    def display_name(self) -> str:
        '''Current worker display name, or empty string.'''
        return self._current.display_name if self._current else ""

    @property
    def is_active(self) -> bool:
        '''True if a worker context is currently set.'''
        return self._current is not None

    def update(self, entry: WorkerContextEntry) -> None:
        '''
        Set a new worker context.
        The previous context is NOT stored here —
        it is stored in WorkerTimeline.

        Args:
            entry: The new WorkerContextEntry to set as current.
        '''
        self._current = entry

    def clear(self) -> None:
        '''Clear the current worker context. Used when window closes.'''
        self._current = None

    def __repr__(self):
        if self._current:
            return (
                f"CurrentWorkerContext("
                f"+{self.worker_number}, "
                f"{self.display_name!r})"
            )
        return "CurrentWorkerContext(none)"