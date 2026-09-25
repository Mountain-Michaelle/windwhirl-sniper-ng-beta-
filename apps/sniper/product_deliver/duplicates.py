"""Duplicate-upload detection: order fingerprints + the per-client ledger.

The CRM lets one order be uploaded twice: two rows, same customer, same
Contact number(s), same Amount, DIFFERENT Order IDs. This module gives
that situation a name (a fingerprint) and remembers, per client, which
copies were already handled -- so a leftover copy is never delivered a
second time on a later run, even when nobody deleted it on the CRM.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from apps.sniper.product_deliver.common import (
    atomic_write_json,
    load_json,
    now_iso,
)
from apps.sniper.product_deliver.identity import (
    clean_name,
)
from apps.sniper.product_deliver.store import (
    phone_keys,
)


def customer_fingerprint(name, phones) -> str:
    """SCAN matching rule: name + Contact number(s) ONLY.

    The whole-pending-list scan deliberately ignores the Amount: the same
    customer + number appearing twice is already a duplicate upload, and
    two rows of one order can carry a different Amount
    """
    wanted = sorted(phone_keys(phones))
    return "|".join((clean_name(name or ""), ",".join(wanted)))


def order_fingerprint(name, phones, amount) -> str:
    """DELIVER-flow matching rule: name + Contact number(s) + Amount.

    Here the Amount stays in: while delivering one customer we only call
    two rows the same order when the money matches too (otherwise the
    customer may genuinely have two different orders).
    """
    wanted = sorted(phone_keys(phones))
    digits = re.sub(r"[^\d]", "", str(amount)) if amount not in (None, "") \
        else ""
    money = str(int(digits)) if digits else "0"
    return "|".join((clean_name(name or ""), ",".join(wanted), money))


def customer_key(row) -> str:
    """groups_from_rows() key for the scan (name + phones)."""
    return customer_fingerprint(row.get("customer"), row.get("phones"))


def order_key(row) -> str:
    """groups_from_rows() key for the deliver flow (name + phones + amount)."""
    return order_fingerprint(row.get("customer"), row.get("phones"),
                             row.get("amount"))


def groups_from_rows(rows, key_fn) -> list:
    """Group table rows into duplicate groups: [(fingerprint, [rows...])].

    Only groups with 2+ copies are returned (that IS the definition of a
    duplicate upload), biggest group first. `key_fn` is `customer_key` for
    the scan and `order_key` for the deliver flow, so both flows share
    this one grouper. Rows without a usable key are skipped.
    """
    buckets: dict = {}
    for row in rows or []:
        try:
            key = key_fn(row)
        except Exception:                             # noqa: BLE001
            continue
        if not key or key.startswith("|"):
            continue                  # no name -> nothing to fingerprint on
        buckets.setdefault(key, []).append(row)
    groups = [(key, items) for key, items in buckets.items()
              if len(items) > 1]
    groups.sort(key=lambda item: (-len(item[1]), item[0]))
    return groups


DUPLICATE_COLUMNS = ("exportedAt", "client", "customer", "phones", "orderId",
                     "amount", "status", "copies", "orderIds", "match",
                     "sourceFile", "runId", "fingerprint")


def export_duplicate(path: Path, copies, *, client: str, source: str = "",
                     run_id: str = "", match: str = "name+phones",
                     fingerprint: str = "") -> int:
    """Append the not-yet-listed copies of ONE duplicate group to a CSV.

    One row per COPY (Order ID) so a human can see and delete each one;
    the shared `fingerprint` column groups them. A copy already present in
    the file (same fingerprint + Order ID) is never listed twice, so
    re-scanning the same CRM does not grow the file. Returns how many rows
    were written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = set()
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                listed.add((row.get("fingerprint", ""),
                            str(row.get("orderId", ""))))
    order_ids = [str(c.get("orderId") or "") for c in copies or []]
    pending = [c for c in (copies or [])
               if (fingerprint, str(c.get("orderId") or "")) not in listed]
    if not pending:
        return 0
    is_new = not path.exists()
    written = 0
    with open(path, "a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(DUPLICATE_COLUMNS)
        for copy in pending:
            writer.writerow([
                now_iso(), client, copy.get("customer", ""),
                ", ".join(phone_keys(copy.get("phones"))),
                copy.get("orderId", ""), copy.get("amount", ""),
                copy.get("status", ""), len(order_ids),
                ", ".join(order_ids), match, source, run_id, fingerprint])
            written += 1
    return written


class DuplicateLedger:
    """<client>/state/deliver/duplicates.json: what duplicate uploads were
    seen, which copies they were (Order IDs) and which one was handled.
    """

    def __init__(self, path: Path | None = None, data=None):
        self.path = path
        self.data = data if isinstance(data, dict) else {}

    @classmethod
    def load(cls, path: Path) -> "DuplicateLedger":
        return cls(path, load_json(path, {}) or {})

    def entry(self, fingerprint: str) -> dict:
        value = self.data.get(fingerprint)
        return value if isinstance(value, dict) else {}

    def known_ids(self, fingerprint: str) -> set:
        """Every Order ID already seen for this order fingerprint."""
        return {str(o) for o in self.entry(fingerprint).get("orderIds") or []
                if o}

    def handled(self, fingerprint: str) -> str:
        """The Order ID that was already delivered for this fingerprint."""
        return str(self.entry(fingerprint).get("handled") or "")

    def remember(self, fingerprint: str, *, name: str, amount,
                 phones, order_ids, handled_id: str = "",
                 action: str = "") -> dict:
        """Record every copy seen for this order and, when one was
        delivered, which copy that was. Persisted immediately."""
        entry = dict(self.entry(fingerprint))
        ids = [str(o) for o in entry.get("orderIds") or []]
        for order_id in order_ids:
            order_id = str(order_id or "")
            if order_id and order_id not in ids:
                ids.append(order_id)
        entry.update({
            "fingerprint": fingerprint,
            "customer": name,
            "amount": amount,
            "phones": sorted(phone_keys(phones)),
            "orderIds": ids,
            "copies": len(ids),
            "lastSeenAt": now_iso(),
        })
        if handled_id:
            entry["handled"] = str(handled_id)
            entry["handledAt"] = now_iso()
            entry["action"] = action or "delivered"
        self.data[fingerprint] = entry
        self.save()
        return entry

    def save(self) -> None:
        if self.path is not None:
            atomic_write_json(self.path, self.data)
