"""Tests for run_upload shard runtime paths (no browser or CRM)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import apps.sniper.run_upload as ru  # noqa: E402


def _workspace(tmp_path):
    return SimpleNamespace(auth_dir=tmp_path)


def test_shard_zero_uses_canonical_session(tmp_path):
    path = ru._seed_shard_auth_storage(_workspace(tmp_path), (0, 2))
    assert path == tmp_path / "session_state.json"


def test_higher_shard_gets_private_storage_seeded_from_canonical(tmp_path):
    canonical = tmp_path / "session_state.json"
    canonical.write_text("canonical-session", encoding="utf-8")

    path = ru._seed_shard_auth_storage(_workspace(tmp_path), (1, 2))

    assert path == (tmp_path / "workers" / "shard1of2" / "session_state.json")
    assert path.read_text(encoding="utf-8") == "canonical-session"


def test_higher_shard_does_not_overwrite_existing_private_storage(tmp_path):
    canonical = tmp_path / "session_state.json"
    canonical.write_text("canonical-session", encoding="utf-8")
    worker = tmp_path / "workers" / "shard1of2" / "session_state.json"
    worker.parent.mkdir(parents=True)
    worker.write_text("worker-session", encoding="utf-8")

    path = ru._seed_shard_auth_storage(_workspace(tmp_path), (1, 2))

    assert path.read_text(encoding="utf-8") == "worker-session"


def test_higher_shard_without_canonical_still_returns_private_path(tmp_path):
    path = ru._seed_shard_auth_storage(_workspace(tmp_path), (2, 3))

    assert path == (tmp_path / "workers" / "shard2of3" / "session_state.json")
    assert path.parent.is_dir()
    assert not path.exists()
