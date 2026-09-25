"""Read-only duplicate scan of the LIVE Pending list (`--scan-duplicate`).

No Excel look-up at all: it filters the CRM the same way the deliver flow
does, then walks EVERY page with the Prev/Next pager at the default 50
rows per page -- so pagination cannot hide a single order -- and
groups the rows by **name + Contact number(s)** -- the matching rule for
this scan. Any group with 2+ copies is a duplicate upload: it is flagged
and exported to the client's own CSV, and its fingerprint is saved in the
client's duplicates.json so later runs know those copies.

Nothing is ever written on the CRM here -- no menu, no modal, no Save.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import (
    Error as PWError,
    sync_playwright,
)

from apps.sniper.client_workspace import (
    JobLock,
    WorkspaceManager,
)
from apps.sniper.product_deliver.common import (
    atomic_write_json,
    now_iso,
)
from apps.sniper.product_deliver.constants import (
    DEFAULT_PERIOD,
    LOGIN_NETWORK_WAIT_MS,
)
from apps.sniper.product_deliver.crm import (
    _confirm_orders_page,
    _ensure_viewport,
)
from apps.sniper.product_deliver.driver import (
    DeliverDriver,
)
from apps.sniper.product_deliver.duplicates import (
    DuplicateLedger,
    customer_key,
    export_duplicate,
    groups_from_rows,
)
from apps.sniper.product_deliver.identity import (
    join_name_parts,
    phones_from_hrefs,
)
from apps.sniper.product_deliver.selectors import (
    AMOUNT_LABELED_RE,
    AMOUNT_RE,
    TABLE_ROW_DATA,
)
from apps.sniper.product_deliver.store import (
    SqliteDeliverStore,
)
from apps.sniper.run_upload import (
    LOGGED_IN_SELECTOR,
    LOGIN_URL,
    TARGET_URL,
    login_with_session_cache,
)


# One round trip for the WHOLE table: raw primitives only. Every identity
# rule (name joining, phone extraction, amount parsing) is applied in
# Python below with the SAME helpers the deliver flow uses, so the two
# readers can never drift apart.
_TABLE_JS = """
(sel) => {
  const out = [];
  for (const tr of document.querySelectorAll(sel)) {
    const tds = tr.querySelectorAll('td');
    if (tds.length < 6) continue;
    const nameCell = tds[5];
    const wrap = nameCell ? nameCell.querySelector('div.font-medium > span:not(.mono)') : null;
    out.push({
      nameParts: nameCell
        ? Array.from(nameCell.querySelectorAll('span.block')).map(s => (s.textContent || '').trim())
        : [],
      wrapText: wrap ? (wrap.innerText || '') : '',
      fallbackName: nameCell ? (nameCell.innerText || '').split('\\n')[0].trim() : '',
      hrefs: tds[6]
        ? Array.from(tds[6].querySelectorAll('a[href]')).map(a => a.getAttribute('href') || '')
        : [],
      orderId: tds[2] ? (tds[2].innerText || '').trim() : '',
      status: tds[9] ? (tds[9].innerText || '').trim().split('\\n')[0].trim() : '',
      product: tds[8] ? (tds[8].innerText || '') : '',
    });
  }
  return out;
}
"""


def read_table_bulk(page) -> list:
    """Every data row of the orders table, in ONE page.evaluate call."""
    try:
        return list(page.evaluate(_TABLE_JS, TABLE_ROW_DATA) or [])
    except PWError:
        return []


def row_count(page) -> int:
    """How many data rows the table is rendering right now (one tiny call)."""
    try:
        return int(page.evaluate(
            "(sel) => document.querySelectorAll(sel).length", TABLE_ROW_DATA)
            or 0)
    except PWError:
        return 0


def wait_for_rows_settled(driver: DeliverDriver, target: int = 0,
                          timeout_s: float = 25.0) -> int:
    """Wait until the table stops re-rendering, then return the row count.

    After a tab switch, Search, or Next click the row count climbs in waves
    as the list loads. Polling until the count stops changing -- rather
    than trusting a single read -- is what keeps a page read from landing
    on a half-rendered table.
    """
    deadline = time.monotonic() + timeout_s
    last = -1
    stable = 0
    while time.monotonic() < deadline:
        count = row_count(driver.page)
        if count == last and (target <= 0 or count >= target):
            stable += 1
            if stable >= 3 or (target and count >= target and stable >= 1):
                return count
        else:
            stable = 0
        last = count
        driver.page.wait_for_timeout(500)
    return max(last, 0)


def enrich_rows(raw_rows) -> list:
    """Raw JS rows -> the same dict shape the deliver flow reads.

    Names go through join_name_parts (the CRM renders one `span.block` per
    name line), phones through phones_from_hrefs (tel: + wa.me), amounts
    through the labeled/first-figure regexes -- identical semantics to
    DeliverDriver.read_row / _name_in_cell / _phones_in_cell.
    """
    rows = []
    for raw in raw_rows or []:
        name = join_name_parts(raw.get("nameParts") or [])
        if not name:
            name = join_name_parts((raw.get("wrapText") or "").split("\n"))
        if not name:
            name = (raw.get("fallbackName") or "").strip()
        phones = phones_from_hrefs(raw.get("hrefs") or [])
        product = " ".join((raw.get("product") or "").split())
        amount = 0
        match = (AMOUNT_LABELED_RE.search(product) or AMOUNT_RE.search(product))
        if match:
            amount = int(match.group(1).replace(",", ""))
        rows.append({
            "customer": name,
            "phones": phones,
            "phone": phones[0] if phones else "",
            "amount": amount,
            "orderId": (raw.get("orderId") or "").strip(),
            "status": (raw.get("status") or "").strip(),
            "product": product[:200],
        })
    return rows


# Reliable Prev/Next pagination (the list is a real paged table, not a
# virtual scroll): read page 1 in place, then click Next until it is
# disabled or the "Page N of N" label stops advancing. Never touches the
# rows-per-page combobox, so the selected page size can never race or
# reset mid-scan. Order-ID dedupe across pages, so a re-rendered page can
# never double-count a row.
_PAGE_LABEL_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)


def _page_label(page) -> str:
    try:
        text = page.get_by_text(_PAGE_LABEL_RE).first.inner_text(timeout=2000)
    except Exception:                                 # noqa: BLE001
        return ""
    match = _PAGE_LABEL_RE.search(text or "")
    return match.group(0) if match else ""


def _page_number(label: str) -> int:
    """1-based page number from a "Page N of M" label (0 when absent)."""
    match = _PAGE_LABEL_RE.search(label or "")
    return int(match.group(1)) if match else 0


def collect_page_rows(driver: DeliverDriver) -> list:
    """Raw rows rendered on the CURRENT page only."""
    try:
        return list(read_table_bulk(driver.page))
    except Exception:                                 # noqa: BLE001
        return []


def _page_first_id(page) -> str:
    """Order id of the first data row -- proves a Next click advanced."""
    try:
        rows = page.evaluate(_TABLE_JS, TABLE_ROW_DATA) or []
    except Exception:                                 # noqa: BLE001
        return ""
    return ((rows[0].get("orderId") or "").strip() if rows else "")


def _page_turned(page, before_label: str, before_first: str) -> bool:
    """True only when BOTH pager signals moved: the "Page N of M" label AND
    the first data row (a different, non-empty order id).

    Why both, not either: a Next click can update the label to "Page 6 of 6"
    while the old page-5 rows are still what the table renders. Accepting
    that state re-reads the old page, dedupes every row away and stops
    short -- exactly how a walk reported "read 250 of 259" (five full pages,
    then a phantom read of a page it never actually turned to).
    """
    label = _page_label(page)
    if before_label and label:
        before_no, now_no = _page_number(before_label), _page_number(label)
        if before_no and now_no:
            if now_no == before_no:
                return False                          # label has not moved
        elif label == before_label:
            return False                              # label has not moved
    first = _page_first_id(page)
    return bool(first) and first != before_first      # rows really changed


def _wait_page_turn(page, before_label: str, before_first: str,
                    timeout_s: float = 20.0) -> bool:
    """True once `_page_turned` proves the turn (bounded poll, no sleep)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _page_turned(page, before_label, before_first):
            return True
        try:
            page.wait_for_timeout(300)
        except Exception:                             # noqa: BLE001
            return False
    return _page_turned(page, before_label, before_first)


def _reset_to_first_page(driver: DeliverDriver, max_pages: int = 40) -> bool:
    """Walk the pager back to page 1 -- proof: the label says 1, Prev is
    disabled, or there is no pager at all (single page).

    A re-walk MUST start here: retrying from wherever the failed pass
    stopped only re-reads that page -- the bug that turned a 250-row walk
    into a 9-row one (9 = the last page's rows and nothing else).
    """
    for _ in range(1, max_pages + 1):
        before = _page_label(driver.page)
        if _page_number(before) == 1:
            return True                               # already on page 1
        try:
            prev_btn = driver.page.get_by_role(
                "button",
                name=re.compile(r"previous|^prev$", re.I)).first
            if not prev_btn.count():
                # No Prev we can name: only a pager with no Next either
                # (single page -> we ARE on page 1) proves where we are.
                nxt = driver.page.get_by_role(
                    "button", name=re.compile(r"^Next$", re.I)).first
                return not nxt.count()
            if not prev_btn.is_enabled():
                # Prev disabled = first page; refuse when the label says
                # otherwise (then we cannot prove where we are).
                return _page_number(before) in (0, 1)
            before_first = _page_first_id(driver.page)
            prev_btn.scroll_into_view_if_needed(timeout=5000)
            prev_btn.click(timeout=8000)
        except Exception:                             # noqa: BLE001
            return False
        if not _wait_page_turn(driver.page, before, before_first):
            return False
    return _page_number(_page_label(driver.page)) == 1


def collect_tab_pages(driver: DeliverDriver, expected: int = 0,
                      max_pages: int = 40) -> list:
    """Every raw row of the current tab, page by page (Prev/Next pager).

    Read the settled page -> click Next (enabled only) -> wait for the
    next page to land -> repeat until Next is gone/disabled or the
    "Page N of N" label stops advancing. Order-ID dedupe across pages, so
    a re-rendered page can never double-count a row.
    """
    seen_order: set = set()
    pages: list = []
    for _ in range(1, max_pages + 1):
        # Two waits, in order: first a bounded "at least one row exists"
        # (wait_for_orders_ready), then poll until the row count has
        # STOPPED MOVING. The old first-row-only wait returned while pages
        # were still filling -- the row count is what proves a page is
        # fully rendered.
        driver.wait_for_orders_ready()
        wait_for_rows_settled(driver)
        pages.append(collect_page_rows(driver))
        before = _page_label(driver.page)
        before_first = _page_first_id(driver.page)
        try:
            next_btn = driver.page.get_by_role(
                "button", name=re.compile(r"^Next$", re.I)).first
            if not (next_btn.count() and next_btn.is_enabled()):
                break                                 # last page reached
            # The pager sits under a sticky footer: an instant click can hit
            # the overlay instead, leaving the page un-advanced while the
            # click "succeeds". Scrolling it into view first lands the
            # click; _wait_page_turn then requires BOTH proofs (label AND
            # first order id) before the next page may be read.
            next_btn.scroll_into_view_if_needed(timeout=5000)
            next_btn.click(timeout=8000)
        except Exception:                             # noqa: BLE001
            break                                     # pager gone: done
        if not _wait_page_turn(driver.page, before, before_first):
            print("[scan] page did not advance after Next -- "
                  "stopping there", flush=True)
            break
    out: list = []
    for raw_rows in pages:
        for raw in raw_rows:
            order_id = (raw.get("orderId") or "").strip()
            if order_id:
                if order_id in seen_order:
                    continue                          # re-rendered page
                seen_order.add(order_id)
            out.append(raw)
    return out


def scan_duplicates(client: str, config: dict, *, headful: bool = True,
                   tab: str = "pending", verbose: bool = False,
                   duplicates_csv: Path | None = None) -> dict:
    """Browser entry for `--scan-duplicate`: READ-ONLY duplicate detection.

    Same login and filters as the deliver flow, but the local Excel is never
    read and nothing is ever written on the CRM: no row menu, no modal, no
    Save. It only reads the live list, flags duplicate uploads and writes the
    client's own export files.
    """
    manager = WorkspaceManager()
    canonical = manager.resolve_client_id(client)
    paths = manager.client_paths(canonical)
    deliver_dir = paths.state_dir / "deliver"
    if duplicates_csv is None:
        cfg_csv = config.get("duplicatesCsv")
        duplicates_csv = (Path(cfg_csv) if cfg_csv
                          else deliver_dir / "duplicates.csv")
    report_path = deliver_dir / "duplicate_scan_report.json"
    ledger_path = deliver_dir / "duplicates.json"
    ledger = DuplicateLedger.load(ledger_path)
    # A scan touches no order state, but it drives the same browser session,
    # so it takes the same per-client lock (serialised with a deliver run).
    store = SqliteDeliverStore(
        canonical, config.get("period", DEFAULT_PERIOD), deliver_dir)
    run_id = f"scan_{now_iso()}"
    summary: dict = {}
    try:
        with JobLock(store.lock_path):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not headful)
                context = None
                try:
                    context, page = login_with_session_cache(
                        browser, LOGIN_URL, TARGET_URL, LOGGED_IN_SELECTOR,
                        storage_path=str(paths.auth_dir / "session_state.json"),
                        network_wait_timeout_ms=LOGIN_NETWORK_WAIT_MS,
                        client_id=canonical,
                        credential_reference=manager.credential_reference(
                            canonical),
                        env_file=str(manager.shared_credentials_env_path()),
                        cache_file=str(paths.auth_dir / "credentials.json"),
                    )
                    _confirm_orders_page(page)
                    _ensure_viewport(page)
                    driver = DeliverDriver(page, store, config,
                                           verbose=verbose)
                    summary = scan_once(
                        driver, canonical,
                        duplicates_csv=duplicates_csv, run_id=run_id,
                        tab=tab, ledger=ledger, source="")
                finally:
                    try:
                        if context is not None:
                            context.close()
                    except Exception:                   # noqa: BLE001
                        pass
                    try:
                        browser.close()
                    except Exception:                   # noqa: BLE001
                        pass
        ledger.save()
        report = dict(summary)
        report.update({"runId": run_id, "client": canonical, "readOnly": True,
                       "scannedAt": now_iso(),
                       "duplicatesCsv": str(duplicates_csv),
                       "ledger": str(ledger_path), "sourceFile": "",
                       "tab": tab})
        atomic_write_json(report_path, report)
        print(f"[scan] {report.get('groups', 0)} duplicate group(s) over "
              f"{report.get('rows', 0)} row(s) read "
              f"(tab={tab}, per-page={report.get('perPage')})", flush=True)
        if not report.get("complete", True):
            print(f"[scan] INCOMPLETE: read {report.get('rows', 0)} of "
                  f"{report.get('expected', 0)} row(s) the tab reports -- the "
                  f"list below may be missing duplicates; re-run the scan",
                  flush=True)
        print(f"[scan] report: {report_path}", flush=True)
        if report.get("exported"):
            print(f"[scan] {report['exported']} copy row(s) exported -> "
                  f"{duplicates_csv}", flush=True)
        else:
            print(f"[scan] no new duplicate copies to export (CSV kept as "
                  f"is: {duplicates_csv})", flush=True)
        return report
    finally:
        store.close()




def _merge_raw_rows(*walks) -> list:
    """Union of pager walks, keyed by order id.

    A shorter re-walk can ADD rows the first walk missed, but can never
    throw away rows already read -- the old code overwrote a 250-row pass
    with a 9-row retry that had started on the last page.
    """
    out: list = []
    seen: set = set()
    for walk in walks:
        for raw in walk or []:
            order_id = (raw.get("orderId") or "").strip()
            if order_id:
                if order_id in seen:
                    continue
                seen.add(order_id)
            out.append(raw)
    return out


def scan_tab_pages(driver: DeliverDriver, which: str, expected: int = 0) -> list:
    """One read-only pass over a single tab: pager walk -> enriched rows.

    Reads at the CRM's own default 50/page -- no rows-per-page combobox
    touch at all -- then advances Next until it is disabled or the page
    label stops moving. One clean bulk read per page. A short first walk
    is re-walked ONCE, but only after the pager is provably rewound to
    page 1, and the two walks are merged by order id.
    """
    raw = collect_tab_pages(driver, expected=expected)
    if expected and len(raw) < expected:
        print(f"[scan] {which}: read {len(raw)} of {expected} -- "
              f"re-walking from page 1 once", flush=True)
        if _reset_to_first_page(driver):
            retry = collect_tab_pages(driver, expected=expected)
            raw = _merge_raw_rows(raw, retry)
        else:
            print(f"[scan] {which}: pager would not rewind to page 1 -- "
                  f"keeping the {len(raw)} row(s) already read", flush=True)
    rows = enrich_rows(raw)
    for row in rows:
        row["tab"] = which
    return rows


def scan_once(driver: DeliverDriver, client_id: str, *,
              duplicates_csv, source: str = "", run_id: str = "",
              tab: str = "pending", ledger=None) -> dict:
    """One read-only pass over the live list: filter -> pager walk every
    page -> group by name+phones -> flag + export.

    `tab` is "pending" or "both" (Pending plus Delivered -- the shape of a
    double upload where one copy already moved). Nothing is ever written on
    the CRM; only the client's own files change.
    """
    started_at = now_iso()
    t0 = time.monotonic()
    driver.ensure_form_chips()
    driver.ensure_filters(client_id)
    driver.submit_search()
    driver.wait_for_orders_ready()
    driver.clear_search()          # no stale quick-search text can hide rows
    driver.submit_search()
    driver.wait_for_orders_ready()

    tabs = ("pending", "delivered") if tab == "both" else ("pending",)
    expected = 0
    all_rows: list = []
    for which in tabs:
        if which == "pending":
            expected = driver.open_pending_tab()
        else:
            driver.open_delivered_tab()
            expected = 0
        # Every walk must start at page 1: the pager can still be parked
        # where an earlier visit left it, and a walk that starts mid-table
        # reports only the pages it happened to see.
        driver.wait_for_orders_ready()
        if not _reset_to_first_page(driver):
            print(f"[scan] {which}: could not confirm page 1 before "
                  f"walking -- the read may start mid-table", flush=True)
        # Default 50/page, walked with the pager -- one clean read per page.
        all_rows.extend(scan_tab_pages(driver, which, expected=expected))

    if expected and len(all_rows) < expected:
        print(f"[scan] WARNING: read {len(all_rows)} row(s) but the Pending "
              f"tab says {expected} -- some page may not have loaded; "
              f"duplicates below it may be hidden", flush=True)

    groups = groups_from_rows(all_rows, customer_key)
    exported = 0
    names = []
    detail = []
    for key, copies in groups:
        shown = sorted(copies, key=lambda c: c.get("orderId") or "")
        written = 0
        try:
            written = export_duplicate(
                duplicates_csv, shown, client=client_id, source=source,
                run_id=run_id, match="name+phones", fingerprint=key)
        except Exception as exc:                      # noqa: BLE001
            print(f"[scan] warning: could not write {duplicates_csv}: {exc}",
                  flush=True)
        exported += written
        first = shown[0]
        names.append(first.get("customer", ""))
        detail.append({
            "fingerprint": key,
            "customer": first.get("customer", ""),
            "phones": first.get("phones"),
            "copies": len(shown),
            "orderIds": [c.get("orderId") for c in shown],
            "amounts": sorted({c.get("amount") for c in shown}),
            "exportedNow": written,
        })
        print(f"[scan] duplicate: {first.get('customer', '')!r} x{len(shown)} "
              f"(ids={[c.get('orderId') for c in shown]})", flush=True)
        if ledger is not None:
            try:
                ledger.remember(
                    key, name=first.get("customer", ""),
                    amount=first.get("amount"), phones=first.get("phones"),
                    order_ids=[c.get("orderId") for c in shown],
                    action="scan")
            except Exception as exc:                  # noqa: BLE001
                print(f"[scan] warning: ledger not saved: {exc}", flush=True)
    summary = {
        "rows": len(all_rows), "expected": expected, "perPage": 50,
        # False => the table was still short of the tab's own count, so the
        # duplicate list below may be incomplete (never silently trusted).
        "complete": bool(not expected or len(all_rows) >= expected),
        "tabs": list(tabs), "groups": len(groups),
        "copies": sum(len(copies) for _, copies in groups),
        "exported": exported, "names": names, "detail": detail,
        "seconds": round(time.monotonic() - t0, 3),
    }
    return summary
