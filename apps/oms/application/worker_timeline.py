from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from apps.oms.application.worker_context import WorkerContextEntry


@dataclass(frozen=True)
class WorkerTimelineEntry:
    '''
    One immutable record of a worker context change.

    worker_number:  Phone number of the mentioned worker.
    display_name:   Name as it appeared in the message.
    changed_at:     When this context became active.
    window_id:      Active window at the time.
    raw_message_id: Source message fingerprint.
    sequence_num:   Position in the worker timeline (1-based).
    '''
    worker_number:  str
    display_name:   str
    changed_at:     datetime
    window_id:      str
    raw_message_id: str
    sequence_num:   int

    def __repr__(self):
        return (
            f"WorkerTimelineEntry("
            f"#{self.sequence_num}, "
            f"+{self.worker_number}, "
            f"name={self.display_name!r}, "
            f"window={self.window_id!r})"
        )


class WorkerTimeline:
    '''
    Append-only record of every worker context change.
    Never modified after recording. Never reordered.

    Usage:
        timeline = WorkerTimeline()
        timeline.record(worker_number, display_name, window_id, message_id)
        all_changes = timeline.all_entries()
    '''

    def __init__(self):
        self._entries: list[WorkerTimelineEntry] = []

    def record(
        self,
        worker_number:  str,
        display_name:   str,
        window_id:      str,
        raw_message_id: str,
    ) -> WorkerTimelineEntry:
        '''
        Record a worker context change.

        Args:
            worker_number:  Phone number of the new worker context.
            display_name:   How the worker was mentioned (@Michael etc).
            window_id:      ID of the currently active window.
            raw_message_id: Fingerprint of the source message.

        Returns:
            The immutable WorkerTimelineEntry created.
        '''
        entry = WorkerTimelineEntry(
            worker_number  =worker_number,
            display_name   =display_name,
            changed_at     =datetime.now(),
            window_id      =window_id,
            raw_message_id =raw_message_id,
            sequence_num   =len(self._entries) + 1,
        )
        self._entries.append(entry)
        return entry

    def all_entries(self) -> list[WorkerTimelineEntry]:
        return list(self._entries)

    def for_window(self, window_id: str) -> list[WorkerTimelineEntry]:
        return [e for e in self._entries if e.window_id == window_id]

    def latest(self) -> Optional[WorkerTimelineEntry]:
        return self._entries[-1] if self._entries else None

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def __repr__(self):
        return f"WorkerTimeline(total={self.total_count})"