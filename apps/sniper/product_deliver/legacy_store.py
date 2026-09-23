"""Legacy JSON file store (kept for the test suite)."""

from __future__ import annotations

import json
import re
from apps.sniper.product_deliver.store import (
    phone_keys,
    phone_matches,
)
from pathlib import (
    Path,
)
from apps.sniper.product_deliver.common import (
    atomic_write_json,
    fingerprint,
    load_json,
    now_iso,
)
from apps.sniper.product_deliver.constants import (
    DISCOVERED,
    FAILED_FINAL,
    FAILED_RETRYABLE,
    SUCCESS,
    TARGET_STATUS,
)



class OrderRecord:
    """One order's persistent state-machine row (doc §3)."""

    __slots__ = ("data",)

    def __init__(self, data: dict):
        self.data = data

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




# ============================ persistence ===================================


class DeliverStore:
    """File-backed state for one deliver job (doc §3/§23/§26)."""

    def __init__(self, client_id: str, period: str, root: Path):
        self.client_id = client_id
        self.period = period
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = root / "state.json"
        self.processed_path = root / "processed.json"
        self.report_path = root / "report.json"
        self.lock_path = root / "locks" / "deliver.lock"
        self.screens_dir = root / "screens"
        self.state = load_json(self.state_path, {"orders": {}})
        self.processed = load_json(self.processed_path, {})
        self.shutdown = False

    def record(self, customer: str, order_id: str = "",
               phone="") -> OrderRecord:
        key = fingerprint(self.client_id, self.period,
                          order_id or f"name:{customer}")
        orders = self.state.setdefault("orders", {})
        if key not in orders:
            orders[key] = {
                "key": key,
                "customer": customer,
                "orderId": order_id,
                "state": DISCOVERED,
                "attempt": 0,
                "createdAt": now_iso(),
            }
        rec = OrderRecord(orders[key])
        if order_id and rec.order_id != order_id:
            rec.data["orderId"] = order_id
        wanted = " ".join(phone_keys(phone))
        if wanted and rec.data.get("phone") != wanted:
            rec.data["phone"] = wanted
        return rec

    def checkpoint(self) -> None:
        atomic_write_json(self.state_path, self.state)

    def is_done(self, customer: str, phones=None) -> bool:
        """True when this customer was already delivered.

        `phones` (the sheet's Phone/WhatsApp value) keeps two DIFFERENT
        people who share a name apart: a success stored against another
        number does not count, so the second customer is still processed. A
        legacy entry with no stored phone still counts (nothing to compare).
        """
        wanted = phone_keys(phones)
        for entry in self.processed.values():
            if (entry.get("customer") != customer
                    or entry.get("status") != TARGET_STATUS):
                continue
            stored = phone_keys(entry.get("phone"))
            if not wanted or not stored or phone_matches(stored, wanted):
                return True
        return False

    def mark_success(self, rec: OrderRecord, order_id: str,
                     phone="") -> None:
        rec.transition(SUCCESS, lastStep="verify")
        key = fingerprint(self.client_id, self.period, order_id)
        entry = {
            "key": key,
            "orderId": order_id,
            "customer": rec.data["customer"],
            "status": TARGET_STATUS,
            "completedAt": now_iso(),
        }
        if phone_keys(phone):
            entry["phone"] = " ".join(phone_keys(phone))
        self.processed[key] = entry
        rec.data["orderId"] = order_id
        self.checkpoint()
        atomic_write_json(self.processed_path, self.processed)

    def mark_failed(self, rec: OrderRecord, step: str, reason: str,
                    final: bool) -> None:
        rec.transition(FAILED_FINAL if final else FAILED_RETRYABLE,
                       lastStep=step, reason=reason)
        self.checkpoint()

    def evidence(self, page, rec: OrderRecord, step: str, reason: str) -> None:
        """doc §25: screenshot + DOM + structured event for failures."""
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
        try:
            with open(self.root / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": now_iso(),
                    "customer": rec.data.get("customer"),
                    "orderId": rec.order_id,
                    "state": rec.state,
                    "attempt": rec.data.get("attempt", 0),
                    "step": step,
                    "reason": reason,
                }) + "\n")
        except Exception:
            pass
