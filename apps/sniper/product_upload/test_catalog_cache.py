"""Tests for catalog-cache atomicity and shard single-flight refresh locking.

These exercise only pure helpers / cache I/O -- no browser, no CRM. They run
with a real temporary directory so the atomic replace and lock-file handling
are tested against the actual filesystem.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import apps.sniper.run_upload as ru  # noqa: E402


def _point_at(tmp_path, monkeypatch):
    """Redirect all catalog cache globals at ``tmp_path`` for one test."""
    monkeypatch.setattr(ru, "PROGRESS_DIR", tmp_path)
    monkeypatch.setattr(ru, "CATALOG_CACHE_PATH", tmp_path / "catalog_cache.json")
    monkeypatch.setattr(ru, "CATALOG_METADATA_PATH", tmp_path / "catalog_metadata.json")


def _no_scrape(monkeypatch):
    calls: list[object] = []

    def fake(page):
        calls.append(page)
        return ["LIVE-SCRAPE"]

    monkeypatch.setattr(ru, "scrape_crm_catalog", fake)
    return calls


def test_atomic_write_text_replaces_content_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "value.json"
    target.write_text("old", encoding="utf-8")

    ru._atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["value.json"]


def test_write_and_read_catalog_cache_round_trip(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    ru._write_catalog_cache(["Widget A", "Widget B"])

    assert ru._read_catalog_cache() == ["Widget A", "Widget B"]
    metadata = json.loads((tmp_path / "catalog_metadata.json").read_text(encoding="utf-8"))
    assert metadata["product_count"] == 2
    # No ``.catalog_cache.json.<pid>.tmp`` leftovers from the atomic replace.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "catalog_cache.json", "catalog_metadata.json",
    ]


def test_write_catalog_cache_failure_preserves_previous_cache(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    ru._write_catalog_cache(["OLD"])

    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(ru, "_atomic_write_text", boom)

    ru._write_catalog_cache(["NEW"])

    # The reader never sees a half-written or empty file.
    assert ru._read_catalog_cache() == ["OLD"]


def test_acquire_catalog_refresh_lock_returns_held_lock_when_free(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    lock = ru._acquire_catalog_refresh_lock(timeout_s=1.0)

    assert lock is not None
    lock_path = tmp_path / "catalog_refresh.lock"
    assert lock_path.exists()
    lock.__exit__(None, None, None)
    assert not lock_path.exists()


def test_acquire_catalog_refresh_lock_times_out_on_live_owner(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    lock_path = tmp_path / "catalog_refresh.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    lock = ru._acquire_catalog_refresh_lock(timeout_s=0.0)

    assert lock is None
    # A live owner's lock must be left alone for it to release normally.
    assert lock_path.exists()


def test_acquire_catalog_refresh_lock_reclaims_dead_owner(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    lock_path = tmp_path / "catalog_refresh.lock"
    lock_path.write_text(json.dumps({"pid": 2147483647}), encoding="utf-8")

    lock = ru._acquire_catalog_refresh_lock(timeout_s=1.0)

    assert lock is not None
    lock.__exit__(None, None, None)
    assert not lock_path.exists()


def test_load_catalog_reuses_cache_populated_while_waiting(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    products = ["Widget A"]

    def fake_lock(timeout_s=0.0):
        # Simulate a sibling shard finishing the scrape while we blocked.
        ru._write_catalog_cache(products)
        return None

    monkeypatch.setattr(ru, "_acquire_catalog_refresh_lock", fake_lock)
    calls = _no_scrape(monkeypatch)

    result = ru._load_catalog(object(), lock_mode=False, refresh_catalog=False)

    assert result == products
    assert calls == []


def test_load_catalog_refresh_reuses_cache_written_while_waiting(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    ru._write_catalog_cache(["OLD"])
    stale = time.time() - 100
    os.utime(tmp_path / "catalog_cache.json", (stale, stale))

    def fake_lock(timeout_s=0.0):
        ru._write_catalog_cache(["NEW"])
        fresh = time.time() + 10
        os.utime(tmp_path / "catalog_cache.json", (fresh, fresh))
        return None

    monkeypatch.setattr(ru, "_acquire_catalog_refresh_lock", fake_lock)
    calls = _no_scrape(monkeypatch)

    result = ru._load_catalog(object(), lock_mode=False, refresh_catalog=True)

    assert result == ["NEW"]
    assert calls == []


def test_load_catalog_scrapes_and_caches_when_no_sibling_wrote(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    monkeypatch.setattr(ru, "_acquire_catalog_refresh_lock",
                        lambda timeout_s=0.0: None)
    calls = _no_scrape(monkeypatch)

    result = ru._load_catalog(object(), lock_mode=False, refresh_catalog=False)

    assert result == ["LIVE-SCRAPE"]
    assert len(calls) == 1
    assert ru._read_catalog_cache() == ["LIVE-SCRAPE"]
