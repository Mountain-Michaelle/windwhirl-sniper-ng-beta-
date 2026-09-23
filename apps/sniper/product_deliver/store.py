"""SQLite-backed per-client deliver progress store.

One database per client at <client>/state/deliver_progress.db:
  deliver_orders  -- one row per customer/order with full state machine
  deliver_events  -- append-only event log (failures, evidence)

Same method surface as run_deliver.DeliverStore (JSON), so the driver
works with either.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SUCCESS = "SUCCESS"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
FAILED_FINAL = "FAILED_FINAL"
DISCOVERED = "DISCOVERED"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(client: str, period: str, order_id: str) -> str:
    """Identity is client|period|orderId (order id when known, else a
    name-prefixed placeholder until the row reveals the real id)."""
    return f"{client}|{period}|{order_id}"


def normalize_phone(raw) -> str:
    """Excel/CRM phone -> comparable digits.

    '+234 808 250 8065' / '2348082508065' / '08082508065' all become
    '2348082508065' (local 0-prefixed numbers take their 234 form). Anything
    that is not a recognisable NG-style number is returned digits-only, and
    an empty/unusable value returns ''. Used for CUSTOMER IDENTITY, so two
    people who share a name are not treated as one customer.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    if digits.startswith("2340") and len(digits) == 14:   # 234 + 0 + 10
        digits = "234" + digits[4:]
    if digits.startswith("234") and len(digits) >= 13:
        return digits[:13]
    if digits.startswith("0") and len(digits) == 11:
        return "234" + digits[1:]
    if len(digits) == 10:
        return "234" + digits
    return digits


def phone_keys(value) -> list:
    """Coerce a phone / list of phones / space-or-comma separated string of
    phones into comparable digits-only keys (order preserved, no dupes)."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = re.split(r"[\s,;/]+", str(value))
    keys: list = []
    for item in items:
        key = normalize_phone(item)
        if key and key not in keys:
            keys.append(key)
    return keys


def phone_matches(a, b) -> bool:
    """True when two phone values are the same subscriber. Compares the
    canonical forms, then the last 10 digits, so '+234...' vs a local
    '0...' vs a CRM value that lost its country code still match."""
    keys_a, keys_b = phone_keys(a), phone_keys(b)
    for one in keys_a:
        for two in keys_b:
            if one == two or one[-10:] == two[-10:]:
                return True
    return False


class OrderRecord:
    """Dict-style view over one deliver_orders row; transition() writes
    through to SQLite immediately (no lost state on crash)."""

    __slots__ = ("store", "data")

    def __init__(self, store: "SqliteDeliverStore", data: dict):
        self.store = store
        self.data = data

    @property
    def key(self) -> str:
        return self.data["key"]

    @property
    def order_id(self) -> str:
        return self.data.get("orderId") or ""

    @property
    def state(self) -> str:
        return self.data.get("state", DISCOVERED)

    def transition(self, state: str, **extra) -> None:
        self.data["state"] = state
        self.data["updatedAt"] = now_iso()
        self.data.update(extra)
        if state in (FAILED_RETRYABLE, FAILED_FINAL):
            self.data["attempt"] = int(self.data.get("attempt", 0)) + 1
        self.store._write_row(self.data)


class SqliteDeliverStore:
    def __init__(self, client_id: str, period: str, root: Path):
        self.client_id = client_id
        self.period = period
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "deliver_progress.db"
        self.report_path = self.root / "deliver_report.json"
        self.screens_dir = self.root / "deliver_screens"
        self.lock_path = self.root / "locks" / "deliver.lock"
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS deliver_orders (
                key TEXT PRIMARY KEY,
                client TEXT, period TEXT,
                customer TEXT NOT NULL,
                order_id TEXT DEFAULT '',
                state TEXT DEFAULT 'DISCOVERED',
                attempt INTEGER DEFAULT 0,
                last_step TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                agent TEXT DEFAULT '',
                package TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                created_at TEXT, updated_at TEXT)""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deliver_customer"
            " ON deliver_orders(customer, period)")
        # Migrate databases created before phone identity: the column holds
        # the customer's Contact number(s), so two DIFFERENT people who share
        # a name are not mistaken for one customer.
        columns = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(deliver_orders)")}
        if "phone" not in columns:
            self.conn.execute(
                "ALTER TABLE deliver_orders ADD COLUMN phone TEXT DEFAULT ''")
        self.conn.commit()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS deliver_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, customer TEXT, order_id TEXT,
                state TEXT, attempt INTEGER, step TEXT, reason TEXT)""")
        self.conn.commit()
        self.shutdown = False

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        return {
            "key": d["key"], "customer": d["customer"],
            "orderId": d["order_id"] or "", "state": d["state"],
            "attempt": d["attempt"], "lastStep": d["last_step"],
            "reason": d["reason"], "agent": d["agent"],
            "package": d["package"], "phone": d.get("phone") or "",
            "createdAt": d["created_at"], "updatedAt": d["updated_at"],
        }

    def _write_row(self, data: dict) -> None:
        self.conn.execute(
            """UPDATE deliver_orders SET order_id=?, state=?, attempt=?,
               last_step=?, reason=?, agent=?, package=?, phone=?,
               updated_at=? WHERE key=?""",
            (data.get("orderId", ""), data.get("state", DISCOVERED),
             int(data.get("attempt", 0)), data.get("lastStep", ""),
             data.get("reason", ""), data.get("agent", ""),
             data.get("package", ""), data.get("phone", "") or "",
             data.get("updatedAt", now_iso()), data["key"]))
        self.conn.commit()


    def _event(self, customer: str, order_id: str, state: str,
               attempt: int, step: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO deliver_events (ts, customer, order_id, state,"
            " attempt, step, reason) VALUES (?,?,?,?,?,?,?)",
            (now_iso(), customer, order_id, state, attempt, step, reason))
        self.conn.commit()

    # -- driver-facing surface (same as run_deliver.DeliverStore) -----------
    def record(self, customer: str, order_id: str = "",
               phone="") -> OrderRecord:
        row = self.conn.execute(
            "SELECT * FROM deliver_orders WHERE customer=? AND period=?"
            " ORDER BY updated_at DESC LIMIT 1",
            (customer, self.period)).fetchone()
        wanted_phone = " ".join(phone_keys(phone))
        if row is None:
            key = fingerprint(self.client_id, self.period,
                              order_id or f"name:{customer}")
            self.conn.execute(
                "INSERT INTO deliver_orders (key, client, period, customer,"
                " order_id, state, attempt, phone, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, self.client_id, self.period, customer, order_id,
                 DISCOVERED, 0, wanted_phone, now_iso(), now_iso()))
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM deliver_orders WHERE key=?", (key,)).fetchone()
        data = self._row_to_dict(row)
        changed = False
        if order_id and data["orderId"] != order_id:
            data["orderId"] = order_id
            changed = True
        if wanted_phone and data.get("phone") != wanted_phone:
            data["phone"] = wanted_phone
            changed = True
        if changed:
            data["updatedAt"] = now_iso()
            self._write_row(data)
        return OrderRecord(self, data)

    def row(self, customer: str):
        row = self.conn.execute(
            "SELECT * FROM deliver_orders WHERE customer=? AND period=?"
            " ORDER BY updated_at DESC LIMIT 1",
            (customer, self.period)).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def checkpoint(self) -> None:
        pass  # SQLite writes through immediately; nothing to flush.

    def is_done(self, customer: str, phones=None) -> bool:
        """True when this customer already has a SUCCESS row.

        `phones` (the sheet's Phone/WhatsApp value) separates two DIFFERENT
        people who share a name: a success stored against a different number
        does not count, so the second customer is still processed instead of
        being skipped as 'already delivered'. A legacy row with no stored
        phone still counts -- there is nothing to compare it against.
        """
        rows = self.conn.execute(
            "SELECT phone FROM deliver_orders WHERE customer=? AND period=?"
            " AND state=?", (customer, self.period, SUCCESS)).fetchall()
        wanted = phone_keys(phones)
        for row in rows:
            stored = phone_keys(row["phone"])
            if not wanted or not stored or phone_matches(stored, wanted):
                return True
        return False

    def mark_success(self, rec: OrderRecord, order_id: str,
                     phone="") -> None:
        extra = {"orderId": order_id}
        if phone_keys(phone):
            extra["phone"] = " ".join(phone_keys(phone))
        rec.transition(SUCCESS, lastStep="verify", reason="", **extra)
        self._event(rec.data["customer"], order_id, SUCCESS,
                    int(rec.data.get("attempt", 0)), "verify", "confirmed")

    def mark_failed(self, rec: OrderRecord, step: str, reason: str,
                    final: bool) -> None:
        rec.transition(FAILED_FINAL if final else FAILED_RETRYABLE,
                       lastStep=step, reason=reason)
        self._event(rec.data["customer"], rec.order_id, rec.state,
                    int(rec.data.get("attempt", 0)), step, reason)

    def evidence(self, page, rec, step: str, reason: str) -> None:
        """Screenshot + DOM + sqlite event (doc section 25)."""
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                      f"{rec.data.get('customer', 'x')}_{step}")[:80]
        try:
            self.screens_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(self.screens_dir / f"{slug}.png"),
                            full_page=True)
            (self.screens_dir / f"{slug}.html").write_text(
                page.content(), encoding="utf-8")
        except Exception:
            pass
        self._event(rec.data.get("customer", "?"), rec.order_id, rec.state,
                    int(rec.data.get("attempt", 0)), step, reason)

    def counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM deliver_orders"
            " WHERE period=? GROUP BY state", (self.period,)).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

