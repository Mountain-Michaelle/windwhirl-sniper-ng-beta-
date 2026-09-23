import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


@dataclass
class Checkpoint:
    '''
    A saved recovery point — the last successfully processed message.

    fingerprint:  Message fingerprint (for matching in DOM during recovery).
    timestamp_str: Timestamp string as shown in WhatsApp ("14:32", "Yesterday").
    saved_at:     When this checkpoint was saved (ISO format string).
    message_preview: Short preview of the message text (for debugging).
    '''
    fingerprint:      str
    timestamp_str:    str
    saved_at:         str
    message_preview:  str = ""

    @staticmethod
    def now_str() -> str:
        return datetime.now().isoformat()


class CheckpointStore:
    '''
    Persists and retrieves recovery checkpoints.

    Stores the last N fingerprints seen before the OMS went offline.
    RecoveryManager uses these to find where to resume in the chat.

    Usage:
        store = CheckpointStore(group_name="Nabeau Orders", max_history=5)
        store.save(checkpoint)
        history = store.load_history()   # List[Checkpoint], most recent first
    '''

    DEFAULT_MAX_HISTORY = 5   # Store last 5 checkpoints

    def __init__(
        self,
        group_name:  str,
        data_dir:    str = "data",
        max_history: int = DEFAULT_MAX_HISTORY,
    ):
        '''
        Args:
            group_name:  The WhatsApp group name — used in the filename
                         so each group has its own checkpoint file.
            data_dir:    Directory to store checkpoint files.
            max_history: How many checkpoints to retain.
        '''
        self._max_history = max_history
        self._data_dir    = Path(data_dir)

        # Build a safe filename from the group name
        safe_name    = "".join(c if c.isalnum() else "_" for c in group_name)
        self._path   = self._data_dir / f"oms_checkpoint_{safe_name}.json"

        log.debug(f"CheckpointStore: {self._path}")

    def save(self, checkpoint: Checkpoint) -> None:
        '''
        Save a checkpoint to disk.
        Prepends to history (most recent first).
        Trims history to max_history entries.
        Atomic write — safe against crashes mid-write.

        Args:
            checkpoint: The checkpoint to save.
        '''
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Load existing history
        history = self.load_history()

        # Prepend new checkpoint (most recent first)
        history.insert(0, checkpoint)

        # Trim to max history size
        history = history[:self._max_history]

        # Convert to serializable format
        data = {
            "version":  1,
            "updated":  Checkpoint.now_str(),
            "history":  [asdict(cp) for cp in history],
        }

        # Atomic write: write to temp file, then rename
        # This prevents corrupt checkpoint files if OMS crashes mid-write
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix=".tmp")

        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # Atomic rename — replaces the old file in one OS operation
            os.replace(tmp_path, self._path)
            log.debug(
                f"Checkpoint saved: fp={checkpoint.fingerprint!r}, "
                f"ts={checkpoint.timestamp_str!r}"
            )

        except Exception as e:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            log.error(f"Failed to save checkpoint: {e}", exc_info=True)

    def load_history(self) -> list[Checkpoint]:
        '''
        Load checkpoint history from disk.
        Returns empty list if no checkpoint file exists.
        Most recent checkpoint is first in the list.

        Returns:
            List of Checkpoint objects, most recent first.
        '''
        if not self._path.exists():
            log.debug("No checkpoint file found — starting fresh.")
            return []

        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)

            history = [
                Checkpoint(**cp)
                for cp in data.get("history", [])
            ]

            log.debug(
                f"Loaded {len(history)} checkpoint(s) from {self._path}"
            )
            return history

        except Exception as e:
            log.warning(
                f"Could not load checkpoint file: {e}\n"
                f"Starting recovery from scratch."
            )
            return []

    def latest(self) -> Optional[Checkpoint]:
        '''
        The most recent checkpoint, or None if no history exists.
        '''
        history = self.load_history()
        return history[0] if history else None

    def all_fingerprints(self) -> list[str]:
        '''
        All fingerprints from checkpoint history.
        Used by MessageCache.seed() to pre-populate the cache
        before recovery begins, preventing double-processing.
        '''
        return [cp.fingerprint for cp in self.load_history()]

    def offline_duration_hours(self) -> Optional[float]:
        '''
        Calculate how long the OMS was offline based on the latest checkpoint.
        Returns None if no checkpoint exists (first ever run).

        Used by RecoveryManager to decide whether to attempt recovery.
        '''
        latest = self.latest()
        if not latest:
            return None

        try:
            saved_at = datetime.fromisoformat(latest.saved_at)
            delta    = datetime.now() - saved_at
            return delta.total_seconds() / 3600  # Convert to hours
        except Exception as e:
            log.warning(f"Could not parse checkpoint timestamp: {e}")
            return None

    def clear(self) -> None:
        '''Delete the checkpoint file. Used for fresh starts.'''
        if self._path.exists():
            self._path.unlink()
            log.info(f"Checkpoint cleared: {self._path}")

    def __repr__(self):
        latest = self.latest()
        return (
            f"CheckpointStore("
            f"path={self._path.name!r}, "
            f"latest={latest.fingerprint[:8] if latest else None!r}"
            f")"
        )