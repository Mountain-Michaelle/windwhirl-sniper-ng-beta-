"""
maintenance.py

Database maintenance and purge controls for the shared Turso production tracker.

Normal uploads must NEVER purge data automatically. All operations here are
explicit, require confirmations, and preserve successful order keys by default.

Usage (from apps/sniper directory):

    python maintenance.py inspect
    python maintenance.py retention --max-days 90 --dry-run
    python maintenance.py retention --max-days 90
    python maintenance.py reset-client --client client_jane_97a9624c --confirm RESET_CLIENT
    python maintenance.py reset-all --confirm RESET_ALL_PRODUCTION_TRACKING

Requires TURSO_URL and TURSO_AUTH environment variables.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    import libsql
except ImportError as exc:
    raise ImportError(
        "maintenance.py requires the 'libsql' package for Turso access. "
        "Install it with: pip install libsql"
    ) from exc


def _connect():
    url = os.environ.get("TURSO_URL", "")
    auth = os.environ.get("TURSO_AUTH", "")
    if not url or not auth:
        raise RuntimeError(
            "TURSO_URL and TURSO_AUTH must be set for maintenance operations."
        )
    return libsql.connect(url=url, auth_token=auth)


def _ensure_schema(conn):
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retry_queue (
            client_id     TEXT NOT NULL,
            order_key     TEXT NOT NULL,
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_reason   TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            PRIMARY KEY (client_id, order_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed_ledger (
            client_id        TEXT NOT NULL,
            order_key        TEXT NOT NULL,
            attempts         INTEGER NOT NULL DEFAULT 0,
            reason           TEXT NOT NULL,
            first_failed_at  TEXT NOT NULL,
            last_attempt_at  TEXT NOT NULL,
            PRIMARY KEY (client_id, order_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_history (
            run_id         TEXT PRIMARY KEY,
            client_id      TEXT NOT NULL,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            status         TEXT NOT NULL,
            orders_total   INTEGER DEFAULT 0,
            orders_succeeded INTEGER DEFAULT 0,
            orders_failed   INTEGER DEFAULT 0,
            orders_skipped  INTEGER DEFAULT 0,
            dry_run        INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()


def cmd_inspect() -> None:
    """Show aggregate statistics from the shared Turso database."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        print("\n=== Turso Production Database Inspection ===\n")

        # Total records by table
        for table in ("succeeded_orders", "retry_queue", "failed_ledger", "run_history"):
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            count = row[0] if row else 0
            print(f"  {table}: {count} record(s)")

        # Record counts by client
        print("\n  Records by client (succeeded_orders):")
        rows = conn.execute(
            "SELECT client_id, COUNT(*) FROM succeeded_orders GROUP BY client_id ORDER BY client_id"
        ).fetchall()
        if not rows:
            print("    (none)")
        for client_id, count in rows:
            print(f"    {client_id}: {count}")

        # Oldest and newest succeeded orders
        print("\n  Succeeded orders date range:")
        row = conn.execute(
            "SELECT MIN(succeeded_at), MAX(succeeded_at) FROM succeeded_orders"
        ).fetchone()
        if row and row[0]:
            print(f"    Oldest: {row[0]}")
            print(f"    Newest: {row[1]}")
        else:
            print("    (none)")

        # Retry queue size
        row = conn.execute("SELECT COUNT(*) FROM retry_queue").fetchone()
        print(f"\n  Retry queue: {row[0] if row else 0} active entry(ies)")

        # Failed order count
        row = conn.execute("SELECT COUNT(*) FROM failed_ledger").fetchone()
        print(f"  Failed orders: {row[0] if row else 0} entry(ies)")

        # Run history size
        row = conn.execute("SELECT COUNT(*) FROM run_history").fetchone()
        print(f"  Run history: {row[0] if row else 0} entry(ies)")

        print("\n=== End Inspection ===\n")
    finally:
        conn.close()


def cmd_retention(max_days: int, dry_run: bool = False) -> None:
    """Clean up old run_history and resolved failure details."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).isoformat(timespec="seconds")

        # Count records that would be affected
        old_history = conn.execute(
            "SELECT COUNT(*) FROM run_history WHERE started_at < ?", (cutoff,)
        ).fetchone()[0]

        # Only delete run_history entries older than cutoff
        # Preserve succeeded_orders permanently (dedup history)
        # Preserve active retry_queue entries
        # Only delete failed_ledger entries older than cutoff that are resolved
        old_failures = conn.execute(
            "SELECT COUNT(*) FROM failed_ledger WHERE last_attempt_at < ?", (cutoff,)
        ).fetchone()[0]

        print(f"\n=== Retention Cleanup (max_days={max_days}) ===")
        print(f"Cutoff date: {cutoff}")
        print(f"Run history entries older than cutoff: {old_history}")
        print(f"Failed ledger entries older than cutoff: {old_failures}")
        print(f"Successful orders preserved: YES (never purged by retention)")
        print(f"Active retry entries preserved: YES (never purged by retention)")

        if dry_run:
            print("DRY RUN: No changes made.\n")
            return

        confirm = input(f"\nDelete {old_history} old run_history entries and "
                        f"{old_failures} old failed ledger entries? (yes/NO): ")
        if confirm.lower() != "yes":
            print("Aborted.\n")
            return

        if old_history > 0:
            conn.execute("DELETE FROM run_history WHERE started_at < ?", (cutoff,))
        if old_failures > 0:
            conn.execute("DELETE FROM failed_ledger WHERE last_attempt_at < ?", (cutoff,))
        conn.commit()
        print(f"Deleted {old_history} run_history entries and {old_failures} failed ledger entries.\n")
    finally:
        conn.close()


def cmd_reset_client(client_id: str, confirm: str) -> None:
    """Clear all tracking data for one explicitly selected client."""
    if confirm != "RESET_CLIENT":
        raise SystemExit(
            "Client reset requires --confirm RESET_CLIENT. "
            "Refusing to proceed without explicit confirmation."
        )

    conn = _connect()
    try:
        _ensure_schema(conn)

        # Count records that would be affected
        succeeded = conn.execute(
            "SELECT COUNT(*) FROM succeeded_orders WHERE client_id = ?", (client_id,)
        ).fetchone()[0]
        retries = conn.execute(
            "SELECT COUNT(*) FROM retry_queue WHERE client_id = ?", (client_id,)
        ).fetchone()[0]
        failures = conn.execute(
            "SELECT COUNT(*) FROM failed_ledger WHERE client_id = ?", (client_id,)
        ).fetchone()[0]
        history = conn.execute(
            "SELECT COUNT(*) FROM run_history WHERE client_id = ?", (client_id,)
        ).fetchone()[0]

        print(f"\n=== Client Reset: {client_id} ===")
        print(f"WARNING: This will DELETE all tracking data for this client.")
        print(f"  Succeeded orders to delete: {succeeded}")
        print(f"  Retry queue entries to delete: {retries}")
        print(f"  Failed ledger entries to delete: {failures}")
        print(f"  Run history entries to delete: {history}")
        print(f"  Clearing succeeded orders CAN cause old orders to be processed again.")
        print()

        conn.execute("DELETE FROM succeeded_orders WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM retry_queue WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM failed_ledger WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM run_history WHERE client_id = ?", (client_id,))
        conn.commit()

        print(f"Reset complete for client {client_id}:")
        print(f"  Deleted {succeeded} succeeded order(s)")
        print(f"  Deleted {retries} retry queue entry(ies)")
        print(f"  Deleted {failures} failed ledger entry(ies)")
        print(f"  Deleted {history} run history entry(ies)\n")
    finally:
        conn.close()


def cmd_reset_all(confirm: str) -> None:
    """Full database reset. Never runs automatically."""
    if confirm != "RESET_ALL_PRODUCTION_TRACKING":
        raise SystemExit(
            "Full reset requires --confirm RESET_ALL_PRODUCTION_TRACKING. "
            "Refusing to proceed without explicit confirmation phrase."
        )

    conn = _connect()
    try:
        _ensure_schema(conn)

        # Count all records
        succeeded = conn.execute("SELECT COUNT(*) FROM succeeded_orders").fetchone()[0]
        retries = conn.execute("SELECT COUNT(*) FROM retry_queue").fetchone()[0]
        failures = conn.execute("SELECT COUNT(*) FROM failed_ledger").fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM run_history").fetchone()[0]

        print(f"\n=== FULL DATABASE RESET ===")
        print(f"WARNING: This will DELETE ALL tracking data from ALL clients.")
        print(f"  Succeeded orders to delete: {succeeded}")
        print(f"  Retry queue entries to delete: {retries}")
        print(f"  Failed ledger entries to delete: {failures}")
        print(f"  Run history entries to delete: {history}")
        print(f"  Schema version table preserved.")
        print()

        second = input("Type RESET_ALL_PRODUCTION_TRACKING again to confirm: ")
        if second != "RESET_ALL_PRODUCTION_TRACKING":
            print("Aborted.\n")
            return

        conn.execute("DELETE FROM succeeded_orders")
        conn.execute("DELETE FROM retry_queue")
        conn.execute("DELETE FROM failed_ledger")
        conn.execute("DELETE FROM run_history")
        conn.commit()

        print(f"Full reset complete:")
        print(f"  Deleted {succeeded} succeeded order(s)")
        print(f"  Deleted {retries} retry queue entry(ies)")
        print(f"  Deleted {failures} failed ledger entry(ies)")
        print(f"  Deleted {history} run history entry(ies)\n")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Database maintenance for the shared Turso production tracker."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect", help="Show aggregate database statistics.")

    p_ret = sub.add_parser("retention", help="Clean up old run_history and failure details.")
    p_ret.add_argument("--max-days", type=int, default=90, help="Maximum age in days (default: 90).")
    p_ret.add_argument("--dry-run", action="store_true", help="Report only, no changes.")

    p_reset_client = sub.add_parser("reset-client", help="Clear all tracking for one client.")
    p_reset_client.add_argument("--client", required=True, help="Client ID to reset.")
    p_reset_client.add_argument("--confirm", default="", help="Confirmation phrase: RESET_CLIENT")

    p_reset_all = sub.add_parser("reset-all", help="Full database reset (all clients).")
    p_reset_all.add_argument("--confirm", default="", help="Confirmation: RESET_ALL_PRODUCTION_TRACKING")

    args = parser.parse_args()

    if args.command == "inspect":
        cmd_inspect()
    elif args.command == "retention":
        cmd_retention(max_days=args.max_days, dry_run=args.dry_run)
    elif args.command == "reset-client":
        cmd_reset_client(client_id=args.client, confirm=args.confirm)
    elif args.command == "reset-all":
        cmd_reset_all(confirm=args.confirm)


if __name__ == "__main__":
    main()