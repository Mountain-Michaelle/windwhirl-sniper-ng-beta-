"""Tests for navigation settle behavior (no browser or CRM)."""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper import nav_utils  # noqa: E402


class _FakePage:
    def __init__(self):
        self.url = "https://sniper.ng/orders"
        self.goto_calls = []
        self.load_state_calls = []

    def goto(self, url, wait_until=None):
        self.goto_calls.append((url, wait_until))

    def wait_for_load_state(self, state, timeout=None):
        self.load_state_calls.append((state, timeout))


def test_default_settle_preserves_existing_behavior(monkeypatch):
    sleeps = []
    monkeypatch.setattr(nav_utils.time, "sleep", sleeps.append)
    page = _FakePage()

    nav_utils.safe_goto(page, "https://sniper.ng/orders")

    assert page.goto_calls == [("https://sniper.ng/orders", "commit")]
    assert sleeps == [0.3]


def test_zero_settle_skips_fixed_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(nav_utils.time, "sleep", sleeps.append)
    page = _FakePage()

    nav_utils.safe_goto(page, "https://sniper.ng/orders", settle_ms=0)

    assert page.goto_calls == [("https://sniper.ng/orders", "commit")]
    assert page.load_state_calls == [("domcontentloaded", 15000)]
    assert sleeps == []
