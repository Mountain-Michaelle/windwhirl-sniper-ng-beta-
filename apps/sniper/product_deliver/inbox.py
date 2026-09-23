"""status-inbox distribution for the deliver flow.

Drop a customer file anywhere in apps/sniper/status_inbox/ with the
client's name anywhere in the filename (e.g. michael_orders.xlsx);
on the next run it is moved to
  clients/<canonical_client_id>/jobs/status/<filename>
so every client's deliver inputs live in one tracked place.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

INBOX_DIR = Path(__file__).resolve().parents[1] / "status_inbox"
SUPPORTED_SUFFIXES = {".xlsx", ".xls", ".csv", ".json", ".txt"}


def match_name_keys(filename_stem: str, registry: dict) -> list[str]:
    """Pure matching: which registry name_keys appear in the filename?

    Longest key wins on multi-match inside one client; two DIFFERENT
    clients matching = ambiguous (returns both; caller leaves file put).
    """
    stem = str(filename_stem).lower()
    matched = set()
    for key in (registry or {}).get("name_keys", {}):
        if len(key) >= 3 and key.lower() in stem:
            matched.add(key)
    return sorted(matched, key=len, reverse=True)


def resolve_unique_client(filename: str, registry: dict,
                          resolver) -> str | None:
    """Canonical client id for a filename or None. `resolver` maps a
    name_key to its canonical id (WorkspaceManager.resolve_client_id)."""
    keys = match_name_keys(filename, registry)
    if not keys:
        return None
    return resolver(keys[0])


def distribute(manager, inbox_dir: Path | None = None,
               log=print) -> dict[str, list[Path]]:
    """Move every matchable inbox file into its client's jobs/status dir.
    Returns {canonical_client_id: [moved paths]}."""
    inbox_dir = Path(inbox_dir) if inbox_dir else INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    registry = manager._load_registry()
    moved: dict[str, list[Path]] = {}
    for src in sorted(inbox_dir.iterdir()):
        if not src.is_file() or src.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        client_id = resolve_unique_client(src.name, registry,
                                          manager.resolve_client_id)
        if client_id is None:
            log(f"[inbox] no client matched in filename: {src.name}")
            continue
        dest_dir = manager.client_paths(client_id).jobs_dir / "status"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.exists():
            stem, suffix = src.stem, src.suffix
            dest = dest_dir / f"{stem}_{int(time.time())}{suffix}"
        os.replace(str(src), str(dest))
        moved.setdefault(client_id, []).append(dest)
        log(f"[inbox] {src.name} -> {dest}")
    return moved
