"""
progress_tracker.py

Local, persistent "how far did we get" cache. Lets a run resume from where
it left off instead of restarting the whole sheet from row 1.

Deliberately NOT keyed by row position -- each order gets a content-based
key (customer name + phone + product + price) that stays stable across
runs as long as that particular order line itself hasn't changed.

Stores one JSON file: a flat list of keys that have already SUCCEEDED.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional


def order_key(order: dict) -> str:
    """Stable identity for one order line, independent of its row position
    in the sheet."""
    parts = [
        str(order.get("customer_name", "")).strip().lower(),
        str(order.get("phone_number", order.get("phone", ""))).strip(),
        str(order.get("product", "")).strip().lower(),
        str(order.get("price", "")).strip(),
    ]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class ProgressTracker:
    def __init__(self, path: str = "customers/progress/processed_orders.json"):
        # .resolve() -- the printed path is always the FULL absolute path
        # actually being read/written, not whatever relative string got
        # passed in. If two different copies of run_upload.py (or two
        # different working directories) end up pointing at two different
        # physical files, this makes that immediately visible instead of
        # both printing the same ambiguous relative string.
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._processed: set[str] = self._load()

        # Everything mark_done() is asked to save THIS run, in call order,
        # duplicates included. Lets end-of-run reporting distinguish "the
        # same order legitimately succeeded twice this run" (expected -- a
        # set just collapses that to one key) from "a save silently didn't
        # happen" (a real bug).
        self._mark_done_calls_this_run: list[str] = []

    def _load(self) -> set[str]:
        if not self.path.exists():
            print(f"[progress] no cache file at {self.path} yet -- starting from empty.",
                  flush=True)
            return set()
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            keys = set(data.get("succeeded_keys", []))
            print(f"[progress] loaded {len(keys)} previously-succeeded key(s) from {self.path}",
                  flush=True)
            return keys
        except (OSError, json.JSONDecodeError) as e:
            # This used to fail silently. A silent fallback to empty here
            # is exactly how a whole batch gets reprocessed without any
            # visible warning -- now it's a loud, unmissable line at the
            # very start of the run instead.
            print(f"[progress] WARNING: could not read {self.path} ({e}) -- "
                  f"starting from EMPTY. Any previously-completed orders will "
                  f"be treated as new and MAY BE RESUBMITTED.", flush=True)
            return set()

    def _save(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"succeeded_keys": sorted(self._processed)}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # force onto physical disk before the rename --
            # close() alone only guarantees the OS knows about the write, not
            # that it's physically on disk yet.
        os.replace(tmp_path, self.path)  # atomic on POSIX and Windows

    def is_done(self, order: dict) -> bool:
        return order_key(order) in self._processed

    def mark_done(self, order: dict) -> None:
        key = order_key(order)
        self._mark_done_calls_this_run.append(key)
        self._processed.add(key)
        self._save()

        # Per-order verification: read the file back immediately and
        # confirm THIS key is actually there. Fires the instant a single
        # save fails, naming the exact order it happened on.
        try:
            with open(self.path, encoding="utf-8") as f:
                on_disk = set(json.load(f).get("succeeded_keys", []))
            if key not in on_disk:
                print(f"[progress] WARNING: just saved {key} for "
                      f"{order.get('customer_name')!r}, but re-reading "
                      f"{self.path} does NOT show it. The save silently "
                      f"failed -- possible causes: disk full, permissions, "
                      f"or another process (backup/sync tool, antivirus) "
                      f"rewriting this file at the same moment.", flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[progress] WARNING: could not verify the save of {key} "
                  f"for {order.get('customer_name')!r} -- re-read failed: {e}",
                  flush=True)

    @property
    def count(self) -> int:
        return len(self._processed)

    def summarize_run(self) -> str:
        """Called once at the end of main(). Reports how many times
        mark_done() was invoked this run vs. how many DISTINCT orders that
        represents -- a gap here is expected (a set collapses a genuine
        repeat success to one key) and reported for visibility, not as an
        error."""
        calls = len(self._mark_done_calls_this_run)
        distinct = len(set(self._mark_done_calls_this_run))
        lines = [f"[progress] mark_done() called {calls} time(s) this run "
                 f"({distinct} distinct order(s))."]
        if calls != distinct:
            dupes = calls - distinct
            lines.append(f"[progress] {dupes} of those were the SAME order succeeding "
                         f"more than once this run (expected to collapse to one key each).")
        return "\n".join(lines)

    def verify_final_state(self) -> Optional[str]:
        """Re-reads the file from disk one final time and compares it
        against the in-memory set, end to end. Returns None if they match
        exactly, or a diagnostic string describing exactly what's
        missing/extra -- catches any drift accumulated over the WHOLE run,
        not just a single save."""
        try:
            with open(self.path, encoding="utf-8") as f:
                on_disk = set(json.load(f).get("succeeded_keys", []))
        except (OSError, json.JSONDecodeError) as e:
            return f"could not read {self.path} for final verification: {e}"

        missing_from_disk = self._processed - on_disk
        extra_on_disk = on_disk - self._processed
        if not missing_from_disk and not extra_on_disk:
            return None

        parts = []
        if missing_from_disk:
            sample = sorted(missing_from_disk)[:5]
            parts.append(f"{len(missing_from_disk)} key(s) tracked in memory but "
                         f"MISSING from disk (sample: {sample})")
        if extra_on_disk:
            sample = sorted(extra_on_disk)[:5]
            parts.append(f"{len(extra_on_disk)} key(s) on disk but not tracked in memory "
                         f"this run -- likely written by another process or a "
                         f"previous run (sample: {sample})")
        return "; ".join(parts)