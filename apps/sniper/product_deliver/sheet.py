"""The customer sheet: status rule, names/prices/phones loaders."""

from __future__ import annotations

import csv
import re
from apps.sniper.product_deliver.store import (
    phone_keys,
)
from pathlib import (
    Path,
)
from apps.sniper.product_deliver.common import (
    load_json,
    now_iso,
)
from apps.sniper.product_deliver.identity import (
    clean_name,
)



PRICE_HEADERS = ("price", "total amount", "amount", "total", "order amount",
                 "amount paid", "total amount paid", "product amount",
                 "selling price")



def _norm_header(text_value) -> str:
    """'Price (₦)' / ' PRICE (NGN) ' / 'Price' -> 'price': lower-case, any
    '(...)' currency/unit suffix dropped, whitespace collapsed."""
    text_value = re.sub(r"\(.*?\)", " ", str(text_value or ""))
    return re.sub(r"\s+", " ", text_value).strip().lower()



def parse_price(raw):
    """'₦28,500' / 28500.0 / '28500' -> 28500 (int when whole), else None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        digits = re.sub(r"[^\d.]", "", str(raw).replace(",", ""))
        if not digits:
            return None
        try:
            value = float(digits)
        except ValueError:
            return None
    if value <= 0:
        return None
    return int(value) if value == int(value) else round(value, 2)



def _cell_text(row: list, idx: int | None) -> str:
    """Row cell as trimmed text ('' for short rows / None cells)."""
    if idx is None or idx >= len(row):
        return ""
    value = row[idx]
    return str(value).strip() if value is not None else ""



# Last _select_work_rows() summary; printed once by main() so the sheet
# driven selection is always visible in the log.
_selection_note = ""



def _select_work_rows(body_rows: list[list], name_idx: int,
                      status_idx: int | None,
                      key_idx: int | None = None) -> tuple[list[list], str]:
    """The sheet status rule, shared by EVERY loader (names, prices,
    phones) so all three always see the exact same rows.

    A real data row is identified by the filtered s/n column ("Filtered #"
    / "S/N") when the sheet has one -- else by the name column; other
    empty cells never disqualify a row. "Blank" is judged ONLY on the
    status column of real rows:
    * no status column, or every real status blank -> every real row is
      work (the whole file is ready to be moved to delivered);
    * ANY status marked -> the marked column is the tracker: ONLY rows
      whose status starts with "delivered" are work -- those are the
      rows the sheet says to move. Pending / Scheduled / Cancelled rows
      and blank-status rows are ignored even when they are the majority,
      and a marked column with zero Delivered rows means NOTHING to
      process (no silent fall back to "everything").
    Returns (work_rows, human-readable summary).
    """
    global _selection_note
    id_idx = key_idx if key_idx is not None else name_idx
    real = [row for row in body_rows if _cell_text(row, id_idx)]
    if status_idx is None:
        note = "no status column -> all names are work"
        _selection_note = note
        return real, note
    counts: dict[str, int] = {}
    blank = 0
    for row in real:
        value = _cell_text(row, status_idx).lower()
        if value:
            counts[value] = counts.get(value, 0) + 1
        else:
            blank += 1
    if not sum(counts.values()):
        note = (f"status column entirely blank ({blank} row(s)) "
                "-> all names are work")
        _selection_note = note
        return real, note
    work_rows = [row for row in real
                 if _cell_text(row, status_idx).lower()
                 .startswith("delivered")]
    others = sorted(((value, num) for value, num in counts.items()
                     if not value.startswith("delivered")),
                    key=lambda item: -item[1])
    note = (f"{len(work_rows)} delivered, "
            f"{sum(counts.values()) - len(work_rows)} other"
            + (f" ({', '.join(f'{num} {value}' for value, num in others)})"
               if others else "")
            + f", {blank} blank -> processing {len(work_rows)}")
    if not work_rows:
        note += " -- NOTHING to process (no row marked Delivered)"
    _selection_note = note
    return work_rows, note



# A Price cell holding a phone number parses to billions of naira (live
# example: sheet cell 7049528233 = the customer's 07049528233). Anything
# past this cap is data-entry noise, not a price, and is ignored so the
# CRM row Amount is used instead of being saved as the order Total.
MAX_PLAUSIBLE_PRICE = 5_000_000



def load_customer_prices(path: Path, price_column: str = ""):
    """Excel/CSV price per customer, if the file has a price column.
    Returns (prices {clean_name: price}, column_header_used, conflicts).
    Rows follow the shared _select_work_rows() status rule (only rows
    marked Delivered when the sheet marks any status; every real row
    when the status column is blank/absent; real rows keyed by the
    filtered s/n column when present); a name that appears with
    different prices is dropped into `conflicts` (the CRM row Amount is
    used for it instead). A price above MAX_PLAUSIBLE_PRICE (a phone
    number typed into the Price cell) is ignored for the same reason."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    elif suffix == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.reader(fh)]
    else:
        return {}, "", set()
    if not rows:
        return {}, "", set()
    header = [str(c).strip().lower() if c is not None else ""
              for c in rows[0]]
    header_norm = [_norm_header(c) for c in rows[0]]

    def _find(*wanted: str):
        for want in wanted:                       # exact header first...
            for i, h in enumerate(header):
                if h == want:
                    return i
        for want in wanted:                       # ...then ignoring "(₦)" etc.
            for i, h in enumerate(header_norm):
                if h == _norm_header(want):
                    return i
        return None

    name_idx = _find("customer name")
    if name_idx is None:
        name_idx = 0
    key_idx = _find("filtered #", "filtered#", "filtered", "s/n", "sn")
    price_idx = None
    if price_column:
        price_idx = _find(price_column.strip().lower())
    else:
        price_idx = _find(*PRICE_HEADERS)
        if price_idx is None:
            for i, h in enumerate(header):
                if "amount" in h or "price" in h:
                    price_idx = i
                    break
    if price_idx is None:
        return {}, "", set()
    status_idx = _find("action", "order status", "status")
    body, _ = _select_work_rows(rows[1:], name_idx, status_idx, key_idx)
    prices: dict = {}
    conflicts: set = set()
    for row in body:
        if name_idx >= len(row) or price_idx >= len(row):
            continue
        name = clean_name(str(row[name_idx]) if row[name_idx] is not None
                          else "")
        price = parse_price(row[price_idx])
        if not name or price is None:
            continue
        if price > MAX_PLAUSIBLE_PRICE:   # phone number in the Price cell
            continue
        if name in prices and prices[name] != price:
            conflicts.add(name)
        prices.setdefault(name, price)
    for name in conflicts:
        prices.pop(name, None)
    return prices, str(rows[0][price_idx]).strip(), conflicts



PHONE_HEADERS = ("phone number", "phone", "whatsapp number", "whatsapp",
                 "customer phone", "contact", "mobile")



def load_customer_phones(path: Path):
    """{clean_name(customer): [phone, ...]} from the sheet's phone columns.

    The CRM's Contact cell shows the customer's phone (`tel:`) AND their
    WhatsApp (`wa.me`) number, and those can differ, so every phone-ish
    column ('Phone Number', 'WhatsApp Number', ...) is captured and values
    are normalised with the store's phone_keys(). Same row rule as
    load_customers()/load_customer_prices() via _select_work_rows():
    when the sheet marks any status, only Delivered rows are used; when
    the status column is blank/absent, every real row (keyed by the
    filtered s/n column when present) is used. Returns
    (phones, headers_used) and
    never raises for an unreadable/odd file (callers treat {} as 'no phone
    identity available', which is exactly the old behaviour).
    """
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    elif suffix == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.reader(fh)]
    else:
        return {}, ()
    if not rows:
        return {}, ()

    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]

    def _find(*wanted: str):
        for want in wanted:
            for i, h in enumerate(header):
                if h == want:
                    return i
        return None

    name_idx = _find("customer name")
    if name_idx is None:
        name_idx = 0
    key_idx = _find("filtered #", "filtered#", "filtered", "s/n", "sn")
    phone_cols = [i for i, h in enumerate(header)
                  if "phone" in h or "whatsapp" in h]
    if not phone_cols:
        idx = _find(*PHONE_HEADERS)
        phone_cols = [idx] if idx is not None else []
    if not phone_cols:
        return {}, ()

    status_idx = _find("action", "order status", "status")
    body, _ = _select_work_rows(rows[1:], name_idx, status_idx, key_idx)
    phones: dict = {}
    for row in body:
        if name_idx >= len(row):
            continue
        name = clean_name(str(row[name_idx]) if row[name_idx] is not None
                          else "")
        if not name:
            continue
        found = list(phones.get(name, []))
        for i in phone_cols:
            if i >= len(row):
                continue
            for key in phone_keys(row[i]):
                if key not in found:
                    found.append(key)
        if found:
            phones[name] = found
    used = tuple(str(rows[0][i]).strip() for i in phone_cols)
    return phones, used



NOT_FOUND_COLUMNS = ("exportedAt", "client", "customer", "excelPrice",
                     "whatWasSeen", "sourceFile", "runId")



def export_not_found(path: Path, client: str, customer: str, detail: str,
                     price, source: str, run_id: str) -> bool:
    """Append a customer that is in neither Pending nor Delivered to a CSV
    so a human can find out why (name not uploaded / misspelt). One row per
    name: a name already listed is not added again. True if a row was
    written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = set()
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                listed.add(clean_name(row.get("customer", "")))
    if clean_name(customer) in listed:
        return False
    is_new = not path.exists()
    with open(path, "a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(NOT_FOUND_COLUMNS)
        writer.writerow([now_iso(), client, customer,
                         "" if price is None else price, detail, source,
                         run_id])
    return True




# ============================ config / customers ============================


def load_customers(path: Path, only_pending: bool = True) -> list[str]:
    """Customer names from .txt (one per line) / .csv / .json / .xlsx.

    Header-aware for xlsx/csv (the sniper export layout): names come from
    the 'Customer Name' column (else the first column). The status rule
    lives in _select_work_rows(): a real data row is keyed by the
    filtered s/n column ("Filtered #" / "S/N") when present, else the
    name column. With a status column ('Action' / 'Order Status' /
    'Status') that marks ANY row, only rows marked Delivered are
    returned -- Pending / Scheduled / Cancelled and blank rows are
    ignored; if the status column is entirely blank (or there is none),
    every real row is returned. only_pending=False always returns every
    real row.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = load_json(path, [])
        if isinstance(data, dict):
            data = data.get("customers", [])
        return [str(c).strip() for c in data if str(c).strip()]

    if suffix in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    elif suffix == ".csv":
        import csv
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.reader(fh)]
        width = max((len(r) for r in rows), default=0)
        rows = [r + [""] * (width - len(r)) for r in rows]
    else:  # .txt
        names = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name = line.split(",")[0].strip()
            if name:
                names.append(name)
        return names

    if not rows:
        return []

    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]

    def _col(*names_: str) -> int | None:
        for want in names_:
            for i, h in enumerate(header):
                if h == want:
                    return i
        return None

    name_idx = _col("customer name")
    if name_idx is None:
        name_idx = 0
        status_idx = None
        key_idx = None
    else:
        status_idx = _col("action", "order status", "status")
        key_idx = _col("filtered #", "filtered#", "filtered", "s/n", "sn")

    if not only_pending:                    # explicit "everything" mode
        names = []
        for row in rows[1:]:
            name = _cell_text(row, name_idx)
            if name:
                names.append(name)
        return names
    work, _ = _select_work_rows(rows[1:], name_idx, status_idx, key_idx)
    return [_cell_text(row, name_idx) for row in work]



def load_config(path: Path | None) -> dict:
    if path is None:
        return {}
    return load_json(path, {}) or {}
