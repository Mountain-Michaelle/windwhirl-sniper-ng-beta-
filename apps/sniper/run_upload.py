"""
run_automation.py

Entry point for the Product Order Automation. Mirrors run_recorder.py's
structure on purpose: log in -> for each row, run add_products -> log
results. Does NOT touch nav_utils.py or any browser-launch logic -- it
only plugs into them.

FIX (Sep 2026, "selection failures cascade into slow full-page reloads"):
_attempt_order's lock-mode retry loop previously called process_order()
again immediately after a transient failure, with no cleanup in between.
A failed attempt can leave the Add Order dialog (and/or its modal
backdrop, mid fade-out-animation) sitting on the page; the next
process_order() call's first action is clicking "Add Order" again, which
then gets swallowed by that leftover backdrop, times out, gets
misclassified as transient too, and repeats -- until the 3-strikes rule
forces a full page reload. See add_products.py's module docstring, bug
(2), for the field-log evidence. _force_close_any_dialog(page, cfg) is
now called between retries, not just after the loop exits, so each retry
starts from a clean dialog state instead of fighting the last one.

FIX (Sep 2026, "_select_combobox() missing 1 required positional
argument: 'state'"): add_products.py's combobox helpers now track which
combobox is currently open via a small `_ComboState` object (see that
module's docstring, bug (3)/(4), for why) -- `_select_combobox()` takes
one as a required argument everywhere it's called. `scrape_crm_catalog()`
here is the one caller of `_select_combobox()` that lives outside
add_products.py, and it was never updated when that parameter was added,
so every catalog scrape crashed before it could read a single product.
Fixed by importing `_ComboState` and giving each scrape its own fresh
instance, exactly like `process_order()` does per order.

FIX (Sep 2026, "need proof of why it closed, not just that it closed"):
add_products.py's closure-diagnostics capture (screenshot + state dump,
always; a saved Playwright trace chunk, if tracing is running) is
best-effort and silently skips the trace part if nothing ever called
`context.tracing.start()`. That call belongs here, once, since this file
owns the browser/context -- add_products.py never does. Tracing is
started right after login and stopped (chunk discarded, since a clean
run has nothing worth keeping) when the browser closes; every closure
detected in between gets its own saved chunk via
`_capture_closure_evidence`, which also re-arms a fresh chunk immediately
after saving so later orders keep being covered. Wrapped in try/except
throughout -- if tracing isn't available in this environment for any
reason, the run proceeds exactly as before, just without saved traces.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import statistics
import sys
import time

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# ==============================================================

_SCRIPT_DIR = Path(__file__).resolve().parent
CUSTOMERS_DIR = _SCRIPT_DIR / "customers"
CUSTOMERS_DATA_DIR = CUSTOMERS_DIR / "data"
MANUAL_CHECK_DIR = CUSTOMERS_DIR / "manual_check"
MANUAL_CHECK_PATH = MANUAL_CHECK_DIR / "failed_exported.csv"
FUZZY_REVIEW_PATH = MANUAL_CHECK_DIR / "fuzzy_matches_for_review.csv"
SKIPPED_ROWS_PATH = MANUAL_CHECK_DIR / "skipped_incomplete_rows.csv"
PROGRESS_DIR = CUSTOMERS_DIR / "progress"
CATALOG_CACHE_PATH = PROGRESS_DIR / "catalog_cache.json"
CATALOG_METADATA_PATH = PROGRESS_DIR / "catalog_metadata.json"
AUTH_STORAGE_PATH = _SCRIPT_DIR / "session_state.json"
ACTIVE_WORKSPACE: ClientWorkspace | None = None


def _configure_workspace(workspace: ClientWorkspace) -> None:
    """Point legacy module-level paths at one immutable client job."""
    global ACTIVE_WORKSPACE, CUSTOMERS_DIR, CUSTOMERS_DATA_DIR
    global MANUAL_CHECK_DIR, MANUAL_CHECK_PATH, FUZZY_REVIEW_PATH
    global SKIPPED_ROWS_PATH, PROGRESS_DIR, CATALOG_CACHE_PATH
    global CATALOG_METADATA_PATH, AUTH_STORAGE_PATH
    ACTIVE_WORKSPACE = workspace
    CUSTOMERS_DIR = workspace.job_dir
    CUSTOMERS_DATA_DIR = workspace.job_dir / "source"
    MANUAL_CHECK_DIR = workspace.run_dir
    MANUAL_CHECK_PATH = MANUAL_CHECK_DIR / "failed_exported.csv"
    FUZZY_REVIEW_PATH = MANUAL_CHECK_DIR / "fuzzy_matches_for_review.csv"
    SKIPPED_ROWS_PATH = MANUAL_CHECK_DIR / "skipped_incomplete_rows.csv"
    PROGRESS_DIR = workspace.progress_dir
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_CACHE_PATH = workspace.catalog_dir / "catalog_cache.json"
    CATALOG_METADATA_PATH = workspace.catalog_dir / "catalog_metadata.json"
    workspace.catalog_dir.mkdir(parents=True, exist_ok=True)
    AUTH_STORAGE_PATH = workspace.auth_dir / "session_state.json"
    workspace.auth_dir.mkdir(parents=True, exist_ok=True)

ORDER_FILE_EXTENSIONS = (".csv", ".xlsx", ".xls")

FILTER_COLUMN = "Filtered #"

REQUIRED_ORDER_FIELDS = ("customer_name", "phone_number", "product", "address", "price")


import pandas as pd
from playwright.sync_api import sync_playwright

from apps.sniper.nav_utils import safe_goto
from apps.sniper.utils.auth import login_with_session_cache, MissingCredentialsError
from apps.sniper.product_upload.add_products import (
    AutomationConfig, process_order, to_failed_order,
    to_flagged_order, FlaggedForReview,
)
from apps.sniper.product_upload.dialog_controls import _force_close_any_dialog, _ComboState
from apps.sniper.product_upload.failure_logger import FailureLogger, FailedOrder
import apps.sniper.utils.field_selectors as sel

from apps.sniper.product_upload.progress_tracker import ProgressTracker, order_key
from apps.sniper.product_upload.retry_cache import (
    RetryCache, RetryEntry, is_transient_failure, MAX_RETRY_ATTEMPTS,
    collect_and_merge_shard_queues, partition_queue_into_shards,
    _shard_queue_path,
)
from apps.sniper.product_upload.failed_ledger import (
    FailedLedger, collect_and_merge_shard_ledgers,
    partition_ledger_into_shards, _shard_ledger_path,
)
from apps.sniper.product_upload.planning import ExecutionPlan, PlanningError, build_execution_plan
from apps.sniper.product_upload.state_engine import (
    cache_lookup_source, load_state_cache, prewarm_state_cache,
    save_state_cache,
)
from apps.sniper.client_workspace import (
    WorkspaceManager, ClientWorkspace, JobLock, name_key_from_filename,
)

# ==============================================================================
LOGIN_URL = "https://sniper.ng/login"
TARGET_URL = "https://sniper.ng/orders"

LOGGED_IN_SELECTOR = 'button:has-text("Add Order")'

COLUMN_MAP = {
    "Customer Name": "customer_name",
    "Phone Number": "phone_number",
    "WhatsApp Number": "whatsapp_number",
    "Address": "address",
    "Product": "product",
    "Price (₦)": "price",
}


def find_orders_file() -> Path:
    candidates = sorted(
        p for p in CUSTOMERS_DATA_DIR.glob("*")
        if p.is_file() and p.suffix.lower() in ORDER_FILE_EXTENSIONS
    )

    if not candidates:
        raise FileNotFoundError(
            f"No .csv/.xlsx/.xls file found in {CUSTOMERS_DATA_DIR}. "
            f"Place exactly one order file there and re-run."
        )
    if len(candidates) > 1:
        listed = "\n  ".join(str(p) for p in candidates)
        raise RuntimeError(
            f"Expected exactly one order file in {CUSTOMERS_DATA_DIR}, found {len(candidates)}:\n"
            f"  {listed}\n"
            f"Remove the extras and re-run."
        )
    return candidates[0]


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() in ("", "NaT", "NaN", "None")


def _has_filter_number(value) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _missing_required_fields(order: dict) -> list[str]:
    """Oct 2026 rule: the ONLY rows that may be skipped at load time are ones
    that are genuinely un-actionable --
      - no product AND no price (nothing to upload and nothing to infer), or
      - no customer name, or
      - no phone number AND no WhatsApp number (no way to reach the customer).
    Address is deliberately NOT a gate any more: a blank address is handled
    downstream by defaulting to Lagos, and any other missing piece is reported
    by the normal failure path rather than silently dropped here."""
    problems: list[str] = []
    if _is_blank(order.get("product")) and _is_blank(order.get("price")):
        problems.append("product and price")
    if _is_blank(order.get("customer_name")):
        problems.append("customer_name")
    if _is_blank(order.get("phone_number")) and _is_blank(order.get("whatsapp_number")):
        problems.append("phone_number/whatsapp_number")
    return problems


@dataclasses.dataclass
class SkippedRow:
    excel_row: int
    reason: str
    customer_name: str = ""
    phone_number: str = ""
    product: str = ""


def load_orders(path: str) -> list[dict]:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:
        raise ValueError(
            f"Input file is missing expected column(s): {missing}. "
            f"Found columns: {list(df.columns)}"
        )

    has_filter_column = FILTER_COLUMN in df.columns
    if not has_filter_column:
        print(f"WARNING: '{FILTER_COLUMN}' column not found in {path} -- "
              f"skipping that gate and relying on the blank/required-field "
              f"checks below only.")

    orders: list[dict] = []
    skipped_rows: list[SkippedRow] = []
    skipped_no_filter_number = 0
    skipped_blank = 0
    skipped_missing_required = 0

    for pos, (_, row) in enumerate(df.iterrows()):
        excel_row = pos + 2

        if has_filter_column and not _has_filter_number(row[FILTER_COLUMN]):
            skipped_no_filter_number += 1
            continue

        order = {internal: row[excel_col] for excel_col, internal in COLUMN_MAP.items()}

        if all(_is_blank(v) for v in order.values()):
            skipped_blank += 1
            continue

        missing_fields = _missing_required_fields(order)
        if missing_fields:
            skipped_missing_required += 1
            skipped_rows.append(SkippedRow(
                excel_row=excel_row,
                reason=f"missing required field(s): {', '.join(missing_fields)}",
                customer_name=_clean_for_report(order.get("customer_name")),
                phone_number=_clean_for_report(order.get("phone_number")),
                product=_clean_for_report(order.get("product")),
            ))
            continue

        orders.append(order)

    if skipped_no_filter_number:
        print(f"Skipped {skipped_no_filter_number} row(s) with no number in '{FILTER_COLUMN}'.")
    if skipped_blank:
        print(f"Skipped {skipped_blank} fully-blank row(s) in the input file.")
    if skipped_missing_required:
        print(f"Skipped {skipped_missing_required} row(s) with no product+price, "
              f"no customer name, or no phone/whatsapp number -- not attempted.")

    if skipped_rows:
        skipped_path = export_skipped_rows(skipped_rows)
        print(f"Skipped-row details written to: {skipped_path}")

    return orders


def _clean_for_report(value) -> str:
    if _is_blank(value):
        return ""
    return " ".join(str(value).split())


def export_skipped_rows(skipped_rows: list[SkippedRow]) -> Path:
    MANUAL_CHECK_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([dataclasses.asdict(row) for row in skipped_rows])
    df.to_csv(SKIPPED_ROWS_PATH, index=False)
    return SKIPPED_ROWS_PATH


def _read_catalog_cache() -> Optional[list[str]]:
    """FIX (per request, "product list should be cached to a local JSON
    file and reused day to day"): the CRM catalog only changes when
    someone edits the product list on the website, which this
    automation has no reason to expect happens between runs -- so
    re-scraping it by opening the Add Order wizard, selecting Owning
    Form, opening the product dropdown, and reading every option back
    out is pure overhead on every single script start once it's been
    read once. If a previous run already wrote a cache, use it and skip
    the wizard entirely -- scrape_crm_catalog() is never even called.

    Deliberately does NOT try to detect whether the website's catalog
    has changed -- any check like that would need to open the wizard
    anyway, which is exactly the cost this cache exists to avoid paying
    on every run. Use --refresh-catalog when the product list actually
    changes; the cache is otherwise trusted as-is, same as the person
    asked for."""
    try:
        if not CATALOG_CACHE_PATH.exists():
            return None
        data = json.loads(CATALOG_CACHE_PATH.read_text(encoding="utf-8"))
        products = data.get("products")
        timestamp = data.get("cached_at")
        cached_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        if (
            isinstance(products, list)
            and products
            and datetime.now(timezone.utc) - cached_at <= timedelta(hours=6)
        ):
            return products
        return None
    except Exception:
        return None


def _write_catalog_cache(products: list[str]) -> None:
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        CATALOG_CACHE_PATH.write_text(
            json.dumps({"products": products,
                        "cached_at": datetime.now(timezone.utc).isoformat()}, indent=2),
            encoding="utf-8",
        )
        CATALOG_METADATA_PATH.write_text(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(),
                        "product_count": len(products)}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[cache] could not write catalog cache (non-fatal, will just re-scrape "
              f"next run too): {e}", flush=True)


def scrape_crm_catalog(page) -> list[str]:
    err = None
    cfg_stub = AutomationConfig(crm_catalog=[])
    try:
        from apps.sniper.product_upload.dialog_controls import (
            _open_add_order_wizard, _select_combobox,
        )
        dialog = _open_add_order_wizard(page, cfg_stub)

        # FIX (Sep 2026, "_select_combobox() missing 1 required positional
        # argument: 'state'"): every combobox interaction now needs a
        # _ComboState to track which listbox is currently open (see
        # add_products.py's module docstring) -- this is the one caller of
        # _select_combobox() living outside that module, and it never got
        # updated when that parameter was introduced. One fresh state per
        # scrape, same as process_order() does per order.
        combo_state = _ComboState()
        owning_form_err = _select_combobox(
            dialog, page, dialog.locator(sel.combo_trigger(sel.OWNING_FORM_LABEL)),
            sel.OWNING_FORM_VALUE, cfg_stub, "Owning Form", combo_state,
        )
        if owning_form_err:
            raise RuntimeError(owning_form_err)

        first_block = dialog.locator(sel.PRODUCT_BLOCK_SELECTOR).first
        product_combo = first_block.locator('button[role="combobox"]').first
        product_combo.click(no_wait_after=True)
        page.locator(sel.LISTBOX_SELECTOR).first.wait_for(state="visible", timeout=15000)
        labels = page.locator(sel.OPTION_SELECTOR).all_inner_texts()
        page.keyboard.press("Escape")

        try:
            dialog.get_by_role("button", name=sel.CLOSE_BUTTON_NAME).first.click(
                timeout=3000, no_wait_after=True
            )
        except Exception:
            page.keyboard.press("Escape")
    except Exception as e:
        err = e
        labels = []
    finally:
        # A failed catalog read can leave the modal/backdrop open. Close it
        # before the next local retry so the page remains usable.
        _force_close_any_dialog(page, cfg_stub)

    if err:
        print(f"ERROR: could not scrape the product catalog from the Add Order wizard: {err}")
        return []

    junk = ("", "select", "select product", "select a product first")
    return [l.strip() for l in labels if l.strip() and l.strip().lower() not in junk]


def _reload_for_recovery(page) -> None:
    page.reload(wait_until="commit")
    page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=40000)


def _load_catalog(page, lock_mode: bool, refresh_catalog: bool = False) -> list[str]:
    if not refresh_catalog:
        cached = _read_catalog_cache()
        if cached is not None:
            print(f"Loaded {len(cached)} sellable product(s) from the local catalog "
                  f"cache ({CATALOG_CACHE_PATH}) -- skipping the live wizard scrape "
                  f"entirely. Pass --refresh-catalog if the product list on the site "
                  f"has actually changed.", flush=True)
            return cached
    else:
        print("[cache] --refresh-catalog passed -- ignoring any existing catalog "
              "cache and re-scraping from the live wizard.", flush=True)

    if not lock_mode:
        catalog = scrape_crm_catalog(page)
        if catalog:
            _write_catalog_cache(catalog)
        return catalog

    failures = 0
    while True:
        catalog = scrape_crm_catalog(page)
        if catalog:
            _write_catalog_cache(catalog)
            return catalog
        failures += 1
        print(f"[lock] catalog attempt {failures}/3 failed; retrying immediately.",
              flush=True)
        if failures < 3:
            continue
        print("[lock] catalog failed 3 consecutive times; reloading the page.",
              flush=True)
        _reload_for_recovery(page)
        failures = 0


def export_manual_check(failed_rows: list[FailedOrder]) -> Path:
    MANUAL_CHECK_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([dataclasses.asdict(row) for row in failed_rows])
    df.to_csv(MANUAL_CHECK_PATH, index=False)
    return MANUAL_CHECK_PATH


def export_fuzzy_review(flagged_rows: list[FlaggedForReview]) -> Path:
    MANUAL_CHECK_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([dataclasses.asdict(row) for row in flagged_rows])
    df.to_csv(FUZZY_REVIEW_PATH, index=False)
    return FUZZY_REVIEW_PATH


def print_performance_summary(counters: dict) -> None:
    samples = counters.get("timings_ms", [])
    if not samples:
        return
    totals = [sum(t.values()) for t in samples]
    p95 = max(totals) if len(totals) < 20 else statistics.quantiles(
        totals, n=20, method="inclusive"
    )[18]
    print(
        "[metrics] browser phases ms: "
        f"orders={len(totals)} avg={statistics.mean(totals):.1f} "
        f"p50={statistics.median(totals):.1f} p95={p95:.1f}"
    )


def _match_fresh_order(
    entry,
    orders: list[dict],
) -> tuple[dict | None, int | None, bool, str | None]:
    """Re-read the live workbook row for a stale failure so it is NEVER
    judged from the cached order dict that was saved at failure time.

    If the row was EDITED since it failed, the fresh order (with a NEW
    dedup key) MUST be retried, and the old cached entry (recorded under
    the OLD key) must be removed -- otherwise "same error" skips a row the
    user already fixed. If the row was REMOVED from the file, drop it
    cleanly from both the ledger and the retry queue with no browser.

    Returns:
      (fresh_order_or_None, row_number, changed, note_or_None)
      - row_number is the 1-based position in the fresh workbook (None when
        the row has since been deleted from the file);
      - changed is True exactly when order_key(fresh) != entry.key.
    Matching uses a customer_name backstop (not just row position) so it
    survives row reordering inside the workbook.
    NOTE (Oct 2026, "failed rows were judged from cached data and never
    re-read the real source file, so a fixed row was still skipped as
    'same error'"): this function is the fix -- every stale entry re-reads
    the ACTUAL workbook row before any permanent-reason skip or same-error
    skip, and an edited row is retried instead of skipped.
    """
    # entry is a FailedLedgerEntry or a RetryEntry; neither carries a
    # customer_name field, so the backstop reads it from the cached order.
    cached = entry.order or {}
    name = cached.get("customer_name")
    tot_rows = len(orders)
    placement: int | None = None
    matched = False

    # 1) Try the recorded row number first (still present in the live file
    #    and still matches by key OR by the customer_name backstop).
    if (entry.row_number is not None
            and 1 <= entry.row_number <= tot_rows):
        candidate = orders[entry.row_number - 1]
        if (order_key(candidate) == entry.key
                or candidate.get("customer_name") == name):
            matched = True
            placement = entry.row_number

    # 2) Fall back to a full scan by key + customer_name backstop (so a row
    #    that moved within the workbook is still recognized).
    if not matched:
        for idx, candidate in enumerate(orders, start=1):
            if order_key(candidate) != entry.key:
                continue
            if (name is not None
                    and candidate.get("customer_name") != name):
                continue
            matched = True
            placement = idx
            break

    if not matched:
        if (entry.row_number is not None
                and 1 <= entry.row_number <= tot_rows):
            still_there = orders[entry.row_number - 1]
            if still_there.get("customer_name") != name:
                return (None, entry.row_number, False,
                        "row removed from the upload file")
            return (None, entry.row_number, False,
                    f"row {entry.row_number} present but no longer matches "
                    f"entry key {entry.key!r} (customer_name differs too)")
        return None, None, False, "row not found in the upload file"

    fresh = orders[placement - 1]
    changed = order_key(fresh) != entry.key
    note: str | None = None
    if changed:
        note = (
            f"row at position {placement} was EDITED since the failure -- "
            f"key changed from {entry.key!r} to {order_key(fresh)!r}; "
            f"retrying the amended row"
        )
    return fresh, placement, changed, note


def _attempt_order(page, cfg, order: dict, row_number, progress: ProgressTracker,
                    job_progress: ProgressTracker,
                    retry_cache: RetryCache, failed_ledger: FailedLedger,
                    failure_log: FailureLogger,
                    failed_rows: list[FailedOrder], counters: dict,
                    flagged_rows: list[FlaggedForReview],
                    plan: ExecutionPlan | None = None,
                    lock_mode: bool = False) -> bool:
    if progress.is_done(order) or job_progress.is_done(order):
        counters["skipped_duplicate"] = counters.get("skipped_duplicate", 0) + 1
        retry_cache.remove(order)
        failed_ledger.remove(order)
        print(f"    SKIP -- already succeeded earlier this run/a previous run "
              f"(same customer+phone+product+price fingerprint) -- not resubmitting", flush=True)
        return True

    consecutive_failures = 0
    while True:
        result = process_order(page, order, cfg, row_number=row_number, plan=plan)
        if not lock_mode or result.success or not is_transient_failure(result.reason):
            break
        consecutive_failures += 1
        # FIX (Sep 2026): clean up whatever the failed attempt left behind
        # (typically the Add Order dialog and/or its backdrop) before
        # looping back into process_order(). Previously this loop retried
        # process_order() with no cleanup in between, so a failed
        # attempt's leftover modal backdrop (still animating out, still
        # `data-state="open"`) would sit on top of the page and intercept
        # the next attempt's very first click (the "Add Order" button),
        # which then burned its full click_resilient timeout budget
        # failing to click through the interception, got misclassified
        # as transient again, and repeated the same broken cycle --
        # eventually forcing a full page reload every 3 attempts. See
        # add_products.py's module docstring, bug (2).
        dialog_confirmed_closed = _force_close_any_dialog(page, cfg)
        if not dialog_confirmed_closed:
            # FIX (Sep 2026, "still clicking Add Order while the modal is
            # open"): _force_close_any_dialog now reports whether it
            # actually got the dialog closed instead of assuming so.
            # Retrying process_order() here would just click "Add Order"
            # behind a modal that's confirmed still open -- the exact
            # 2x30s-timeout-then-reload-anyway cascade seen in the field
            # log. Skip straight to a reload instead of paying for that
            # doomed retry.
            print("    [lock] Add Order dialog would not close after a "
                  "transient failure (tried Cancel/Escape/discard-confirm) "
                  "-- reloading now instead of retrying against a stuck "
                  "modal.", flush=True)
            _reload_for_recovery(page)
            consecutive_failures = 0
            continue
        if consecutive_failures < 3:
            print(f"    [lock] transient failure {consecutive_failures}/3; "
                  "retrying immediately.", flush=True)
            continue
        print("    [lock] 3 consecutive transient failures; reloading the page.",
              flush=True)
        _reload_for_recovery(page)
        consecutive_failures = 0
    if result.timings_ms:
        counters.setdefault("timings_ms", []).append(result.timings_ms)

    if result.success:
        counters["succeeded"] += 1
        progress.mark_done(order)
        job_progress.mark_done(order)
        retry_cache.remove(order)
        failed_ledger.remove(order)
        print(f"    OK   -- matched: {result.matched_products} | {result.reason}", flush=True)
        if result.needs_manual_review:
            counters["flagged_for_review"] = counters.get("flagged_for_review", 0) + 1
            flagged_rows.append(to_flagged_order(order, result))
            print(f"    FLAGGED -- uploaded at reduced confidence, added to "
                  f"fuzzy_matches_for_review.csv for a manual double-check", flush=True)
    elif is_transient_failure(result.reason):
        entry = retry_cache.record_failure(order, result.reason, row_number=row_number)
        # FIX (Sep 2026, "try all failed uploads first on every fresh run"):
        # the failed-orders ledger mirrors EVERY failure (queued, given-up,
        # or permanent) so the next run retries it first and so it keeps the
        # job from being marked completed while it is still un-uploaded.
        failed_ledger.record_failure(order, result.reason, row_number=row_number)
        if retry_cache.should_give_up(entry):
            print(f"    FAIL -- gave up after {entry.attempts} attempt(s), treating as "
                  f"permanent: {result.reason}", flush=True)
            retry_cache.remove(order)
            counters["failed"] += 1
            failed_order = to_failed_order(order, result)
            failure_log.log(failed_order)
            failed_rows.append(failed_order)
        else:
            print(f"    QUEUED FOR RETRY ({entry.attempts}/{MAX_RETRY_ATTEMPTS}) -- {result.reason}",
                  flush=True)
            counters["queued"] += 1
    else:
        counters["failed"] += 1
        print(f"    FAIL -- {result.reason}", flush=True)
        failed_ledger.record_failure(order, result.reason, row_number=row_number)
        failed_order = to_failed_order(order, result)
        failure_log.log(failed_order)
        failed_rows.append(failed_order)

    # Keep the SPA warm.  Repair the wizard locally first; navigation is a
    # last-resort recovery only when the known ready-state anchor is absent.
    # PERF: only failures pay for dialog cleanup now -- on success
    # detect_save_outcome already confirmed the dialog closed, and the
    # next order's _open_add_order_wizard re-checks the overlay anyway.
    if not result.success:
        _force_close_any_dialog(page, cfg)
        try:
            page.get_by_role("button", name=sel.ADD_ORDER_BUTTON_NAME, exact=True).wait_for(
                state="attached", timeout=1500
            )
        except Exception:
            safe_goto(page, TARGET_URL, wait_until="commit")
            page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=20000)
    return result.success


def _drain_retry_queue(page, cfg, orders, progress, job_progress, retry_cache, failed_ledger,
                       failure_log, failed_rows, counters, flagged_rows,
                       label: str, lock_mode: bool = False) -> None:
    """Re-attempt everything sitting in the persistent retry queue.

    FIX (Oct 2026, "failed rows were judged from CACHED data and never
    re-read the real source file, so a row that was fixed after failing was
    still skipped as 'same error'"): before each queued attempt the row is
    re-read from the LIVE workbook via _match_fresh_order:
      - row removed from the file -> dropped from the retry queue AND the
        failed ledger, no browser interaction;
      - row EDITED since it failed -> stale cached entry removed and the
        fresh amended row retried;
      - row unchanged -> the cached order is retried exactly as before.
    """
    pending = retry_cache.pending_entries()
    if not pending:
        return
    print(f"  [retry] {label}: {len(pending)} queued order(s)...", flush=True)
    for entry in pending:
        order = entry.order
        row_number = entry.row_number
        if orders:
            fresh_order, fresh_row, changed, note = _match_fresh_order(entry, orders)
            if fresh_order is None:
                retry_cache.remove_key(entry.key)
                failed_ledger.remove_key(entry.key)
                print(f"  [retry] {order.get('customer_name')!r}: row no longer "
                      f"present in the upload file -- dropped from the retry "
                      f"queue and failed ledger.", flush=True)
                continue
            if changed:
                retry_cache.remove_key(entry.key)
                failed_ledger.remove_key(entry.key)
                print(f"  [retry] {order.get('customer_name')!r}: {note} -- "
                      f"retrying the amended row.", flush=True)
            order = fresh_order
            if fresh_row is not None:
                row_number = fresh_row
        print(f"  [retry] Retrying {order.get('customer_name')!r} "
              f"(attempt {entry.attempts + 1}/{MAX_RETRY_ATTEMPTS}, "
              f"last reason: {entry.last_reason})", flush=True)
        _attempt_order(page, cfg, order, row_number, progress, job_progress,
                       retry_cache, failed_ledger, failure_log, failed_rows, counters,
                       flagged_rows, lock_mode=lock_mode)


def _drain_failed_ledger(page, cfg, orders, progress, job_progress, retry_cache,
                         failed_ledger, failure_log, failed_rows, counters,
                         flagged_rows, label: str, lock_mode: bool = False) -> None:
    """FIX (Sep 2026, "try all failed uploads first on every fresh run; if
    the same error repeats, skip; never mark the job completed while failed
    customers are still un-uploaded"): every failed upload is recorded in the
    persistent failed-orders ledger, and THIS is the function that gives them
    their fresh attempt at the very start of a run -- before the retry queue
    and before any new row is touched.

    Per business rule:
      - each ledger entry gets exactly ONE fresh attempt per run;
      - if it fails again with the SAME reason as last recorded, it is
        skipped for the rest of the run (identical input against identical
        CRM data can only fail identically -- re-attempting it just burns
        time);
      - if the reason CHANGED, the order gets one more attempt this run
        before being left for the next one;
      - permanent data rejections (no product match, no state, CRM validation
        -- see is_transient_failure) are skipped WITHOUT a browser attempt at
        all: they are deterministic, so "same error" is guaranteed. They stay
        in the ledger and keep blocking job completion.

    FIX (Oct 2026, "failed rows were judged from CACHED data and never
    re-read the real source file, so a row that was fixed after failing was
    still skipped as 'same error'"): every entry re-reads the LIVE workbook
    row FIRST via _match_fresh_order. If the row was edited, the fresh
    amended row is retried (the stale cached entry is removed, and because
    the input changed the permanent-rejection shortcut no longer applies).
    If the row was removed from the file, it is dropped cleanly from both
    stores without opening the browser.
    """
    pending = failed_ledger.pending_entries()
    if not pending:
        return
    print(f"  [failed-retry] {label}: {len(pending)} previously failed order(s)...",
          flush=True)
    for entry in pending:
        order = entry.order
        row_number = entry.row_number
        row_changed = False

        # --- live-row recheck (the stale-skip fix) ---
        if orders:
            fresh_order, fresh_row, changed, note = _match_fresh_order(entry, orders)
            if fresh_order is None:
                retry_cache.remove_key(entry.key)
                failed_ledger.remove_key(entry.key)
                print(f"  [failed-retry] {order.get('customer_name')!r}: row no "
                      f"longer present in the upload file -- dropped from the "
                      f"failed ledger and retry queue.", flush=True)
                continue
            if changed:
                retry_cache.remove_key(entry.key)
                failed_ledger.remove_key(entry.key)
                print(f"  [failed-retry] {order.get('customer_name')!r}: {note} -- "
                      f"retrying the amended row.", flush=True)
                row_changed = True
            order = fresh_order
            if fresh_row is not None:
                row_number = fresh_row

        if progress.is_done(order) or job_progress.is_done(order):
            failed_ledger.remove_key(entry.key)
            continue
        # A changed row must be attempted even if the last reason looked
        # permanent: the input it was judged on no longer exists.
        if not row_changed and not is_transient_failure(entry.reason):
            print(f"  [failed-retry] {order.get('customer_name')!r}: permanent data "
                  f"rejection ({entry.reason}) -- same input can only fail the same "
                  f"way, skipping the browser attempt (still blocks completion).",
                  flush=True)
            continue
        print(f"  [failed-retry] Retrying {order.get('customer_name')!r} "
              f"(previously failed: {entry.reason})", flush=True)
        last_reason = entry.reason
        success = _attempt_order(page, cfg, order, row_number, progress, job_progress,
                                 retry_cache, failed_ledger, failure_log, failed_rows,
                                 counters, flagged_rows, lock_mode=lock_mode)
        if success:
            continue
        fresh = failed_ledger.get(entry.key)
        if fresh is None:
            # entry was replaced under a new key (edited row) or cleared --
            # a fresh failure is already recorded under the new key.
            continue
        if fresh.reason == last_reason:
            print(f"  [failed-retry] {order.get('customer_name')!r}: same error as the "
                  f"last run -- skipping further attempts this run (stays in the "
                  f"failed ledger).", flush=True)
            continue
        print(f"  [failed-retry] {order.get('customer_name')!r}: failure reason changed "
              f"({last_reason!r} -> {fresh.reason!r}) -- retrying once more this run.",
              flush=True)
        _attempt_order(page, cfg, order, row_number, progress, job_progress,
                       retry_cache, failed_ledger, failure_log, failed_rows,
                       counters, flagged_rows, lock_mode=lock_mode)


def _block_unnecessary_resources(page) -> None:
    """Strategic speed upgrade, not a logic change: stops the browser
    from downloading things this automation never looks at or waits on
    -- web fonts, and a fixed list of well-known third-party analytics/
    tracking scripts (Google Analytics, Meta Pixel, Hotjar, Intercom,
    Segment, Sentry, FullStory, Mixpanel, Microsoft Clarity). None of
    these affect the CRM's own DOM, selectors, or form behavior -- they
    are strictly extra page weight. Cutting them shortens every page
    load and reload, which matters most exactly where the person asked
    for it: low-network conditions.

    Deliberately does NOT block images: _capture_closure_evidence takes
    a real screenshot on every closure, and a screenshot with missing
    images is a worse piece of evidence for exactly the moments it
    matters most. If more speed is wanted than this gives and losing
    image fidelity in closure screenshots is an acceptable trade, add
    "image" and "media" to _BLOCKED_RESOURCE_TYPES above.

    Best-effort: page.route() itself can fail on some Playwright/browser
    combinations (rare) -- if it does, the run proceeds exactly as
    before, just without this particular saving."""
    def _handle(route):
        req = route.request
        try:
            if req.resource_type in _BLOCKED_RESOURCE_TYPES:
                return route.abort()
            url = req.url
            if any(s in url for s in _BLOCKED_URL_SUBSTRINGS):
                return route.abort()
        except Exception:
            pass
        return route.continue_()

    try:
        page.route("**/*", _handle)
    except Exception as e:
        print(f"[speed] could not set up resource blocking (non-fatal): {e}", flush=True)


def _start_diagnostics_tracing(context) -> bool:
    """FIX (Sep 2026): started once per run, right after login. Chunked
    (not one giant trace) so that add_products.py's
    _capture_closure_evidence can stop+save just the chunk covering the
    closure it just saw, then immediately open a fresh chunk so later
    orders keep being covered -- without this ever having been started,
    that save step is a harmless no-op and only the screenshot/.txt
    evidence gets written. Best-effort: some environments/older Playwright
    builds don't support tracing, or it may already be running from an
    outer harness -- either way, a failure here must never stop the
    order run itself."""
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        context.tracing.start_chunk(name="startup")
        return True
    except Exception as e:
        print(f"[diagnostics] Playwright tracing could not be started ({e}) -- "
              f"closures will still be captured as screenshots + .txt state "
              f"dumps, just without a saved trace .zip.", flush=True)
        return False


def _stop_diagnostics_tracing(context, tracing_active: bool) -> None:
    if not tracing_active:
        return
    try:
        # A clean end-of-run chunk has nothing worth keeping -- any
        # closure that happened along the way already got its own chunk
        # saved by _capture_closure_evidence at the moment it occurred.
        context.tracing.stop_chunk(discard=True)
    except Exception:
        pass
    try:
        context.tracing.stop()
    except Exception:
        pass


def _prewarm_todo_state_cache(todo: list[tuple[int, dict]]) -> dict:
    """FIX (Oct 2026, "temporal cache + very fast search"): resolve every
    todo-row address the local tables + job disk cache can't answer, once, in
    parallel, before planning -- and log each extraction (address -> state +
    source) plus a one-line run summary. Returns a per-row summary dict
    {row_number: (address, state_or_None, source)}. Never raises: any error
    just means planning falls back to its sequential path exactly as before.
    """
    summary: dict = {}
    if not todo:
        return summary
    try:
        addresses = [o.get("address", "") for _, o in todo]
        results = prewarm_state_cache(addresses, max_workers=8)
        # Normalized index: two rows spelling the same address differently
        # ("Sango ota" vs "sango  OTA") share one cache entry; the summary
        # must resolve both spellings, not just the first-seen one.
        norm_results: dict = {}
        for _addr, _state in results.items():
            norm_results.setdefault(" ".join(str(_addr).split()).lower(),
                                    _state)
        counts = {"local": 0, "web": 0, "disk": 0, "fallback": 0}
        for row_number, order in todo:
            address = order.get("address", "")
            source = cache_lookup_source(address)
            state = results.get(address, norm_results.get(
                " ".join(str(address).split()).lower()))
            if state is None and (not isinstance(address, str)
                                  or not address.strip()):
                state = "Lagos"
                source = "blank->Lagos"
            label = state if state is not None else "Lagos(fallback)"
            print(f"[state] row {row_number}: {str(address)[:60]!r} -> "
                  f"{label} ({source or 'local'})", flush=True)
            if source is None or source == "local":
                counts["local"] += 1
            elif "disk" in source:
                counts["disk"] += 1
            elif (source == "blank->Lagos"
                  or source.startswith("web-miss") or state is None):
                counts["fallback"] += 1
            else:
                counts["web"] += 1
            summary[row_number] = (address, state, source or "local")
        print(f"[state] {len(todo)} address(es): {counts['local']} local, "
              f"{counts['web']} web fresh, {counts['disk']} cache hits, "
              f"{counts['fallback']} fallbacks to Lagos.", flush=True)
    except Exception as exc:
        print(f"[state] parallel pre-warm skipped ({exc!r}) -- planning "
              f"resolves addresses sequentially as before.", flush=True)
    return summary


def _run_session(lock_mode: bool, orders: list[dict], progress: ProgressTracker,
                  job_progress: ProgressTracker,
                  retry_cache: RetryCache, failed_ledger: FailedLedger,
                  failure_log: FailureLogger, counters: dict,
                  failed_rows: list[FailedOrder], flagged_rows: list[FlaggedForReview],
                  refresh_catalog: bool = False, enable_tracing: bool = True) -> None:
    """One full attempt at a browser session: launch, log in, load the
    catalog, walk every order. Raises on anything it can't recover from
    locally (a real browser/network failure, an unhandled Playwright
    error, etc.) -- it does not catch broadly itself. main() is what
    decides whether a raised exception here means "give up" (--easy) or
    "relaunch everything and keep going" (--lock).

    FIX (per request, "nothing should stop the code when locked, only
    Ctrl+C"): this function used to BE main() -- any exception escaping
    it (a browser crash, a Playwright error on a bad connection, an
    unhandled edge case in login/catalog loading) had nothing above it
    to catch, so it took the whole script down. Pulling the session logic
    out into its own function that's allowed to raise, with main()
    supplying the "catch everything except Ctrl+C and start over" policy
    around it, is what makes that possible without duplicating the whole
    pipeline in two places.

    progress and retry_cache are file-backed, so a full restart of this
    function after a crash costs nothing: already-succeeded orders are
    skipped immediately via progress.is_done(), and anything still
    pending is exactly where the retry queue left it. The state temporal
    cache (job state/state_cache.json) is likewise file-backed: web
    lookups paid in any previous session are reused, never re-paid.
    """
    with sync_playwright() as p:
        # PERF: keep in-page timers (fast_waits' wait_for_function
        # polling) running at full speed even while the window is
        # occluded -- Chromium otherwise throttles background timers,
        # which would only slow the waits down, never change their
        # answers (see fast_waits.py's docstring).
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        context = None
        tracing_active = False
        try:
            context, page = login_with_session_cache(
                browser, LOGIN_URL, TARGET_URL, LOGGED_IN_SELECTOR,
                storage_path=str(AUTH_STORAGE_PATH),
                client_id=ACTIVE_WORKSPACE.client_id if ACTIVE_WORKSPACE else None,
                credential_reference=(
                    WorkspaceManager().credential_reference(ACTIVE_WORKSPACE.client_id)
                    if ACTIVE_WORKSPACE else None
                ),
                env_file=(
                    str(WorkspaceManager().shared_credentials_env_path())
                    if ACTIVE_WORKSPACE else None
                ),
                cache_file=(
                    str(ACTIVE_WORKSPACE.auth_dir / "credentials.json")
                    if ACTIVE_WORKSPACE else None
                ),
            )
        except MissingCredentialsError:
            try:
                browser.close()
            except Exception:
                pass
            raise
        print("Logged in and on the orders page.")

        # FIX (strategic speed upgrade): stop downloading fonts and known
        # third-party analytics/tracking scripts for the rest of this
        # session -- see _block_unnecessary_resources. Pure network-layer
        # policy, no change to what the script does or waits on.
        _block_unnecessary_resources(page)

        try:
            # FIX (Sep 2026): start closure-diagnostics tracing right after
            # login, once, for the whole run -- see _start_diagnostics_tracing.
            tracing_active = _start_diagnostics_tracing(context) if enable_tracing else False
            if not enable_tracing:
                print("[speed] --no-diagnostics passed -- Playwright tracing not "
                      "started this run. Closure screenshots/.txt/.json still get "
                      "written if anything closes unexpectedly; only the extra "
                      "saved .zip trace is skipped.", flush=True)

            page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=40000)
            catalog = _load_catalog(page, lock_mode, refresh_catalog=refresh_catalog)
            if not catalog:
                raise RuntimeError(
                    "could not read any products from the Add Order wizard -- "
                    "check that the page finished loading and the selectors in "
                    "field_selectors.py / scrape_crm_catalog() are still correct."
                )
            print(f"Loaded {len(catalog)} sellable product(s) from the live CRM catalog.\n")

            cfg = AutomationConfig(
                crm_catalog=catalog, select_retries=3,
                diagnostics_dir=str(ACTIVE_WORKSPACE.diagnostics_dir)
                if ACTIVE_WORKSPACE else "recordings/closure_diagnostics",
            )

            plans: dict[int, ExecutionPlan] = {}
            deterministic_failures: dict[int, str] = {}
            # PERF (Oct 2026, "very long waiting ... already done should not
            # be checked in the real sheet again"): planning (product match +
            # state inference, incl. the web lookup for unresolvable
            # addresses) used to run for EVERY row -- including the dozens
            # already marked done, whose plans were then thrown away when
            # _attempt_order hit its is_done() SKIP branch. Filter them out
            # FIRST in one cheap SQLite pass, and plan only the rows that can
            # actually be attempted. Plans stay keyed by original 1-based row
            # number, so the drains, the main loop, and all logging are
            # unchanged. The full `orders` list is still passed to both
            # drains below -- they need it for _match_fresh_order's
            # edited/removed-row detection, which is all-local and costs
            # nothing measurable.
            todo: list[tuple[int, dict]] = [
                (row_number, order)
                for row_number, order in enumerate(orders, start=1)
                if not (progress.is_done(order) or job_progress.is_done(order))
            ]
            skipped_done = len(orders) - len(todo)
            if skipped_done:
                print(f"[plan] {skipped_done} order(s) already marked done -- "
                      f"planning + uploading only the remaining {len(todo)}.",
                      flush=True)
            if not todo:
                print("[plan] everything in this file is already marked done -- "
                      "nothing to plan or upload this session.", flush=True)
            # FIX (Oct 2026, "temporal cache ... + very fast search ... 200
            # orders"): pre-resolve every todo-row address the local tables +
            # disk cache can't answer, ONCE, in PARALLEL (x8 threads), BEFORE
            # the planning loop -- so planning itself never blocks on the
            # network row-by-row and each address's extraction is logged with
            # its source. The loop below then hits a warm cache every time.
            _prewarm_todo_state_cache(todo)
            for row_number, order in todo:
                try:
                    plans[row_number] = build_execution_plan(
                        order, catalog, state_fallback=cfg.state_fallback
                    )
                except PlanningError as exc:
                    deterministic_failures[row_number] = str(exc)

            # Persist the temporal cache right after planning: whatever the
            # pre-warm + planning loop resolved is on disk even if the
            # browser session crashes mid-upload (Ctrl+C safety comes from
            # the finally-block save below).
            try:
                saved = save_state_cache(PROGRESS_DIR)
                if saved:
                    print(f"[state] saved {saved} address->state "
                          f"resolution(s) to the job cache.", flush=True)
            except Exception:
                pass

            # FIX (Sep 2026, "try all failed uploads FIRST on every fresh
            # run"): the failed-orders ledger drains before anything else --
            # before the retry queue and before any new row is touched.
            _drain_failed_ledger(page, cfg, orders, progress, job_progress, retry_cache,
                                 failed_ledger, failure_log, failed_rows, counters,
                                 flagged_rows, label="failed in a previous run",
                                 lock_mode=lock_mode)
            _drain_retry_queue(page, cfg, orders, progress, job_progress, retry_cache, failed_ledger,
                               failure_log, failed_rows,
                               counters, flagged_rows, label="carried over from a previous run",
                               lock_mode=lock_mode)

            # FIX (per request, "bigger Excel data (100+) starts lagging"):
            # draining the retry queue after EVERY single successful order
            # means every success pays the full cost of re-reading/re-writing
            # the persistent retry-queue file, on top of whatever work the
            # queue itself has pending -- a cost that grows as the run goes
            # on, so total overhead compounds roughly with the square of the
            # order count instead of scaling with it directly. Draining
            # periodically (plus always at the very end, unchanged) keeps
            # that cost flat regardless of how many orders are in the file.
            drain_every = 10
            successes_since_drain = 0

            # PERF (Oct 2026, same "already done" fix as the planning loop
            # above): walk only the rows that can actually be attempted --
            # done rows were already filtered into `todo`, so the main loop
            # never pays the per-row print + is_done() + SKIP-branch cost for
            # work that finished in a previous run. Row numbers (i) are the
            # original 1-based positions, so all logging still lines up with
            # the Excel sheet.
            for i, order in todo:
                print(f"[{i}/{len(orders)}] Processing {order.get('customer_name')!r}...", flush=True)

                if i in deterministic_failures:
                    if lock_mode:
                        print(f"    [lock] row {i}: permanent data problem, not "
                              f"the browser or the network -- {deterministic_failures[i]} "
                              f"-- retrying this specific row can never succeed on "
                              f"identical input, so it is logged as failed once rather "
                              f"than spun on forever. Every OTHER row keeps being "
                              f"processed normally.", flush=True)
                    result = process_order(page, order, cfg, row_number=i)
                    result.reason = deterministic_failures[i]
                    counters["failed"] += 1
                    failed_order = to_failed_order(order, result)
                    failure_log.log(failed_order)
                    failed_rows.append(failed_order)
                    continue

                succeeded = _attempt_order(
                    page, cfg, order, i, progress, job_progress, retry_cache, failed_ledger,
                    failure_log, failed_rows, counters, flagged_rows, plan=plans[i],
                    lock_mode=lock_mode
                )

                if succeeded:
                    successes_since_drain += 1
                    if successes_since_drain >= drain_every:
                        _drain_retry_queue(page, cfg, orders, progress, job_progress, retry_cache,
                                           failed_ledger, failure_log, failed_rows,
                                           counters, flagged_rows,
                                           label=f"periodic drain after {drain_every} successes",
                                           lock_mode=lock_mode)
                        successes_since_drain = 0

            _drain_retry_queue(page, cfg, orders, progress, job_progress, retry_cache, failed_ledger,
                               failure_log, failed_rows, counters, flagged_rows,
                               label="final sweep", lock_mode=lock_mode)
        finally:
            # Ctrl+C / crash safety: persist any state resolutions learned
            # this session (pre-warm, planning loop, drain retries) so the
            # next run reuses them instead of re-paying the lookups.
            try:
                save_state_cache(PROGRESS_DIR)
            except Exception:
                pass
            _stop_diagnostics_tracing(context, tracing_active)
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def _run_sharded(workspace: "ClientWorkspace", workers: int, lock_mode: bool,
                  refresh_catalog: bool, enable_tracing: bool,
                  refresh_state_cache: bool = False) -> None:
    """PERF (--workers N): split one client job across N independent
    browser processes, each owning a disjoint, round-robin slice of the
    order rows (row i -> shard i % N). The CRM is a hosted SPA, so the
    per-order wall time is dominated by network round trips; N workers
    overlap those waits and finish a big file in roughly 1/N the time
    without changing ANY per-order logic.

    Persistence guarantees are preserved exactly:
      - progress DBs are SQLite (WAL) -- concurrent mark_done() from N
        workers serializes safely; the client-level DB stays the dedup
        source of truth.
      - the persistent retry queue is PARTITIONED into per-shard files
        (key hash -> shard) so two workers can never attempt the same
        queued order concurrently, and the parent merges every shard
        file back into the main queue when the workers exit. A crash
        between partition and merge is recovered on the next run by
        collect_and_merge_shard_queues() (called from _run_upload).
      - duplicate fingerprints are dropped up front: parallel workers
        would otherwise both pass the is_done() check before either
        marks the order done and submit it twice (serial mode never had
        this race).
      - this parent holds the job + auth locks for the whole duration;
        shard children (--shard) skip locking entirely.
    """
    _configure_workspace(workspace)
    orders = load_orders(str(workspace.source_path))
    seen_keys: set[str] = set()
    unique_orders: list[dict] = []
    duplicates = 0
    for order in orders:
        try:
            key = order_key(order)
        except Exception:
            unique_orders.append(order)
            continue
        if key in seen_keys:
            duplicates += 1
            continue
        seen_keys.add(key)
        unique_orders.append(order)
    if duplicates:
        print(f"[shard] dropped {duplicates} duplicate row(s) that parallel "
              f"workers would otherwise submit twice.", flush=True)

    partition_queue_into_shards(PROGRESS_DIR,
                                PROGRESS_DIR / "retry_queue.json", workers)
    partition_ledger_into_shards(PROGRESS_DIR,
                                 PROGRESS_DIR / "failed_orders.json", workers)

    processes = []
    for i in range(workers):
        command = [
            sys.executable, str(Path(__file__).resolve()),
            "--client", workspace.client_id,
            "--job", workspace.job_id,
            "--shard", f"{i}/{workers}",
        ]
        if lock_mode:
            command.append("--lock")
        if refresh_catalog:
            command.append("--refresh-catalog")
        if refresh_state_cache:
            command.append("--refresh-state-cache")
        if not enable_tracing:
            command.append("--no-diagnostics")
        print(f"[shard] launching worker {i + 1}/{workers} in a separate "
              f"browser process", flush=True)
        processes.append(subprocess.Popen(command))

    failed = 0
    try:
        for i, process in enumerate(processes):
            code = process.wait()
            if code:
                failed += 1
                print(f"[shard] worker {i + 1}/{workers} exited with code "
                      f"{code}", flush=True)
    except KeyboardInterrupt:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        raise
    finally:
        merged = collect_and_merge_shard_queues(
            PROGRESS_DIR, PROGRESS_DIR / "retry_queue.json")
        if merged:
            print(f"[shard] merged {merged} shard queue file(s) back into "
                  f"the main retry queue.", flush=True)
        merged_ledgers = collect_and_merge_shard_ledgers(
            PROGRESS_DIR, PROGRESS_DIR / "failed_orders.json")
        if merged_ledgers:
            print(f"[shard] merged {merged_ledgers} shard failed-ledger file(s) "
                  f"back into the main ledger.", flush=True)
    if failed:
        print(f"[shard] {failed}/{workers} worker(s) failed; check this "
              f"job's run logs.", flush=True)
    else:
        print(f"[shard] all {workers} worker(s) finished.", flush=True)


def _run_upload(lock_mode: bool = False, refresh_catalog: bool = False,
         enable_tracing: bool = True, client: str | None = None,
         source: str | None = None, job: str | None = None,
         dry_run: bool = False, skip_done: bool = True,
         workers: int = 1, shard: tuple[int, int] | None = None,
         refresh_state_cache: bool = False) -> None:
    if client:
        manager = WorkspaceManager()
        if job:
            workspace = manager.workspace(client, job)
        else:
            input_path = Path(source) if source else None
            if input_path is None:
                raise ValueError("--source is required when creating a new client job")
            # Explicit sources are copied into the immutable job envelope.
            inbox_copy = manager.root / "_inbox" / (
                f".{client}_{os.getpid()}_{time.time_ns()}{input_path.suffix.lower()}"
            )
            inbox_copy.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(input_path, inbox_copy)
            workspace = manager.import_inbox(inbox_copy, client_id=client)
        _configure_workspace(workspace)
        print(f"Client: {workspace.client_id}  Job: {workspace.job_id}")
        print(f"Immutable source: {workspace.source_path}")
        orders_path = workspace.source_path
    else:
        # Legacy single-client layout remains fully supported.
        orders_path = Path(source) if source else find_orders_file()
    orders = load_orders(str(orders_path))
    print(f"Loaded {len(orders)} order(s) from {orders_path}")

    if shard is not None:
        shard_index, shard_count = shard
        before = len(orders)
        orders = orders[shard_index::shard_count]
        print(f"[shard] worker {shard_index + 1}/{shard_count}: processing "
              f"{len(orders)} of {before} order(s) (round-robin slice).")

    if dry_run:
        print(f"Dry run: validated {len(orders)} order(s); no browser or uploads started.")
        return {}

    progress_path = (
        ACTIVE_WORKSPACE.client_progress_db
        if ACTIVE_WORKSPACE else PROGRESS_DIR / "progress.db"
    )
    progress = ProgressTracker(path=str(progress_path))
    job_progress = ProgressTracker(path=str(PROGRESS_DIR / "progress.db"))
    # One-way, idempotent compatibility bridge for the former layout.
    legacy_progress = PROGRESS_DIR / "processed_orders.json"
    if not progress.count and legacy_progress.exists():
        imported = progress.import_from_json_backup(str(legacy_progress))
        if imported:
            print(f"[migration] imported {imported} legacy progress key(s) safely.")
    # PERF (--workers sharding): a shard worker owns its own queue file
    # so two workers can never attempt the same queued order
    # concurrently; the parent merges every shard file back into the
    # main persistent queue when the workers exit. A parent (shard=None)
    # also recovers shard files orphaned by a crashed previous run.
    if shard is not None:
        shard_queue_path = _shard_queue_path(PROGRESS_DIR, shard[0], shard[1])
    else:
        shard_queue_path = PROGRESS_DIR / "retry_queue.json"
        merged_orphans = collect_and_merge_shard_queues(
            PROGRESS_DIR, PROGRESS_DIR / "retry_queue.json")
        if merged_orphans:
            print(f"[shard] merged {merged_orphans} leftover shard queue "
                  f"file(s) from a previous run into the main retry queue.")
    retry_cache = RetryCache(path=str(shard_queue_path))
    if shard is not None:
        shard_ledger_path = _shard_ledger_path(PROGRESS_DIR, shard[0], shard[1])
    else:
        shard_ledger_path = PROGRESS_DIR / "failed_orders.json"
        merged_orphan_ledgers = collect_and_merge_shard_ledgers(
            PROGRESS_DIR, PROGRESS_DIR / "failed_orders.json")
        if merged_orphan_ledgers:
            print(f"[shard] merged {merged_orphan_ledgers} leftover shard "
                  f"failed-ledger file(s) from a previous run into the main ledger.")
    failed_ledger = FailedLedger(path=str(shard_ledger_path))

    # FIX (Oct 2026, "repeating on each run ... temporal cache for the whole
    # lifecycle of a particular job"): seed the state-search cache from the
    # job's state_cache.json so web lookups paid on a previous run are never
    # re-paid. --refresh-state-cache forces re-resolution (same role as
    # --refresh-catalog for the product list).
    from apps.sniper.product_upload import state_engine as _state_engine
    _state_engine.load_state_cache(PROGRESS_DIR)
    if refresh_state_cache:
        for _key in list(_state_engine._SEARCH_CACHE):
            _state_engine._SEARCH_CACHE.pop(_key, None)
            _state_engine._SEARCH_CACHE_META.pop(_key, None)
        try:
            (Path(PROGRESS_DIR) / _state_engine.STATE_CACHE_FILENAME).unlink()
        except Exception:
            pass
        print("[state] --refresh-state-cache passed -- ignoring any cached "
              "address->state resolutions and re-resolving from scratch.",
              flush=True)

    already_done_count = sum(1 for o in orders if progress.is_done(o))
    if already_done_count:
        print(f"{already_done_count} order(s) already marked done in a previous run "
              f"({progress.count} total tracked as done) -- will be skipped automatically.")
    if retry_cache.count:
        print(f"{retry_cache.count} order(s) left over in the retry queue from a previous run "
              f"-- will retry those first.")
    if failed_ledger.count:
        print(f"{failed_ledger.count} order(s) recorded as failed in a previous run -- "
              f"will retry those first, before any new rows.")

    session_name = f"add_products_{int(time.time())}"
    if shard is not None:
        # Parallel shard workers must not overwrite each other's failure
        # exports -- suffix the session name and the fixed CSV paths.
        session_name += f"_shard{shard[0]}of{shard[1]}"
        global MANUAL_CHECK_PATH, FUZZY_REVIEW_PATH
        MANUAL_CHECK_PATH = MANUAL_CHECK_DIR / (
            f"failed_exported.shard{shard[0]}of{shard[1]}.csv")
        FUZZY_REVIEW_PATH = MANUAL_CHECK_DIR / (
            f"fuzzy_matches_for_review.shard{shard[0]}of{shard[1]}.csv")
    failure_log = FailureLogger(
        output_dir=str(ACTIVE_WORKSPACE.run_dir if ACTIVE_WORKSPACE else "recordings"),
        session_name=session_name,
    )

    # These accumulate across every internal session restart in --lock
    # mode, not just the last one -- so the final summary below reflects
    # the WHOLE run, however many times the browser session itself had
    # to be relaunched along the way.
    counters = {"succeeded": 0, "failed": 0, "queued": 0, "skipped_duplicate": 0,
                "flagged_for_review": 0}
    failed_rows: list[FailedOrder] = []
    flagged_rows: list[FlaggedForReview] = []

    if workers > 1 and shard is None and not dry_run:
        if ACTIVE_WORKSPACE is None:
            raise ValueError("--workers requires --client (a managed job); "
                             "the legacy customers\\data layout is "
                             "single-session only.")
        _run_sharded(ACTIVE_WORKSPACE, workers, lock_mode, refresh_catalog,
                     enable_tracing)
        return {}

    if lock_mode:
        # FIX (per request, "nothing should stop the code when locked,
        # only Ctrl+C"): anything that escapes _run_session -- a browser
        # crash, a dropped connection, an unhandled Playwright error --
        # is caught here and the whole session is relaunched from
        # scratch. KeyboardInterrupt (Ctrl+C) is the one thing NOT caught
        # by `except Exception`, so it is the only way to stop this loop.
        attempt = 0
        while True:
            attempt += 1
            try:
                _run_session(lock_mode, orders, progress, job_progress, retry_cache,
                             failed_ledger, failure_log,
                             counters, failed_rows, flagged_rows, refresh_catalog=refresh_catalog,
                             enable_tracing=enable_tracing)
                break
            except KeyboardInterrupt:
                raise
            except MissingCredentialsError as e:
                print(f"[lock] session attempt {attempt}: missing/invalid credentials -- "
                      f"{e} -- this needs a human to fix the credentials; retrying login "
                      f"immediately in case they already have been. Press Ctrl+C to stop.",
                      flush=True)
                continue
            except Exception as e:
                print(f"[lock] session attempt {attempt} crashed: {e!r} -- relaunching a "
                      f"fresh browser session and resuming (already-completed orders are "
                      f"skipped, queued ones pick up where they left off). Press Ctrl+C "
                      f"to stop.", flush=True)
                continue
    else:
        try:
            _run_session(lock_mode, orders, progress, job_progress, retry_cache,
                         failed_ledger, failure_log,
                         counters, failed_rows, flagged_rows, refresh_catalog=refresh_catalog,
                         enable_tracing=enable_tracing)
        except MissingCredentialsError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    print(f"\nDone. {counters['succeeded']} succeeded, {counters['failed']} failed, "
          f"{counters['skipped_duplicate']} skipped as already-done duplicates, "
          f"{retry_cache.count} still queued for retry and {failed_ledger.count} recorded "
          f"as failed for the next run.")
    print_performance_summary(counters)

    print(progress.summarize_run())
    mismatch = progress.verify_final_state()
    if mismatch:
        print(f"[progress] FINAL VERIFICATION FAILED: {mismatch}")
    else:
        print(f"[progress] Final verification OK -- all {progress.count} "
              f"tracked key(s) confirmed present in db.")

    if counters["failed"]:
        print(f"Failed orders exported to: {failure_log.txt_path}")
        print(f"                      and: {failure_log.jsonl_path}")

        manual_check_path = export_manual_check(failed_rows)
        print(f"                      and: {manual_check_path}")

    if counters["flagged_for_review"]:
        print(f"{counters['flagged_for_review']} order(s) uploaded at reduced "
              f"product-match confidence -- flagged for manual review.")
        fuzzy_review_path = export_fuzzy_review(flagged_rows)
        print(f"Flagged orders exported to: {fuzzy_review_path}")

    # FIX (Sep 2026, "file in use" / authorization noise at job completion):
    # release the SQLite handles NOW. Both progress DBs are opened with WAL
    # journaling, and job_progress's files live INSIDE the job directory
    # that archive_completed_job() shutil.move()s right after this returns
    # -- an open connection there made every completion-time archive race
    # Windows' "file in use" error, which in turn downgraded a
    # fully-succeeded job to "failed" and made the next run re-open it.
    # Nothing below this point reads either tracker again.
    for _tracker in (progress, job_progress):
        try:
            _tracker.close()
        except Exception:
            pass
    return counters


def _json_entry_count(path) -> int:
    """Defensive reader for the completion-time gating: returns how many
    entries a durable pending-state file holds (retry queue or failed
    ledger), or 0 for a missing/unreadable/corrupt file. Failing to
    re-read these files here must never raise and must never downgrade a
    fully-succeeded job to "failed" -- that downgrade was exactly how the
    noisy completion logs poisoned the next run into re-opening completed
    jobs."""
    path = Path(path)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0


def main(lock_mode: bool = False, refresh_catalog: bool = False,
         enable_tracing: bool = True, client: str | None = None,
         source: str | None = None, job: str | None = None,
         dry_run: bool = False, skip_done: bool = True,
         legacy: bool = False,
         skip: tuple[str, ...] = (),
         workers: int = 1, shard: tuple[int, int] | None = None,
         refresh_state_cache: bool = False) -> None:
    """Run one explicit job or drain the inbox sequentially.

    The upload engine is intentionally invoked once per workspace. A completed
    workspace returns normally and its browser is closed before the next one
    starts, so one bad client cannot mix state with another client.
    """
    manager = WorkspaceManager()

    def execute_workspace(workspace: ClientWorkspace,
                          shard: tuple[int, int] | None = None) -> None:
        if shard is not None:
            # Shard worker child: the parent owns the job/auth locks and
            # the final job-status transition -- just run this shard's
            # slice, then exit. No locks, no status writes, no cleanup.
            _run_upload(lock_mode, refresh_catalog, enable_tracing,
                        workspace.client_id, None, workspace.job_id,
                        dry_run, skip_done, shard=shard,
                        refresh_state_cache=refresh_state_cache)
            return
        try:
            manager.validate_client_ready(workspace.client_id)
        except RuntimeError as exc:
            manager.set_job_status(
                workspace.client_id,
                workspace.job_id,
                "manual_review",
                error=str(exc),
            )
            print(
                f"[blocked] client {workspace.client_id} job {workspace.job_id}: "
                f"{exc}",
                flush=True,
            )
            return
        manager.set_job_status(workspace.client_id, workspace.job_id, "running",
                               run_id=workspace.run_dir.name)
        counts: dict | None = None
        try:
            with JobLock(workspace.job_lock_path):
                manager.prepare_run(workspace)
                # A client may have multiple jobs, but its browser session and
                # catalog cache are shared; serialize those client resources.
                with JobLock(workspace.auth_dir / "auth.lock"):
                    if workers > 1 and not dry_run:
                        _run_sharded(workspace, workers, lock_mode,
                                     refresh_catalog, enable_tracing,
                                     refresh_state_cache)
                    else:
                        counts = _run_upload(lock_mode, refresh_catalog, enable_tracing,
                                             workspace.client_id, None, workspace.job_id,
                                             dry_run, skip_done, workers=workers,
                                             refresh_state_cache=refresh_state_cache)
            if dry_run:
                manager.set_job_status(
                    workspace.client_id,
                    workspace.job_id,
                    "imported",
                    dry_run_validated_at=time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                )
                print(
                    f"[dry-run] client {workspace.client_id} job "
                    f"{workspace.job_id} remains pending; no upload was performed.",
                    flush=True,
                )
                return
            # FIX (Sep 2026, "shouldn't mark completed if there is a failed
            # customer yet to be uploaded" + "file in use" noise at
            # completion): completion reads BOTH durable pending files
            # defensively (_json_entry_count -- a missing/corrupt one counts
            # as zero instead of raising and downgrading a succeeded job to
            # "failed"), and "completed" is only ever reached when NEITHER
            # the retry queue NOR the failed-orders ledger still holds an
            # un-uploaded customer.
            pending_retry_entries = _json_entry_count(
                workspace.progress_dir / "retry_queue.json")
            pending_failed = _json_entry_count(
                workspace.progress_dir / "failed_orders.json")
            if pending_retry_entries or pending_failed:
                manager.set_job_status(workspace.client_id, workspace.job_id,
                                       "partial", pending_retries=True,
                                       pending_retry_entries=pending_retry_entries,
                                       pending_failed=pending_failed)
                print(
                    f"[status] job {workspace.job_id} NOT completed: "
                    f"{pending_failed} failed customer(s) still to be uploaded"
                    + (f" and {pending_retry_entries} queued retry order(s)"
                       if pending_retry_entries else "") + ".",
                    flush=True,
                )
            else:
                completed_extras: dict = {}
                if counts is not None:
                    completed_extras["succeeded"] = counts.get("succeeded", 0)
                    completed_extras["failed"] = counts.get("failed", 0)
                manager.set_job_status(workspace.client_id, workspace.job_id,
                                       "completed", **completed_extras)
                try:
                    archived = manager.archive_completed_job(
                        workspace.client_id, workspace.job_id
                    )
                except FileExistsError:
                    archived = None
                    print(
                        f"[archive] job {workspace.job_id} already has an archived "
                        f"copy; leaving the live directory in place.",
                        flush=True,
                    )
                if archived:
                    print(
                        f"[archive] completed job {workspace.job_id} moved to {archived}",
                        flush=True,
                    )
                else:
                    # archive_completed_job defers instead of raising when the
                    # move is still blocked by some file handle -- the job
                    # STAYS "completed"; a later run archives it without a
                    # browser (retry_deferred_archives).
                    print(
                        f"[archive] job {workspace.job_id} is completed; archiving "
                        f"deferred (a file handle was still busy) -- it will be "
                        f"archived on a later run without opening a browser.",
                        flush=True,
                    )
        except KeyboardInterrupt:
            manager.set_job_status(workspace.client_id, workspace.job_id, "interrupted")
            raise
        except Exception as exc:
            manager.set_job_status(workspace.client_id, workspace.job_id, "failed",
                                   error=repr(exc))
            raise
        finally:
            manager.cleanup_run(workspace)

    if client or source or job:
        if client and client in skip:
            print(f"[batch] skipping client {client}")
            return
        if client and not job:
            if not source:
                client_jobs = [
                    workspace for workspace in manager.discover_pending_jobs()
                    if workspace.client_id.lower() == client.lower()
                ]
                if not client_jobs:
                    raise FileNotFoundError(
                        f"No pending managed jobs found for client {client!r}."
                    )
                for workspace in client_jobs:
                    execute_workspace(workspace)
                return
            input_path = Path(source).resolve()
            inbox_copy = manager.root / "_inbox" / (
                f".{client}_{os.getpid()}_{time.time_ns()}{input_path.suffix.lower()}"
            )
            import shutil
            shutil.copy2(input_path, inbox_copy)
            workspace = manager.import_inbox(inbox_copy, client_id=client)
            job = workspace.job_id
            source = None
        if client and job:
            # FIX (Sep 2026, "log completed jobs and stop without opening the
            # browser if it is truly completed"): an explicit --client/--job
            # run for a genuinely finished job (status completed, empty retry
            # queue, empty failed ledger -- live or archived) logs the
            # completion cleanly and returns WITHOUT launching a browser.
            summary = manager.completed_job_summary(client, job)
            if summary is not None and not manager.job_has_pending_uploads(client, job):
                print(f"[completed] {summary}", flush=True)
                print("[completed] nothing left to upload -- no browser was opened.",
                      flush=True)
                return
            workspace = manager.workspace(client, job)
            return execute_workspace(workspace, shard=shard)
        return _run_upload(lock_mode, refresh_catalog, enable_tracing, client,
                           source, job, dry_run, skip_done, workers=workers,
                           shard=shard, refresh_state_cache=refresh_state_cache)

    skip_values = {item.lower() for item in skip}
    manager.recover_claimed_files()
    files = []
    for candidate in manager.discover_inbox():
        name_matches = {candidate.stem.lower()}
        try:
            embedded_client = manager.client_id_from_filename(candidate.name)
            if embedded_client:
                name_matches.add(embedded_client.lower())
            else:
                name_matches.add(manager.resolve_canonical_client_id(
                    name_key_from_filename(candidate.name), allow_create=False
                ).lower())
        except (KeyError, ValueError):
            pass
        if not name_matches.intersection(skip_values):
            files.append(candidate)
    pending_jobs = []
    if not files and not legacy:
        # FIX (Sep 2026, "on each run, log all completed jobs"): first
        # finish any archive moves that a busy file handle deferred on a
        # previous completion (never opens a browser), then log every
        # completed job -- live or archived -- before deciding what still
        # needs work.
        manager.retry_deferred_archives()
        completed_summaries = manager.completed_job_summaries()
        if completed_summaries:
            print(f"[batch] completed job log ({len(completed_summaries)} job(s)):",
                  flush=True)
            for line in completed_summaries:
                print(f"  - {line}", flush=True)
        pending_jobs = manager.discover_pending_jobs()
        pending_jobs = [
            workspace for workspace in pending_jobs
            if workspace.client_id.lower() not in skip_values
            and workspace.job_id.lower() not in skip_values
        ]
        if pending_jobs:
            print(
                f"[batch] found {len(pending_jobs)} managed job(s) in client "
                "job directories; validating readiness before browser launch.",
                flush=True,
            )
    files, discarded_files = manager.select_latest_inbox_files(files)
    for discarded in discarded_files:
        rejected = manager.root / "_rejected" / discarded.name
        rejected.parent.mkdir(parents=True, exist_ok=True)
        if discarded.exists():
            import shutil
            shutil.move(str(discarded), str(rejected))
        print(
            f"[rejected] older same-client intake file discarded: {discarded.name}",
            flush=True,
        )

    if not files and not pending_jobs:
        if not legacy and manager._load_registry().get("name_keys"):
            print(
                "[batch] no new intake files or pending client jobs found; "
                "managed multi-client mode did not fall back to the legacy "
                "customers\\data workflow.",
                flush=True,
            )
            print(
                "[batch] place new files in clients\\_inbox or "
                "customers\\data\\waiting, or use --client/--job for an "
                "existing job. Use --legacy only for the old customers\\data "
                "workflow.",
                flush=True,
            )
            return
        return _run_upload(lock_mode, refresh_catalog, enable_tracing,
                           None, None, None, dry_run, skip_done,
                           refresh_state_cache=refresh_state_cache)
    workspaces = list(pending_jobs)
    for inbox_file in files:
        claimed_file = manager.claim_inbox_file(inbox_file)
        if claimed_file is None:
            continue
        try:
            workspace = manager.import_inbox(claimed_file)
        except Exception as exc:
            rejected = manager.root / "_rejected" / claimed_file.name
            rejected.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            if claimed_file.exists():
                shutil.move(str(claimed_file), str(rejected))
            print(f"[batch] rejected {claimed_file.name}: {exc}", flush=True)
            continue
        workspaces.append(workspace)

    if not workspaces:
        return

    ready_workspaces = []
    for workspace in workspaces:
        try:
            manager.validate_client_ready(workspace.client_id)
        except RuntimeError as exc:
            manager.set_job_status(
                workspace.client_id,
                workspace.job_id,
                "manual_review",
                error=str(exc),
            )
            print(
                f"[blocked] client {workspace.client_id} job {workspace.job_id}: "
                f"{exc}",
                flush=True,
            )
            continue
        ready_workspaces.append(workspace)
    workspaces = ready_workspaces
    if not workspaces:
        print(
            "[batch] no client jobs are ready. Configure the blocked client "
            "profiles, then run the command again.",
            flush=True,
        )
        return

    # Each worker owns its Playwright browser and module-level workspace paths.
    # Separate processes allow all client browsers to run concurrently without
    # mixing authentication, progress, or report state.
    processes = []
    for workspace in workspaces:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--client", workspace.client_id,
            "--job", workspace.job_id,
        ]
        if lock_mode:
            command.append("--lock")
        if refresh_catalog:
            command.append("--refresh-catalog")
        if refresh_state_cache:
            command.append("--refresh-state-cache")
        if not enable_tracing:
            command.append("--no-diagnostics")
        if dry_run:
            command.append("--dry-run")
        if workers > 1:
            command.extend(["--workers", str(workers)])
        print(
            f"[batch] launching client {workspace.client_id} job "
            f"{workspace.job_id} in a separate browser process",
            flush=True,
        )
        processes.append((workspace, subprocess.Popen(command)))

    failed = False
    try:
        for workspace, process in processes:
            return_code = process.wait()
            if return_code:
                failed = True
                print(
                    f"[batch] client {workspace.client_id} job {workspace.job_id} "
                    f"exited with code {return_code}",
                    flush=True,
                )
    except KeyboardInterrupt:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        raise
    if failed:
        print("[batch] one or more client workers failed; inspect each job status.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Sniper order automation.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--lock",
        action="store_true",
        help="Retry transient/loading errors forever; reload after every 3 failures.",
    )
    mode.add_argument(
        "--easy",
        action="store_true",
        help="Run once with normal retry limits (the default).",
    )
    parser.add_argument(
        "--refresh-catalog",
        action="store_true",
        help="Ignore any cached product catalog (customers/progress/catalog_cache.json) "
             "and re-scrape it live from the Add Order wizard, overwriting the cache. "
             "Use this after the product list actually changes on the site; otherwise "
             "the cache is trusted as-is and the live scrape is skipped entirely.",
    )
    parser.add_argument(
        "--refresh-state-cache",
        action="store_true",
        help="Ignore any cached address->state resolutions "
             "(job state/state_cache.json) and re-resolve from scratch. "
             "Use this after the local state tables gain new aliases; "
             "otherwise the cache is trusted as-is and web lookups paid on "
             "a previous run are never re-paid.",
    )
    parser.add_argument(
        "--no-diagnostics",
        action="store_true",
        help="Skip starting Playwright tracing for this run, for a bit of extra speed "
             "on every action. Closure screenshots/.txt/.json evidence is unaffected "
             "and still written if anything closes unexpectedly -- only the extra "
             "saved .zip trace is skipped.",
    )
    parser.add_argument("--client", help="Canonical client ID (enables multi-client job layout).")
    parser.add_argument("--source", help="CSV/XLSX source; copied into the job as immutable input.")
    parser.add_argument("--job", help="Resume an existing job for --client.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and plan input without opening a browser or uploading.")
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Explicitly run the original single-client customers\\data workflow "
             "when no managed inbox file is available.",
    )
    parser.add_argument("--skip", default="",
                        help="Comma-separated client IDs or inbox filename stems to skip in batch mode.")
    parser.add_argument("--skip-done", action="store_true", default=True,
                        help="Skip orders already present in the durable progress database (default).")
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Split this client job across N parallel browser processes "
             "(requires --client/--job). Each worker owns a disjoint "
             "slice of the rows and its own retry-queue shard; progress "
             "and retry persistence are shared/merged exactly as before.")
    parser.add_argument("--shard", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    print("[lock] persistent retry mode enabled; press Ctrl+C to stop." if args.lock
          else "[easy] one-pass mode enabled.", flush=True)
    shard = None
    if args.shard:
        try:
            shard_index_s, shard_count_s = args.shard.split("/", 1)
            shard = (int(shard_index_s), int(shard_count_s))
        except ValueError:
            raise SystemExit(f"--shard must look like '0/4', got {args.shard!r}")
    main(lock_mode=args.lock, refresh_catalog=args.refresh_catalog,
         refresh_state_cache=args.refresh_state_cache,
         enable_tracing=not args.no_diagnostics, client=args.client,
         source=args.source, job=args.job, dry_run=args.dry_run,
         skip_done=args.skip_done,
         legacy=args.legacy,
         skip=tuple(item.strip() for item in args.skip.split(",") if item.strip()),
         workers=max(1, args.workers), shard=shard)
