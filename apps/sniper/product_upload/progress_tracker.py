"""
progress_tracker.py

SQLite-backed permanent record of successfully-uploaded orders. Drop-in
replacement for the old JSON-based ProgressTracker -- same class name,
same public methods (is_done, mark_done, count).

Scope, unchanged from the last version:
  - This database tracks ONLY successfully-uploaded orders, permanently.
  - retry_cache.py (JSON) is NOT touched by this file and stays exactly
    as it already is -- transient retry bookkeeping has a different
    lifetime than "permanently done" and was explicitly asked to stay
    on its current logic.
  - Excel remains the only source of order DATA. This database exists
    purely as a dedup reference.
  - CSV exports (failed_exported.csv, fuzzy_matches_for_review.csv,
    FailureLogger's .txt/.jsonl) are entirely separate code paths in
    add_products.py/run_upload.py and are not affected by this file at
    all.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class MissingRequiredFieldError(ValueError):
    """Raised by order_key() when one or more of the four fields it
    hashes on (customer_name, phone_number, product, price) is missing,
    blank, or the literal string "nan" (which is what str(NaN) produces
    for a genuinely blank Excel cell -- see the product_matcher.py and
    state_engine.py fixes earlier in this project for the same root
    cause). A hash built from incomplete data isn't a trustworthy dedup
    key -- failing loudly here beats silently recording (or silently
    failing to match) a 'phantom' fingerprint built from blanks."""
    pass


def order_key(order: dict) -> str:
    """Same four fields, same hash algorithm, same 16-char truncation as
    before -- existing DB/JSON keys remain valid. The only change: every
    field is validated as actually present before hashing, instead of
    silently defaulting a missing one to ''."""
    customer_name = str(order.get("customer_name", "")).strip()
    phone = str(order.get("phone_number", order.get("phone", ""))).strip()
    product = str(order.get("product", "")).strip()
    price = str(order.get("price", "")).strip()

    def _is_blank(value: str) -> bool:
        return value == "" or value.lower() == "nan"

    missing = [
        name for name, value in (
            ("customer_name", customer_name),
            ("phone_number", phone),
            ("product", product),
            ("price", price),
        )
        if _is_blank(value)
    ]
    if missing:
        raise MissingRequiredFieldError(
            f"cannot compute a dedup key -- missing/blank required field(s) "
            f"{missing} (customer_name={customer_name!r}, phone={phone!r}, "
            f"product={product!r}, price={price!r})"
        )

    parts = [customer_name.lower(), phone, product.lower(), price]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class ProgressTracker:
    def __init__(self, path: str = "customers/progress/processed_orders.db"):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS succeeded_orders (
                order_key     TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                phone_number  TEXT NOT NULL,
                product_text  TEXT NOT NULL,
                price         TEXT NOT NULL,
                row_number    INTEGER,
                succeeded_at  TEXT NOT NULL
            )
        """)
        self._conn.commit()

        count = self._conn.execute("SELECT COUNT(*) FROM succeeded_orders").fetchone()[0]
        print(f"[progress] connected to {self.path} -- {count} previously-succeeded "
              f"order(s) on record.", flush=True)

        self._mark_done_calls_this_run: list[str] = []

    def is_done(self, order: dict) -> bool:
        """A row with a missing required field can never have a valid
        key, so it can never be confirmed 'done' -- caught here and
        treated as False (not done) rather than crashing the whole run,
        since is_done() is called on every row BEFORE process_order gets
        a chance to reject it properly with a specific reason."""
        try:
            key = order_key(order)
        except MissingRequiredFieldError as e:
            print(f"[progress] WARNING: {e} -- treating as NOT done "
                  f"(cannot verify).", flush=True)
            return False
        row = self._conn.execute(
            "SELECT 1 FROM succeeded_orders WHERE order_key = ?", (key,)
        ).fetchone()
        return row is not None

    def mark_done(self, order: dict, row_number: Optional[int] = None) -> None:
        """Deliberately does NOT catch MissingRequiredFieldError. By the
        time an order reaches mark_done(), process_order() already
        succeeded end-to-end -- which requires a matched product, a
        resolved price, and verified customer fields -- so all four
        required fields should be provably present already. If this ever
        raises here, that's a genuine invariant violation worth stopping
        the run over, not something to paper over."""
        key = order_key(order)
        self._mark_done_calls_this_run.append(key)

        self._conn.execute(
            """INSERT OR IGNORE INTO succeeded_orders
               (order_key, customer_name, phone_number, product_text, price,
                row_number, succeeded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                key,
                str(order.get("customer_name", "")).strip(),
                str(order.get("phone_number", order.get("phone", ""))).strip(),
                str(order.get("product", "")).strip(),
                str(order.get("price", "")).strip(),
                row_number,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

        row = self._conn.execute(
            "SELECT 1 FROM succeeded_orders WHERE order_key = ?", (key,)
        ).fetchone()
        if row is None:
            print(f"[progress] WARNING: just committed {key} for "
                  f"{order.get('customer_name')!r}, but it's NOT showing up "
                  f"on read-back. This should be essentially impossible with "
                  f"SQLite's own durability guarantees.", flush=True)

    @property
    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM succeeded_orders").fetchone()[0]

    def summarize_run(self) -> str:
        calls = len(self._mark_done_calls_this_run)
        distinct = len(set(self._mark_done_calls_this_run))
        lines = [f"[progress] mark_done() called {calls} time(s) this run "
                 f"({distinct} distinct order(s))."]
        if calls != distinct:
            dupes = calls - distinct
            lines.append(f"[progress] {dupes} of those were the SAME order succeeding "
                         f"more than once this run (expected -- INSERT OR IGNORE "
                         f"collapses repeats to one row).")
        return "\n".join(lines)

    def verify_final_state(self) -> Optional[str]:
        missing = [
            key for key in set(self._mark_done_calls_this_run)
            if self._conn.execute(
                "SELECT 1 FROM succeeded_orders WHERE order_key = ?", (key,)
            ).fetchone() is None
        ]
        if not missing:
            return None
        return (f"{len(missing)} key(s) mark_done() was asked to save this run are "
                f"NOT in the table: {missing[:5]}")

    def close(self) -> None:
        self._conn.close()

    def import_from_json_backup(self, json_path: str) -> int:
        """NOTE: the old JSON only ever stored the hash, never the
        original order content, so imported rows can't be validated
        against order_key()'s new required-field check -- they're
        inserted as-is (blank metadata columns), same as before."""
        p = Path(json_path)
        if not p.exists():
            print(f"[progress] no legacy JSON file found at {p} -- nothing to import.")
            return 0
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        keys = data.get("succeeded_keys", [])
        imported = 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key in keys:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO succeeded_orders
                   (order_key, customer_name, phone_number, product_text, price,
                    row_number, succeeded_at)
                   VALUES (?, '', '', '', '', NULL, ?)""",
                (key, now),
            )
            imported += cur.rowcount
        self._conn.commit()
        print(f"[progress] imported {imported} key(s) from legacy {p} "
              f"({len(keys) - imported} already present, skipped).")
        return imported
