"""
migrate_to_turso.py

One-time migration utility: copies historical successful order keys from each
client's local SQLite database into the shared Turso production database.

Run separately per client BEFORE the first production run for that client.
Never deletes or modifies the original SQLite file.

Usage (from apps/sniper directory):

    python migrate_to_turso.py --client client_jane_97a9624c --dry-run
    python migrate_to_turso.py --client client_jane_97a9624c

Requires TURSO_URL and TURSO_AUTH environment variables.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper.client_workspace import WorkspaceManager


def production_order_key(client_id: str, order: dict) -> str:
    """Same algorithm as production_tracker.production_order_key."""
    customer_name = str(order.get("customer_name", "")).strip().lower()
    phone = str(order.get("phone_number", order.get("phone", ""))).strip()
    product = str(order.get("product", "")).strip().lower()
    price = str(order.get("price", "")).strip()

    raw = "|".join([client_id, customer_name, phone, product, price])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def find_client_db(client_id: str) -> Path | None:
    """Locate the local SQLite progress DB for a client."""
    manager = WorkspaceManager()
    paths = manager.client_paths(client_id)
    state_dir = paths.root / "state"
    if state_dir.exists():
        for candidate in state_dir.glob("*.db"):
            if "progress" in candidate.name.lower():
                return candidate
    return None


def read_sqlite_keys(db_path: Path) -> list[tuple[str, dict]]:
    """Read all succeeded order rows from the local SQLite DB."""
    rows: list[tuple[str, dict]] = []
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT order_key, customer_name, phone_number, product_text, "
            "price, row_number, succeeded_at FROM succeeded_orders"
        )
        for row in cur.fetchall():
            key, customer_name, phone_number, product_text, price, row_number, succeeded_at = row
            rows.append((
                key,
                {
                    "customer_name": customer_name,
                    "phone_number": phone_number,
                    "product": product_text,
                    "price": price,
                    "row_number": row_number,
                    "succeeded_at": succeeded_at,
                },
            ))
    finally:
        conn.close()
    return rows


def dry_run_report(client_id: str, db_path: Path) -> None:
    """Print a dry-run migration report without touching Turso."""
    rows = read_sqlite_keys(db_path)
    print(f"\n=== DRY-RUN MIGRATION REPORT for client: {client_id} ===")
    print(f"Source SQLite DB: {db_path}")
    print(f"Records found: {len(rows)}")
    if rows:
        print(f"First key sample: {rows[0][0]}")
        print(f"Last key sample:  {rows[-1][0]}")
    print("Would insert into shared Turso DB with client_id scoping.")
    print("No changes made to source SQLite or Turso database.\n")


def migrate(client_id: str, dry_run: bool = False) -> dict:
    """Migrate historical orders from local SQLite to shared Turso DB."""
    db_path = find_client_db(client_id)
    if db_path is None:
        print(f"[migrate] No local SQLite DB found for client {client_id}. Nothing to migrate.")
        return {"source_count": 0, "inserted": 0, "skipped": 0, "errors": 0}

    rows = read_sqlite_keys(db_path)
    source_count = len(rows)
    print(f"[migrate] Found {source_count} historical record(s) in {db_path}")

    if dry_run:
        dry_run_report(client_id, db_path)
        return {"source_count": source_count, "inserted": 0, "skipped": 0, "errors": 0}

    try:
        import libsql
    except ImportError as exc:
        print(f"[migrate] ERROR: libsql package not installed: {exc}")
        return {"source_count": source_count, "inserted": 0, "skipped": 0, "errors": 1}

    turso_url = os.environ.get("TURSO_URL", "")
    turso_auth = os.environ.get("TURSO_AUTH", "")
    if not turso_url or not turso_auth:
        print("[migrate] ERROR: TURSO_URL and TURSO_AUTH must be set for migration.")
        return {"source_count": source_count, "inserted": 0, "skipped": 0, "errors": 1}

    conn = libsql.connect(url=turso_url, auth_token=turso_auth)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS succeeded_orders (
                client_id      TEXT NOT NULL,
                order_key      TEXT NOT NULL,
                customer_name  TEXT NOT NULL,
                phone_number   TEXT NOT NULL,
                product_text   TEXT NOT NULL,
                price          TEXT NOT NULL,
                row_number     INTEGER,
                succeeded_at   TEXT NOT NULL,
                PRIMARY KEY (client_id, order_key)
            )
        """)
        conn.commit()

        inserted = 0
        skipped = 0
        errors = 0

        for key, meta in rows:
            try:
                prod_key = production_order_key(client_id, meta)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO succeeded_orders
                       (client_id, order_key, customer_name, phone_number,
                        product_text, price, row_number, succeeded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        client_id,
                        prod_key,
                        str(meta.get("customer_name", "")).strip(),
                        str(meta.get("phone_number", "")).strip(),
                        str(meta.get("product", "")).strip(),
                        str(meta.get("price", "")).strip(),
                        meta.get("row_number"),
                        meta.get("succeeded_at")
                        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as exc:
                errors += 1
                print(f"[migrate] ERROR inserting key {key}: {exc}")
        conn.commit()

        print(f"[migrate] Complete: source={source_count}, inserted={inserted}, "
              f"skipped={skipped} (already present), errors={errors}")
        return {"source_count": source_count, "inserted": inserted, "skipped": skipped, "errors": errors}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate client's historical SQLite progress to shared Turso DB."
    )
    parser.add_argument("--client", required=True, help="Canonical client ID to migrate.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be migrated without touching Turso.")
    args = parser.parse_args()

    turso_url = os.environ.get("TURSO_URL", "")
    if not args.dry_run and not turso_url:
        print("ERROR: TURSO_URL environment variable is required for migration (not dry-run).")
        raise SystemExit(1)

    result = migrate(args.client, dry_run=args.dry_run)
    if result["errors"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()