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

# ==============================================================
# PATH SETUP — MUST BE FIRST
# ==============================================================
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# ==============================================================


import argparse
import dataclasses
import json
import os
import signal
import subprocess
import statistics
import time

from datetime import datetime, timedelta, timezone
from typing import Optional

from apps.sniper.production_tracker import ProductionFailedLedger, ProductionRetryCache

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

# How often a ``--workers`` shard re-checks the shared claim queue while it is
# idly waiting out the steal delay. This is now only the floor the exact
# remaining-grace wait falls back to (see _iter_claimed_rows) -- the wait is
# sized to the steal deadline itself, so this is an anti-busy-spin guard rather
# than the pacing it used to be. Only reached when a peer is holding the
# remaining rows, so it costs nothing on the normal path.
CLAIM_IDLE_POLL_S = 0.02


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


def _seed_shard_auth_storage(workspace: ClientWorkspace,
                             shard: tuple[int, int]) -> Path:
    """Return a shard worker's private session-state path.

    Shard 0 keeps owning the canonical client session file so it can refresh
    it for future runs. Higher-numbered workers get their own subdirectory and
    are seeded from the canonical file when it exists. This prevents concurrent
    ``storage_state()`` writes and metadata updates from racing each other
    while preserving session reuse.
    """
    canonical = workspace.auth_dir / "session_state.json"
    shard_index, shard_count = shard
    if shard_index == 0:
        return canonical
    worker_dir = workspace.auth_dir / "workers" / f"shard{shard_index}of{shard_count}"
    worker_dir.mkdir(parents=True, exist_ok=True)
    worker_storage = worker_dir / "session_state.json"
    if not worker_storage.exists() and canonical.exists():
        import shutil
        shutil.copy2(canonical, worker_storage)
    return worker_storage

ORDER_FILE_EXTENSIONS = (".csv", ".xlsx", ".xls")

FILTER_COLUMN = "Filtered #"

REQUIRED_ORDER_FIELDS = ("customer_name", "phone_number", "product", "address", "price")


import pandas as pd
from playwright.sync_api import sync_playwright

from apps.sniper.nav_utils import safe_goto
from apps.sniper.utils.auth import login_with_session_cache, MissingCredentialsError
from apps.sniper.product_upload.add_products import (
    AutomationConfig, OrderResult, process_order, to_failed_order,
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
from apps.sniper.product_upload.planning import (
    ExecutionPlan, PlanningError, build_execution_plan,
)
from apps.sniper.product_upload import wait_profile
from apps.sniper.product_upload.closure_diagnostics import (
    enable_trace_rotation, maybe_rotate_trace_chunk,
)
from apps.sniper.client_workspace import (
    WorkspaceManager, ClientWorkspace, JobLock, name_key_from_filename,
    ARCHIVE_RETENTION_DAYS,
)
from apps.sniper.client_scheduler import ClientProcessScheduler, ScheduledJob
from apps.sniper.conditional_wait import poll_until
from apps.sniper.work_queue import OrderClaimQueue

# Production-mode imports (lazy-loaded only when --production is used).
# Keeping these separate from the core imports means local execution does
# NOT require libsql, gspread, or google-auth to be installed.
_PRODUCTION_TRACKER = None
_SHEETS_ADAPTER = None


def _import_production_modules():
    """Import production-only modules on first use. Raises ImportError with
    a clear message if the required packages are not installed."""
    global _PRODUCTION_TRACKER, _SHEETS_ADAPTER
    if _PRODUCTION_TRACKER is None:
        from apps.sniper.production_tracker import ProductionTracker, production_order_key
        _PRODUCTION_TRACKER = (ProductionTracker, production_order_key)
    if _SHEETS_ADAPTER is None:
        from apps.sniper.sheets_adapter import load_orders_from_sheets
        _SHEETS_ADAPTER = load_orders_from_sheets
    return *_PRODUCTION_TRACKER, _SHEETS_ADAPTER

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


# =============================================================================
# Missing-address counter (Oct 2026 speed pass)
# =============================================================================
# A blank address cell is a HUMAN problem, not an automation one. Policy:
#   - sighting 1 (any run): the row is NOT uploaded; a prominent
#     HUMAN ACTION NEEDED block in the cmd points at the exact cell
#     (Filtered # + Customer Name + Excel row) to fix;
#   - sighting 2 (the next run, still blank): the counter RESETS and the
#     row proceeds automatically with the "Lagos" address default --
#     business never blocks on it, and the human gets re-nudged every
#     second run if it stays unfixed.
# Counts persist per job in missing_address_counts.json (atomic tmp +
# os.replace, merge-on-write so --workers shard children can't wipe each
# other), keyed by order_key() -- which hashes customer+phone+product+price
# and NOT the address, so fixing the address is what clears the flag, never
# the flag itself.
MISSING_ADDRESS_COUNTS_FILENAME = "missing_address_counts.json"


def _missing_address_counts_path() -> Path:
    return PROGRESS_DIR / MISSING_ADDRESS_COUNTS_FILENAME


def _load_missing_address_counts() -> dict[str, int]:
    """Read the per-job blank-address sighting counts. Missing/corrupt file
    -> {} -- never raises, a corrupt file just restarts counting."""
    try:
        data = json.loads(
            _missing_address_counts_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key, count in data.items():
        try:
            out[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return out


def _save_missing_address_counts(counts: dict[str, int],
                                 remove_keys: frozenset[str] = frozenset()) -> None:
    """Persist sighting counts, MERGING with whatever is on disk first
    (another --workers shard child may have written disjoint keys since
    this process loaded). Last-write-wins per key, never per file. Keys in
    remove_keys are deleted from disk too -- that is how a sighting-2 RESET
    clears its flag instead of being resurrected by the merge. Atomic tmp +
    os.replace so a killed process never corrupts the file. Best-effort: a
    failed write must never fail a run."""
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        merged: dict[str, int] = {}
        try:
            disk = json.loads(_missing_address_counts_path().read_text(
                encoding="utf-8"))
            if isinstance(disk, dict):
                for key, count in disk.items():
                    try:
                        merged[str(key)] = int(count)
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass
        merged.update(counts)
        for key in remove_keys:
            merged.pop(str(key), None)
        tmp = _missing_address_counts_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=1, sort_keys=True),
                       encoding="utf-8")
        import os
        os.replace(tmp, _missing_address_counts_path())
    except Exception:
        pass


def _missing_address_flag(order: dict, row_number: int, count: int) -> None:
    """The cmd block a human needs: exactly where the blank Address cell is,
    what else is in the row (so it can be found even without the Filtered #
    column), and what happens next. Noisy ON PURPOSE."""
    sn = order.get("filtered_sn")
    target = (f"Filtered # = {sn}" if sn
              else f"Excel row = {order.get('excel_row', row_number)}")
    print("=" * 70, flush=True)
    print(f"[HUMAN ACTION NEEDED] row {row_number} - NO ADDRESS in the "
          f"source file - row NOT uploaded (sighting {count}/2).",
          flush=True)
    print(f"  Fix this cell in Excel:  Address  for  {target}  "
          f"|  Customer Name = {order.get('customer_name')!r}", flush=True)
    print(f"  Other fields of this row: phone="
          f"{order.get('phone_number')!r} | product="
          f"{order.get('product')!r} | price={order.get('price')!r}",
          flush=True)
    print("  Then save the file and run again.", flush=True)
    print("  If the Address cell is still empty on your NEXT run, this row "
          "uploads automatically with 'Lagos'.", flush=True)
    print("=" * 70, flush=True)


def _handle_missing_address(order: dict, row_number: int,
                            counts: dict[str, int]) -> bool:
    """True  -> this is the FIRST sighting: flagged for a human, the row
                must not be uploaded this run.
    False -> second sighting: counter reset, the caller plans/uploads the
                row normally and planning's built-in 'Lagos' default applies.
    Side effect: the (possibly reset) count is persisted immediately, so a
    crash right after still leaves an honest count on disk."""
    try:
        key = order_key(order)
    except Exception:
        # No dedup key computable -- treat as first sighting every time;
        # the row will fail loudly downstream anyway.
        _missing_address_flag(order, row_number, 1)
        return True
    count = counts.get(key, 0) + 1
    if count >= 2:
        # Second sighting: reset and let the row proceed with Lagos.
        counts.pop(key, None)
        _save_missing_address_counts(counts, remove_keys={key})
        sn = order.get("filtered_sn")
        where = f"Filtered # {sn}" if sn else f"Excel row {row_number}"
        print(f"[address] row {row_number} ({where}, "
              f"{order.get('customer_name')!r}): address still empty "
              f"(2nd sighting) -- proceeding automatically with the "
              f"'Lagos' address default.", flush=True)
        return False
    counts[key] = count
    _save_missing_address_counts(counts)
    _missing_address_flag(order, row_number, count)
    return True


def load_orders_from_all(paths) -> list[dict]:
    """Load and concatenate the order rows of every source in a job.

    A job accumulates one file per ``client_manager init`` run, so a client
    that was initialized three times holds three workbooks. They are
    concatenated in the given order, which is manifest order (oldest import
    first) -- so a job is always read identically no matter how many files were
    appended.

    Per-file validation is deliberately preserved: each file is loaded through
    ``load_orders`` on its own, so a missing required column names the file
    that is actually missing it instead of failing on some unrelated row of a
    combined frame.
    """
    orders: list[dict] = []
    for path in paths:
        orders.extend(load_orders(str(path)))
    return orders


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

        # Oct 2026 (missing-address targeting): only once the row is known to
        # be a real, actionable order, carry the human-facing cell
        # identifiers so the missing-address flag block can point a person
        # at the exact Excel cell to fix. Stamped AFTER the blank/required
        # checks so it can never make a blank row look non-blank. Purely
        # additive -- order_key() hashes only customer_name/phone_number/
        # product/price, so progress dedup and the retry queue are unaffected.
        if has_filter_column:
            order["filtered_sn"] = _clean_for_report(row[FILTER_COLUMN])
        order["excel_row"] = excel_row

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
    # Every --workers child re-reads the same source and derives the same
    # skipped-row list, so several processes can land here at once. Write to a
    # per-process temp file and os.replace() it in, so a reader never sees a
    # half-written CSV from an overlapping write.
    tmp = SKIPPED_ROWS_PATH.with_name(f".{SKIPPED_ROWS_PATH.name}.{os.getpid()}.tmp")
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, SKIPPED_ROWS_PATH)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
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


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + replace).

    Parallel shard workers may all try to refresh the shared catalog cache.
    A plain ``write_text`` can be observed half-written by a sibling worker
    (or leave a truncated JSON file behind if the process dies mid-write);
    ``os.replace`` is atomic on both POSIX and Windows, so readers only ever
    see the old file or the complete new one.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_catalog_cache(products: list[str]) -> None:
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            CATALOG_CACHE_PATH,
            json.dumps({"products": products,
                        "cached_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        )
        _atomic_write_text(
            CATALOG_METADATA_PATH,
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(),
                        "product_count": len(products)}, indent=2),
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


def _catalog_cache_mtime() -> float | None:
    try:
        return CATALOG_CACHE_PATH.stat().st_mtime
    except OSError:
        return None


def _should_drop_forced_catalog_refresh(effective_refresh_catalog: bool,
                                          mtime_before_run: float | None,
                                          mtime_now: float | None) -> bool:
    """Pure decision for the --lock restart loop: once this run's own
    scrape has demonstrably written a newer catalog cache than existed
    when the run started, a later relaunch (browser crash, laptop sleep,
    a stray modal close -- anything that lands back at this loop) should
    stop forcing --refresh-catalog and just reuse that cache, instead of
    paying for a brand-new scrape (+ up to _CATALOG_REFRESH_LOCK_WAIT_S of
    lock wait) on every single restart. If the mtime hasn't moved, the
    refresh this run asked for hasn't actually happened yet (e.g. the
    previous attempt died before ever reaching catalog load), so the next
    attempt must still get a real one."""
    if not effective_refresh_catalog:
        return False
    return mtime_now != mtime_before_run


_CATALOG_REFRESH_LOCK_WAIT_S = 90.0


def _acquire_catalog_refresh_lock(timeout_s: float = _CATALOG_REFRESH_LOCK_WAIT_S):
    """Best-effort single-flight lock around a live catalog refresh.

    Returns a held :class:`JobLock`, or ``None`` if another live worker held it
    for longer than ``timeout_s``. In the timeout case the caller still scrapes
    (a hung sibling must never stall the whole run); atomic cache writes keep
    the shared file valid even if two workers write at once.
    """
    lock_path = PROGRESS_DIR / "catalog_refresh.lock"

    def _try_take():
        """One non-blocking attempt at the lock: returns the JobLock on
        success, None if a live sibling still holds it."""
        lock = JobLock(lock_path)
        try:
            lock.__enter__()
        except RuntimeError:
            return None
        return lock

    # DYNAMIC WAIT (was a flat time.sleep(0.1) between attempts). The sibling
    # releases the lock by unlinking a file, which lands whenever it lands --
    # the old loop slept a full 100ms before even looking again, so up to
    # 100ms of pure dead time per attempt, on every worker that lost the
    # single-flight race. poll_until re-tests on a short backing-off schedule
    # and returns the instant the file is gone.
    #
    # Unchanged: the same timeout_s deadline, the same "scraping without the
    # shared lock" fallback when it expires, and the same guarantee that a
    # hung sibling can never stall the run.
    taken = poll_until(_try_take, timeout_s)
    if taken is not None:
        return taken
    print(f"[cache] catalog refresh lock held for over "
          f"{timeout_s:.0f}s; scraping without the shared lock so the "
          f"run keeps moving (atomic writes keep the cache valid).",
          flush=True)
    return None


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

    # Single-flight: when several --workers shard processes start together on a
    # cold/stale cache they all reach this point at once. One worker scrapes
    # while the rest wait on the shared lock and then reuse the fresh cache
    # instead of each opening the wizard. The lock is released by __exit__ even
    # if scraping raises, so a crash cannot wedge the next run.
    cache_mtime_before = _catalog_cache_mtime()
    catalog_lock = _acquire_catalog_refresh_lock()
    try:
        if refresh_catalog:
            # A sibling may have finished the refresh while we waited. Compare
            # mtimes so we only reuse a genuinely newer file; if the cache was
            # not touched we still own the duty to refresh it.
            if _catalog_cache_mtime() != cache_mtime_before:
                cached = _read_catalog_cache()
                if cached is not None:
                    print(f"[cache] another worker refreshed the catalog while this "
                          f"worker waited -- reusing {len(cached)} product(s).",
                          flush=True)
                    return cached
        else:
            cached = _read_catalog_cache()
            if cached is not None:
                print(f"[cache] another worker populated the catalog cache while "
                      f"this worker waited -- reusing {len(cached)} product(s).",
                      flush=True)
                return cached

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
    finally:
        if catalog_lock is not None:
            catalog_lock.__exit__(None, None, None)


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


def _percentile(values: list[float], pct: float) -> float:
    """p-th percentile. Uses the same 'inclusive' method as the previous
    total-only summary so the headline number does not jump just because
    the report got more detail."""
    if not values:
        return 0.0
    if len(values) < 20:
        return max(values)
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[idx]


# PERF (Phase 0): the coarse phases that tile every order. The "<phase>.*"
# sub-keys are reported separately and are NOT part of the total, because
# they are nested INSIDE these three (summing both would double-count).
_COARSE_PHASES = ("product_lines_ms", "charges_and_customer_ms", "submit_ms")


def print_performance_summary(counters: dict) -> None:
    """Phase 0 report: per-phase p50/p95 (not just a total average) plus the
    wait_profile verdict, so a real run answers "where does the time go" and
    "are waits actually the bottleneck" with numbers instead of guesses."""
    samples = counters.get("timings_ms", [])
    total_order_ms = 0.0
    if samples:
        totals = [float(t.get("total_ms", 0.0)) for t in samples]
        total_order_ms = sum(totals)
        p95 = _percentile(totals, 95)
        print(
            "[metrics] browser phases ms: "
            f"orders={len(totals)} avg={statistics.mean(totals):.1f} "
            f"p50={statistics.median(totals):.1f} p95={p95:.1f}"
        )
        # Per-phase breakdown, so a slow phase is visible rather than
        # averaged away inside the single total.
        for phase in _COARSE_PHASES:
            vals = [float(t.get(phase, 0.0)) for t in samples if phase in t]
            if not vals:
                continue
            share = (statistics.mean(vals) / statistics.mean(totals) * 100.0) \
                if totals and statistics.mean(totals) else 0.0
            print(f"[metrics]   {phase:<26} avg={statistics.mean(vals):8.1f} "
                  f"p50={statistics.median(vals):8.1f} p95={_percentile(vals, 95):8.1f}"
                  f"  ({share:4.1f}% of order)")
        # Sub-steps (nested inside the coarse phases -- reported for ranking,
        # never summed into the total).
        subs: dict[str, list[float]] = {}
        for t in samples:
            for k, v in t.items():
                if k not in _COARSE_PHASES and k != "total_ms":
                    subs.setdefault(k, []).append(float(v))
        if subs:
            ranked = sorted(subs.items(), key=lambda kv: -statistics.mean(kv[1]))
            print("[metrics] sub-steps (nested in the phases above, not additive):")
            for name, vals in ranked:
                print(f"[metrics]   {name:<26} avg={statistics.mean(vals):8.1f} "
                      f"p50={statistics.median(vals):8.1f} p95={_percentile(vals, 95):8.1f}"
                      f"  n={len(vals)}")

    for line in wait_profile.render_lines(total_order_ms):
        print(line)


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
                    lock_mode: bool = False,
                    production: bool = False,
                    client: str | None = None) -> bool:
    # PERF/DIAGNOSTICS (Sep 2026): keep a long --enable-diagnostics run's
    # trace bounded. A no-op on a normal run -- the rotator is only armed when
    # tracing actually started -- so this costs one dict lookup per order.
    maybe_rotate_trace_chunk(page, cfg, row_number)

    # Production mode: is_done() takes an order_key string, not an order dict.
    # Local mode: is_done() takes an order dict and computes the key internally.
    if production:
        from apps.sniper.production_tracker import production_order_key as _prod_key_fn
        _pk = _prod_key_fn(client, order)
        if progress.is_done(_pk):
            counters["skipped_duplicate"] = counters.get("skipped_duplicate", 0) + 1
            retry_cache.remove_key(_pk)
            failed_ledger.remove_key(_pk)
            print(f"    SKIP -- already succeeded earlier this run/a previous run "
                  f"(same customer+phone+product+price fingerprint) -- not resubmitting", flush=True)
            return True
    else:
        if progress.is_done(order) or (job_progress is not None and job_progress.is_done(order)):
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
        # Production mode: mark_done takes (order_key, order) with client-scoped key.
        # Local mode: mark_done takes (order) and computes key internally.
        if production:
            from apps.sniper.production_tracker import production_order_key as _prod_key_fn
            _pk = _prod_key_fn(client, order)
            progress.mark_done(_pk, order)
            retry_cache.remove_key(_pk)
            failed_ledger.remove_key(_pk)
        else:
            progress.mark_done(order)
            # job_progress only exists in local mode (single shared progress.db)
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
            safe_goto(page, TARGET_URL, wait_until="commit", settle_ms=0)
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

        if progress.is_done(order) or (job_progress is not None and job_progress.is_done(order)):
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


def _dedupe_orders(orders: list[dict]) -> tuple[list[dict], int]:
    """Drop rows that hash to an order_key() already seen in this file.

    PERF (--workers): parallel workers would otherwise both pass the
    is_done() check before either marks an order done and submit it twice
    (serial mode never had that race). Every worker applies this same
    deterministic filter, so all workers -- and the parent -- agree on row
    indices as well as on which rows exist. Rows whose key can't be computed
    are always kept (they can't collide with anything).

    Returns ``(unique_orders, dropped_count)``.
    """
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
    return unique_orders, duplicates


def _iter_claimed_rows(todo: list[tuple[int, dict]], orders: list[dict],
                       claim_queue: Optional[OrderClaimQueue]):
    """Yield ``(row_number, order)`` pairs for one worker to process.

    Without a claim queue this just replays ``todo`` in file order -- the
    single-session behaviour, untouched.

    With one (``--workers``), rows are pulled one at a time from a shared
    cursor over the WHOLE file instead of a per-worker slice, so a worker
    whose rows happen to finish early keeps taking work instead of idling
    while a slower peer still has rows left.

    Positions are claimed over the full ``orders`` list rather than over each
    worker's own remaining-work list on purpose: every worker's view of the
    progress DB drifts as the run proceeds (rows are marked done
    continuously), so a filtered list would map position -> row differently
    in each worker and two workers could claim what is, for them, the same
    row. Claiming full-file positions makes position -> row identical
    everywhere. Rows already marked done are simply skipped here: they cost
    their one claim and nothing else (no planning, no browser work).

    An exception in the caller's loop body leaves the current row "in
    flight" rather than completed, so the *same* worker re-offers it first
    when ``--lock`` restarts the session in this process.

    ``claim_next`` returns ``None`` for two different reasons -- the file is
    genuinely exhausted, or this worker's own band is spent and it is still
    inside the steal delay -- so the loop only stops once *nothing* is
    unclaimed. While rows are merely waiting to be stolen it sleeps briefly and
    asks again, which is what lets an early worker absorb a dead peer's work
    instead of exiting and leaving those rows for the next run.
    """
    if claim_queue is None:
        yield from todo
        return
    todo_rows = {row_number for row_number, _ in todo}
    if not todo_rows:
        # Nothing to do: do not burn one claim per already-done file row.
        return
    # Tracked as 0-based *positions* because that is what the claim queue counts
    # in: ``claim_next`` hands out positions, and ``unclaimed_in`` inspects the
    # ``row-<position>.claim`` sentinels. Keeping these in row numbers instead
    # would be an off-by-one that leaves the exit check permanently unsatisfied
    # and spins the worker forever.
    remaining = {row_number - 1 for row_number in todo_rows}
    while True:
        position = claim_queue.claim_next(len(orders))
        if position is None:
            if not claim_queue.unclaimed_in(remaining):
                return
            # Rows are still unclaimed: a peer is holding its band, or died
            # holding one. Wait for the steal delay to clear, then re-ask.
            #
            # DYNAMIC WAIT (was a flat time.sleep(CLAIM_IDLE_POLL_S), i.e.
            # 1.0s). The steal delay is wall-clock, so the condition
            # ("steal_grace_s has elapsed") becomes true at a point we can
            # predict -- a flat 1s tick either overshoots that point by up to
            # a full second or, on a short grace, wakes up repeatedly for
            # seconds while the delay is plainly still running. Sleeping
            # exactly the remaining grace and re-asking once makes the tail
            # latency after a dead peer's band opens up precise instead of
            # rounded to whole seconds.
            #
            # Deliberately NOT poll_until here: unlike the lock above there
            # is no file to watch, and the release moment is known in advance
            # from the queue's own clock, so an exact sleep is both cheaper
            # and more precise than re-polling. The 20ms floor keeps
            # sub-millisecond remainders from becoming a busy-spin.
            remaining_s = claim_queue.seconds_until_stealable()
            if remaining_s > 0:
                time.sleep(max(remaining_s, CLAIM_IDLE_POLL_S))
            continue
        if position in remaining:
            yield position + 1, orders[position]
            remaining.discard(position)
        claim_queue.mark_complete(position)


def _plan_row(order: dict, row_number: int, catalog: list[str],
              missing_address_counts: dict[str, int]):
    """Plan one row locally (product match + state inference + address rules).

    Returns ``(plan, failure, missing_address)``: ``plan`` is None when the
    row cannot be attempted at all, in which case ``failure`` holds the
    message to log it with and ``missing_address`` says whether the cause was
    a first-sighting blank address cell (logged as failed WITHOUT ever
    opening the browser). Healthy rows are logged as one instant
    ``[address]`` line and returned as a plan.
    """
    raw_address = order.get("address", "")
    used_lagos_default = False
    if not isinstance(raw_address, str) or not raw_address.strip():
        if _handle_missing_address(order, row_number, missing_address_counts):
            # First sighting: flagged for a human, not uploaded.
            return None, (
                f"no address in the source file for "
                f"{order.get('customer_name')!r} (Filtered # "
                f"{order.get('filtered_sn') or 'n/a'}) -- fix the "
                f"Address cell and run again"
            ), True
        # Second sighting: counter reset -- proceed automatically with the
        # 'Lagos' default (no parameter passed anywhere; the default lives
        # right here).
        order = {**order, "address": "Lagos"}
        used_lagos_default = True
    try:
        plan = build_execution_plan(order, catalog)
    except PlanningError as exc:
        return None, str(exc), False
    if used_lagos_default:
        plan = dataclasses.replace(plan, used_state_fallback=True)
    # One instant local line per healthy row: address intact -> resolved
    # state (or 'skipped'), then move on.
    sn = order.get("filtered_sn")
    where = f", Filtered # {sn}" if sn else ""
    label = (plan.state if plan.state is not None
             else "no state inferable -- state field will be skipped")
    def _safe(s):
        return str(s).encode("ascii", errors="replace").decode("ascii")
    print(f"[address] row {row_number}{where} "
          f"({_safe(order.get('customer_name'))}): "
          f"{_safe(order.get('address', ''))[:60]} -> {label}",
          flush=True)
    return plan, None, False


def _run_session(lock_mode: bool, orders: list[dict], progress: ProgressTracker,
                  job_progress: ProgressTracker,
                  retry_cache: RetryCache, failed_ledger: FailedLedger,
                  failure_log: FailureLogger, counters: dict,
                  failed_rows: list[FailedOrder], flagged_rows: list[FlaggedForReview],
                  refresh_catalog: bool = False, enable_tracing: bool = False,
                  claim_queue: Optional[OrderClaimQueue] = None,
                    production: bool = False, client: str | None = None) -> None:
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
    pending is exactly where the retry queue left it. Blank-address
    sighting counts are file-backed too (missing_address_counts.json),
    so the first-run flag / second-run 'Lagos' cycle survives restarts.

    ``claim_queue`` (``--workers`` only): when given, this session does not
    walk a pre-assigned slice of ``orders``. It claims rows one at a time
    from the shared queue, plans each one on the spot (planning is local but
    can include an address lookup, so it is never repeated across workers),
    uploads it, then claims again. A worker that finishes early therefore
    keeps taking work instead of idling behind a slow peer, and a ``--lock``
    session restart resumes the exact row it was in the middle of. Without a
    claim queue the session walks ``orders`` in file order exactly as before.

    Cancellation handling:
      * A module-level ``_SESSION_CANCELLED`` flag is set by SIGINT/SIGTERM
        handlers installed at the start of this session.
      * The order-processing loop checks this flag before each row and stops
        taking new orders when cancellation is detected.
      * An order already in flight is allowed to complete (or fail on its
        own merits) -- we never abort a remote submission mid-flight.
      * The ``finally`` block always closes browser, context, and page.
      * An interrupted run does NOT mark any order successful; it records
        an "interrupted" outcome for the operator to reconcile.
    """
    # --- Cancellation -------------------------------------------------------
    _SESSION_CANCELLED = {"flag": False}

    def _request_cancel(signum, frame):
        _SESSION_CANCELLED["flag"] = True
        print(
            f"\n[cancel] stop requested (signal {signum}) -- finishing current "
            f"order, then shutting down. No new orders will be started.",
            flush=True,
        )

    # Install handlers for both SIGINT (Ctrl+C) and SIGTERM (GH Actions
    # cancellation). On Windows, SIGTERM may not be available; guard it.
    _old_sigint = signal.getsignal(signal.SIGINT)
    _old_sigterm = None
    signal.signal(signal.SIGINT, _request_cancel)
    try:
        _old_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _request_cancel)
    except (AttributeError, OSError):
        # SIGTERM not available on this platform -- SIGINT still works.
        pass

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
            if enable_tracing:
                print("[speed] diagnostics: ON -- Playwright tracing (a DOM "
                      "snapshot + screenshot per action) is recording for this "
                      "whole run and waits are being timed, so expect this run to "
                      "be slower than normal. Trace windows rotate, so memory "
                      "stays bounded even on a long run.", flush=True)
            else:
                print("[speed] diagnostics: OFF (fast mode) -- pass "
                      "--enable-diagnostics to record Playwright traces + wait "
                      "timings for a run you need to debug. Closure "
                      "screenshots/.txt/.json evidence is written either way.",
                      flush=True)

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
            if tracing_active:
                # DIAGNOSTICS (Sep 2026): arm trace-window rotation so a long
                # --enable-diagnostics run cannot buffer its whole trace in
                # RAM -- see closure_diagnostics.maybe_rotate_trace_chunk.
                enable_trace_rotation(cfg.diagnostics_dir)

            plans: dict[int, ExecutionPlan] = {}
            deterministic_failures: dict[int, str] = {}
            # Rows flagged this run for a blank address cell (first sighting):
            # logged as failed WITHOUT ever opening the browser.
            missing_address_rows: set[int] = set()
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
                if not (
                    progress.is_done(order)
                    or (job_progress is not None and job_progress.is_done(order))
                )
            ]
            skipped_done = len(orders) - len(todo)
            if skipped_done:
                print(f"[plan] {skipped_done} order(s) already marked done -- "
                      f"planning + uploading only the remaining {len(todo)}.",
                      flush=True)
            if not todo:
                print("[plan] everything in this file is already marked done -- "
                      "nothing to plan or upload this session.", flush=True)
            # Oct 2026 speed pass: planning is 100% local now -- no
            # parallel web pre-warm, no state_cache.json, no Lagos
            # fallback. Each address logs one instant line; a blank
            # address never reaches planning at all:
            #   sighting 1 -> HUMAN ACTION NEEDED block, row NOT uploaded;
            #   sighting 2 -> counter resets, row proceeds with the
            #   'Lagos' default built into planning itself.
            from apps.sniper.product_upload import state_engine as _state_engine
            _state_engine.clear_memo()
            missing_address_counts = _load_missing_address_counts()

            def record_plan(row_number: int, order: dict) -> None:
                """Plan one row and record the outcome in the run-wide maps."""
                plan, failure, missing_address = _plan_row(
                    order, row_number, catalog, missing_address_counts)
                if plan is not None:
                    plans[row_number] = plan
                    return
                deterministic_failures[row_number] = failure
                if missing_address:
                    missing_address_rows.add(row_number)

            if claim_queue is None:
                # Serial session: every row is known up front, so plan them
                # once before the (drain-first) upload loop, exactly as before.
                for row_number, order in todo:
                    record_plan(row_number, order)

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
            for i, order in _iter_claimed_rows(todo, orders, claim_queue):
                # --- Cancellation check before each order ------------------
                if _SESSION_CANCELLED["flag"]:
                    print(
                        f"[cancel] stopping before row {i} -- no new orders will "
                        f"be started after a stop request.",
                        flush=True,
                    )
                    break

                if claim_queue is not None:
                    # --workers: a worker only learns which row it owns at
                    # claim time, so plan it here, once, just before use.
                    record_plan(i, order)
                print(f"[{i}/{len(orders)}] Processing {order.get('customer_name')!r}...", flush=True)

                if i in deterministic_failures:
                    if lock_mode:
                        print(f"    [lock] row {i}: permanent data problem, not "
                              f"the browser or the network -- {deterministic_failures[i]} "
                              f"-- retrying this specific row can never succeed on "
                              f"identical input, so it is logged as failed once rather "
                              f"than spun on forever. Every OTHER row keeps being "
                              f"processed normally.", flush=True)
                    if i in missing_address_rows:
                        # Blank-address row flagged in planning -- logged as
                        # failed WITHOUT touching the browser (no wizard is
                        # ever opened for it).
                        result = OrderResult(False, i,
                                             reason=deterministic_failures[i],
                                             timings_ms={"total_ms": 0.0})
                    else:
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
            # Restore original signal handlers before exiting.
            try:
                signal.signal(signal.SIGINT, _old_sigint)
            except (ValueError, OSError):
                pass
            try:
                signal.signal(signal.SIGTERM, _old_sigterm)
            except (AttributeError, OSError, ValueError, TypeError):
                pass

            if _SESSION_CANCELLED["flag"]:
                print(
                    "[cancel] run interrupted -- browser closed, no new orders "
                    "started after stop request. Existing tracking data is "
                    "untouched; orders already submitted remotely but not yet "
                    "recorded in Turso must be reconciled manually.",
                    flush=True,
                )

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
                  refresh_catalog: bool, enable_tracing: bool) -> None:
    """PERF (--workers N): split one client job across N independent
    browser processes. Children do not own a fixed slice: they pull the next
    unclaimed row from a shared claim queue as they finish each one, so a
    worker whose rows happen to be fast keeps taking work instead of idling
    behind a slow peer. The CRM is a hosted SPA, so the per-order wall time is
    dominated by network round trips; N workers overlap those waits and finish
    a big file in roughly 1/N the time without changing ANY per-order logic.

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
      - duplicate fingerprints are dropped by the parent AND by every child
        before claiming starts: parallel workers would otherwise both pass
        the is_done() check before either marks the order done and submit it
        twice (serial mode never had this race).
      - one shared claim queue hands each row to exactly one worker; a row
        whose worker process dies is left un-marked-done and reprocessed on
        the next run, whose reset() clears every stale claim.
      - this parent holds the job + auth locks for the whole duration;
        shard children (--shard) skip locking entirely.
    """
    _configure_workspace(workspace)
    source_files = tuple(workspace.source_paths) or (workspace.source_path,)
    unique_orders, duplicates = _dedupe_orders(load_orders_from_all(source_files))
    if duplicates:
        print(f"[shard] dropped {duplicates} duplicate row(s) that parallel "
              f"workers would otherwise submit twice "
              f"({len(unique_orders)} unique row(s) remain).", flush=True)

    # A shared claim queue replaces the old fixed slice: children pull the next
    # unclaimed row as they finish, so a worker whose rows happen to be fast
    # keeps taking work instead of idling behind a slow peer. Reset it now so a
    # crashed previous run's leftover sentinels can never shadow this run.
    OrderClaimQueue(PROGRESS_DIR / "shard_claims").reset()
    print(f"[shard] sharing {len(unique_orders)} row(s) through one claim queue "
          f"across {workers} worker(s) -- each row goes to whichever worker is "
          f"free next.", flush=True)

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
        if enable_tracing:
            command.append("--enable-diagnostics")
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
         enable_tracing: bool = False, client: str | None = None,
         source: str | None = None, job: str | None = None,
         dry_run: bool = False, skip_done: bool = True,
         workers: int = 1, shard: tuple[int, int] | None = None,
         production: bool = False, source_type: str = "excel") -> None:
    global AUTH_STORAGE_PATH

    # --- Production mode: Turso backend, Google Sheets source ----------------
    if production:
        ProductionTrackerCls, production_order_key_fn = _import_production_modules()[0:2]
        turso_url = os.environ.get("TURSO_URL", "")
        turso_auth = os.environ.get("TURSO_AUTH", "")
        if not turso_url or not turso_auth:
            raise RuntimeError(
                "Production mode requires TURSO_URL and TURSO_AUTH environment "
                "variables. Neither may be empty. Refusing to fall back to "
                "local SQLite."
            )
        if not client:
            raise RuntimeError("Production mode requires --client <client_id>.")

        # Validate the client against configured clients before any order
        # processing, browser launch, or credential loading.
        # Accept both registry-registered clients AND client directories
        # that exist on disk (some clients may be provisioned but not yet
        # registered in _registry.json).
        manager = WorkspaceManager()
        known_clients = set(manager.known_client_ids())
        # Also accept any client directory that has a profile/client.yaml
        clients_root = manager.root
        for entry in clients_root.iterdir():
            if entry.is_dir() and (entry / "profile" / "client.yaml").exists():
                known_clients.add(entry.name)
        if client not in known_clients:
            raise RuntimeError(
                f"Unknown client {client!r}. Configured clients: "
                f"{', '.join(sorted(known_clients))}. "
                f"Add the client via 'client_manager.py init <name> --id <client_id>' "
                f"before selecting it in the workflow."
            )

        # Set ACTIVE_WORKSPACE so per-client credentials (from the client's
        # profile/client.yaml credential_reference) are loaded during login.
        # Without this, _run_session falls back to global SNIPER_EMAIL/PASSWORD.
        # In production mode there is no job directory yet, so we construct a
        # lightweight workspace that points at the client's auth/run/diagnostic
        # directories. The path fields are used only for credential cache,
        # failure-log output, and diagnostics; no local SQLite progress DB is
        # created or touched in production mode.
        try:
            paths = manager.client_paths(client)
            _PRODUCTION_WORKSPACE = type(
                "_ProductionWorkspace",
                (),
                {
                    "client_id": client,
                    "auth_dir": paths.auth_dir,
                    "run_dir": paths.state_dir / "run",
                    "diagnostics_dir": paths.state_dir / "diagnostics",
                    "job_id": f"prod_{int(time.time())}",
                    "job_dir": paths.state_dir,
                    "source_path": None,
                    "source_paths": (),
                    "progress_dir": paths.state_dir,
                    "client_progress_db": paths.client_progress_db,
                    "catalog_dir": paths.catalog_dir,
                    "job_lock_path": paths.state_dir / "job.lock",
                    "root": paths.root,
                },
            )()
            _configure_workspace(_PRODUCTION_WORKSPACE)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to configure workspace for client {client!r}: {exc}"
            ) from exc

        tracker = ProductionTrackerCls(
            client_id=client, url=turso_url, auth_token=turso_auth
        )
        retry_cache = ProductionRetryCache(tracker)
        failed_ledger = ProductionFailedLedger(tracker)

        if source_type == "sheets":
            load_orders_from_sheets = _import_production_modules()[2]
            worksheet_name = os.environ.get("GOOGLE_SHEETS_WORKSHEET_NAME", None)
            orders = load_orders_from_sheets(
                spreadsheet_id=source or "",
                worksheet_name=worksheet_name or None,
                client_id=client,
            )
            print(f"Loaded {len(orders)} order(s) from Google Sheet "
                  f"(spreadsheet_id={source[:8]}..., client={client})")
        else:
            raise ValueError(
                f"source_type={source_type!r} is not supported in production mode. "
                f"Use 'sheets'."
            )
    # --- Local mode: existing SQLite + JSON tracking + Excel source ----------
    else:
        tracker = None
        retry_cache = None
        failed_ledger = None

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
            if shard is not None:
                AUTH_STORAGE_PATH = _seed_shard_auth_storage(workspace, shard)
            print(f"Client: {workspace.client_id}  Job: {workspace.job_id}")
            # A job holds every file appended by earlier init runs, so the whole
            # set is the work, not just the newest workbook. Reading only
            # source_path silently dropped every previously imported order.
            source_files = tuple(workspace.source_paths) or (workspace.source_path,)
            for _i, _p in enumerate(source_files, start=1):
                if len(source_files) == 1:
                    print(f"Immutable source: {_p}")
                else:
                    print(f"Immutable source {_i}/{len(source_files)}: {_p}")
            orders = load_orders_from_all(source_files)
            print(f"Loaded {len(orders)} order(s) from "
                  f"{len(source_files)} file(s)")
        else:
            # Legacy single-client layout remains fully supported.
            orders_path = Path(source) if source else find_orders_file()
            orders = load_orders(str(orders_path))
            print(f"Loaded {len(orders)} order(s) from {orders_path}")

    print(f"[run] diagnostics={'on' if enable_tracing else 'off'}  "
          f"--workers={workers}  "
          + (f"shard {shard[0] + 1}/{shard[1]}" if shard else "single session"),
          flush=True)
    if workers > 1 and shard is None:
        print("[run] --workers > 1: this job is split across that many separate "
              "browser processes and each row goes to whichever worker is free "
              "next, so total time drops without changing any per-order logic.",
              flush=True)

    claim_queue: Optional[OrderClaimQueue] = None
    if shard is not None:
        shard_index, shard_count = shard
        before = len(orders)
        # Same deterministic dedupe the parent applies, so every child agrees
        # on which rows exist and on their indices before claiming begins.
        orders, duplicates = _dedupe_orders(orders)
        if duplicates:
            print(f"[shard] worker {shard_index + 1}/{shard_count}: ignored "
                  f"{duplicates} duplicate row(s) that two workers could both "
                  f"have submitted.")
        claim_queue = OrderClaimQueue(PROGRESS_DIR / "shard_claims", shard=shard)
        print(f"[shard] worker {shard_index + 1}/{shard_count}: pulling rows "
              f"from the shared claim queue ({len(orders)} of {before} row(s) "
              f"after dedupe; reserved band {shard_index % shard_count} of "
              f"{shard_count}).")

    if dry_run:
        # --- DRY RUN: non-mutating validation --------------------------------
        # Validates client + config, reads sheet data, checks existing success
        # records in the tracking interface, and reports which rows are valid,
        # already completed, eligible, or invalid. Does NOT open a browser,
        # submit orders, or write to any tracking store.
        from apps.sniper.production_tracker import production_order_key as _prod_key_fn

        valid = 0
        already_done = 0
        invalid = 0
        invalid_reasons: dict[str, int] = {}

        # Batched lookup for production mode: fetch all existing keys once
        # (one query), then check membership locally. Never one query per row.
        existing_keys: set[str] = set()
        if production:
            existing_keys = tracker.get_existing_keys()

        for o in orders:
            # Validate required fields are present
            missing_fields = _missing_required_fields(o)
            if missing_fields:
                invalid += 1
                key = ", ".join(missing_fields)
                invalid_reasons[key] = invalid_reasons.get(key, 0) + 1
                continue

            valid += 1

            if production:
                # Use batched lookup: fetch all existing keys once, check locally
                _pk = _prod_key_fn(client, o)
                if _pk in existing_keys:
                    already_done += 1
            else:
                if progress.is_done(o) or (job_progress is not None and job_progress.is_done(o)):
                    already_done += 1

        eligible = valid - already_done

        print(
            f"[dry-run] client={client}: "
            f"{len(orders)} total row(s), "
            f"{valid} valid, "
            f"{invalid} invalid, "
            f"{already_done} already completed (skipped), "
            f"{eligible} eligible for upload.",
            flush=True,
        )
        if invalid_reasons:
            print(
                f"[dry-run] invalid-row reasons: "
                + "; ".join(f"{k} ({v})" for k, v in invalid_reasons.items()),
                flush=True,
            )
        if production:
            print(
                f"[dry-run] Turso tracking checked for client={client}: "
                f"{already_done} order(s) already recorded as succeeded.",
                flush=True,
            )
        print(
            "[dry-run] NO browser opened, NO orders submitted, NO tracking data "
            "mutated. Safe to repeat.",
            flush=True,
        )
        return {}

    # Production mode: use the Turso-backed tracker that was already
    # instantiated above. No local SQLite files are created or touched.
    if production:
        progress = tracker
        retry_cache = retry_cache
        failed_ledger = failed_ledger
        job_progress = None  # production uses Turso, no local job_progress DB
    else:
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


    # Production mode: batched lookup of existing succeeded order keys,
    # then filter locally in memory. One query instead of one per order.
    # Always scoped by client_id via get_existing_keys().
    if production:
        _, production_order_key_fn = _import_production_modules()[:2]
        from apps.sniper.production_tracker import production_order_key as prod_key_fn
        existing_keys = tracker.get_existing_keys()
        already_done_count = 0
        for o in orders:
            _pk = prod_key_fn(client, o)
            if _pk in existing_keys:
                already_done_count += 1
    else:
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

    # PERF (Phase 0): the wait profile is per-process, so a --lock session
    # that relaunches the browser keeps accumulating into the same counters.
    # That is intentional (it reflects the WHOLE run), but the very first
    # run of the process must start clean.
    wait_profile.reset()

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
        # FIX (speed/persistence pass, "interrupted by sleep or modal close
        # takes long to respond again"): --refresh-catalog previously stayed
        # True across every internal relaunch of this loop, so a browser
        # crash, a laptop sleep, or a stray modal close -- ANY exception that
        # sends control back here -- paid for a brand-new live catalog
        # scrape plus up to _CATALOG_REFRESH_LOCK_WAIT_S (90s) waiting on the
        # shared refresh lock, every single time, even seconds after the
        # previous attempt already wrote a fresh cache. --refresh-catalog
        # means "refresh once for this run", not "refresh again on every
        # relaunch this run" -- once THIS run's own scrape has demonstrably
        # written a newer cache file than existed when the run started,
        # later relaunches reuse it like any other --lock restart would.
        # If an attempt fails before ever reaching the catalog load (e.g. a
        # login failure), the cache mtime is unchanged and the next attempt
        # still gets a real refresh -- this never skips a refresh that
        # hasn't actually happened yet.
        catalog_mtime_before_run = _catalog_cache_mtime()
        effective_refresh_catalog = refresh_catalog
        while True:
            attempt += 1
            try:
                _run_session(lock_mode, orders, progress, job_progress, retry_cache,
                             failed_ledger, failure_log,
                             counters, failed_rows, flagged_rows,
                             refresh_catalog=effective_refresh_catalog,
                             enable_tracing=enable_tracing, claim_queue=claim_queue,
                             production=production, client=client)
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
            finally:
                if _should_drop_forced_catalog_refresh(
                        effective_refresh_catalog, catalog_mtime_before_run,
                        _catalog_cache_mtime()):
                    effective_refresh_catalog = False
    else:
        try:
            _run_session(lock_mode, orders, progress, job_progress, retry_cache,
                         failed_ledger, failure_log,
                         counters, failed_rows, flagged_rows, refresh_catalog=refresh_catalog,
                         enable_tracing=enable_tracing, claim_queue=claim_queue,
                         production=production, client=client)
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


def workspace_housekeeping(manager: WorkspaceManager,
                           retention_days: int = ARCHIVE_RETENTION_DAYS) -> None:
    """Bring every client workspace back to the one-active-job shape.

    Runs before anything decides what work exists, and never opens a browser:
      1. archive moves a previous completion deferred (locked file) are
         finished;
      2. `job_*` folders that never got a manifest are quarantined -- they
         were the "Michael has three folders and no idea which is real" case;
      3. any client holding more than one live job keeps only the newest, the
         rest go to jobs/archive/superseded/ with their state intact;
      4. archived jobs that have been settled for `retention_days` and hold no
         un-uploaded orders are moved out to clients/_graveyard/ so the client
         directory stays readable -- a move, never a delete.
    """
    deferred = manager.retry_deferred_archives()
    if deferred:
        print(f"[housekeeping] finished {deferred} deferred archive move(s).",
              flush=True)
    for line in manager.quarantine_orphan_job_dirs():
        print(f"[housekeeping] {line}", flush=True)
    for line in manager.enforce_single_active_job():
        print(f"[housekeeping] {line}", flush=True)
    for line in manager.prune_job_archives(retention_days):
        print(f"[housekeeping] {line}", flush=True)


def _fair_share_workers(max_parallel: int, distinct_groups: int) -> int:
    """PERF (batch concurrency, "one client should get the whole budget,
    two should split it"): --max-parallel is a proven-safe TOTAL ceiling on
    concurrent browser sessions (the operator's own tested limit -- see
    ClientProcessScheduler's docstring on why it exists at all: shared
    per-client auth/catalog locks, not a CPU guess). Today that ceiling is
    handed out as a flat --workers-per-job number decided before any job's
    contention is known, so a solo client run leaves slots idle instead of
    using them.

    This has NO opinion on whether more than --max-parallel concurrent
    sessions would be safe -- it never returns anything that could push
    total concurrent slots past max_parallel, because ClientProcessScheduler
    still independently enforces that ceiling via its own _fits() check
    regardless of what this returns. All this does is stop wasting an
    already-proven-safe budget on a client that's currently the only one
    asking for it.

    max_parallel <= 0 means "unlimited" (see ClientProcessScheduler.unlimited)
    -- there is no fixed budget to divide, so callers should not use this and
    should fall back to the operator's own --workers value instead.
    """
    if max_parallel <= 0:
        return 1
    groups = max(1, distinct_groups)
    return max(1, max_parallel // groups)


def main(lock_mode: bool = False, refresh_catalog: bool = False,
         enable_tracing: bool = False, client: str | None = None,
         source: str | None = None, job: str | None = None,
         dry_run: bool = False, skip_done: bool = True,
         legacy: bool = False,
         skip: tuple[str, ...] = (),
         workers: int = 1, shard: tuple[int, int] | None = None,
         max_parallel: int = 2, auto_scale_workers: bool = False,
         housekeeping_only: bool = False,
         production: bool = False, source_type: str = "excel") -> None:
    """Run one explicit job or drain the inbox with bounded client concurrency.

    The upload engine is intentionally invoked once per workspace. A completed
    workspace returns normally and its browser is closed before the next one
    starts, so one bad client cannot mix state with another client. In batch
    mode, ``max_parallel`` caps the total browser slots used by concurrently
    running client jobs; each child job with ``--workers N`` consumes N slots.
    """
    manager = WorkspaceManager()

    def execute_workspace(workspace: ClientWorkspace,
                          shard: tuple[int, int] | None = None) -> None:
        if shard is not None:
            # Shard worker child: the parent owns the job/auth locks and
            # the final job-status transition -- just claim and upload
            # rows, then exit. No locks, no status writes, no cleanup.
            _run_upload(lock_mode, refresh_catalog, enable_tracing,
                        workspace.client_id, None, workspace.job_id,
                        dry_run, skip_done, shard=shard,
                        production=production, source_type=source_type)
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
                                     refresh_catalog, enable_tracing)
                    else:
                        counts = _run_upload(lock_mode, refresh_catalog, enable_tracing,
                                             workspace.client_id, None, workspace.job_id,
                                             dry_run, skip_done, workers=workers,
                                             production=production, source_type=source_type)
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
            if source_type != "sheets":
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
                           shard=shard, production=production,
                           source_type=source_type)

    skip_values = {item.lower() for item in skip}
    manager.recover_claimed_files()
    # FIX (Sep 2026, "every client has exactly one job folder"): settle the
    # workspace shape BEFORE anything decides what work exists. Without this a
    # client could keep several live job folders forever -- the newest file's
    # import, a re-run, and an interrupted import each left one behind -- and
    # the run would then pick whichever sorted first, not the newest.
    workspace_housekeeping(manager)
    if housekeeping_only:
        for client_id in manager.known_client_ids():
            live = manager.live_jobs(client_id)
            if live:
                for job_dir, manifest in live:
                    print(f"[housekeeping] active job {client_id}/"
                          f"{job_dir.name}: {manifest.get('status', 'imported')}",
                          flush=True)
        return
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
        # FIX (Sep 2026, "on each run, log all completed jobs"): log every
        # completed job -- live or archived -- before deciding what still
        # needs work. (The deferred-archive retry itself already ran, as part
        # of workspace_housekeeping above, for every run including this one.)
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
        return _run_upload(lock_mode, refresh_catalog, enable_tracing, client,
                           None, None, dry_run, skip_done,
                           production=production)
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
    # mixing authentication, progress, or report state. The parent now starts
    # only as many child jobs as the global browser-slot budget allows and
    # keeps one job per client active at a time.
    if max_parallel < 0:
        raise ValueError("--max-parallel must be non-negative")

    child_workers = workers
    if max_parallel > 0 and not dry_run:
        if auto_scale_workers:
            distinct_groups = len({workspace.client_id for workspace in workspaces})
            fair_share = _fair_share_workers(max_parallel, distinct_groups)
            if fair_share != child_workers:
                print(
                    f"[batch] --auto-scale-workers: {distinct_groups} client(s) "
                    f"ready right now -- giving each job --workers {fair_share} "
                    f"(of {max_parallel} total slot(s)) instead of the plain "
                    f"--workers {workers} you passed.",
                    flush=True,
                )
            child_workers = fair_share
        elif child_workers > max_parallel:
            print(
                f"[batch] reducing per-client --workers {child_workers} to "
                f"--workers {max_parallel} to respect --max-parallel {max_parallel}.",
                flush=True,
            )
            child_workers = max_parallel

    scheduled_jobs = []
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
        if enable_tracing:
            command.append("--enable-diagnostics")
        if dry_run:
            command.append("--dry-run")
        if child_workers > 1:
            command.extend(["--workers", str(child_workers)])
        slots = 1 if dry_run else child_workers
        scheduled_jobs.append(ScheduledJob(
            key=f"{workspace.client_id}:{workspace.job_id}",
            group=workspace.client_id,
            command=command,
            slots=slots,
            label=f"client {workspace.client_id} job {workspace.job_id}",
        ))

    if not scheduled_jobs:
        return

    cap = "unlimited" if max_parallel == 0 else str(max_parallel)
    print(
        f"[batch] scheduling {len(scheduled_jobs)} client job(s) with "
        f"max_parallel={cap} browser slot(s).",
        flush=True,
    )
    # FIX (Sep 2026, "why is --workers 8 doing nothing?"): the --max-parallel
    # cap can silently reduce --workers, and the reduction line was easy to
    # lose in a long run's output. State the EFFECTIVE number every time, right
    # next to the cap that produced it.
    reduction = ("" if child_workers == workers else
                 f" (--max-parallel {max_parallel} reduced your --workers "
                 f"{workers}; pass --max-parallel 0 for no ceiling)")
    print(
        f"[batch] effective --workers {child_workers} per job, "
        f"{len(scheduled_jobs)} job(s) scheduled{reduction}.",
        flush=True,
    )

    def _on_job_start(scheduled_job: ScheduledJob) -> None:
        print(f"[batch] launching {scheduled_job.label}", flush=True)

    def _on_job_finish(result) -> None:
        if result.returncode:
            print(
                f"[batch] {result.job.label} exited with code "
                f"{result.returncode}",
                flush=True,
            )

    scheduler = ClientProcessScheduler(
        max_parallel=max_parallel,
        on_start=_on_job_start,
        on_finish=_on_job_finish,
    )
    results = scheduler.run(scheduled_jobs)
    failed = any(result.returncode != 0 for result in results)
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
        "--enable-diagnostics",
        action="store_true",
        help="OPT IN to the slow debugging extras. They are OFF by default "
             "because they cost real time on every action: full Playwright "
             "tracing (a DOM snapshot + a screenshot recorded per action, "
             "rotated into bounded trace windows) plus per-wait timing "
             "accounting (WW_WAIT_PROFILE). Use it only for a run you are "
             "investigating. Closure evidence (screenshot + state .txt/.json) "
             "is written on an unexpected dialog closure EITHER WAY -- this "
             "flag only adds the trace .zip and the wait timings.",
    )
    parser.add_argument(
        "--no-diagnostics",
        action="store_true",
        dest="no_diagnostics_legacy",
        help=argparse.SUPPRESS,
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
             "(requires --client/--job). Workers pull rows from one shared "
             "claim queue -- each row goes to whichever worker is free next -- "
             "and each keeps its own retry-queue shard; progress and retry "
             "persistence are shared/merged exactly as before.")
    parser.add_argument(
        "--max-parallel", type=int, default=2,
        help="Batch-mode global browser-slot cap across client jobs "
             "(default: 2). Use 0 for unlimited. A child job with "
             "--workers N consumes N slots; values above the cap are reduced "
             "so the total active browser count stays bounded.")
    parser.add_argument(
        "--auto-scale-workers", action="store_true",
        help="Batch mode only: instead of giving every client job a fixed "
             "--workers count, split --max-parallel slots evenly across "
             "however many DIFFERENT clients actually have work ready right "
             "now -- one client alone gets the full slot budget, two "
             "clients ready at once split it in half, and so on. This only "
             "ever redistributes the SAME --max-parallel ceiling you've "
             "already been running with -- it never launches more total "
             "concurrent browser sessions than --max-parallel allows, it "
             "just stops leaving slots idle when there's only one client "
             "to give them to. Off by default so existing invocations keep "
             "their exact current behavior unless you opt in. No effect "
             "with --max-parallel 0 (unlimited) or outside batch mode.")
    parser.add_argument(
        "--housekeeping-only",
        action="store_true",
        help="Only tidy the client job directories and exit without opening a browser: "
             "finish deferred archive moves, quarantine job folders that have no "
             "manifest, and make sure every client keeps exactly ONE live job "
             "(older ones move to jobs\\archive\\superseded with all their state).",
    )
    parser.add_argument("--shard", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--production",
        action="store_true",
        help="Run in production mode: shared Turso database + Google Sheets source. "
             "Requires TURSO_URL, TURSO_AUTH, and GOOGLE_SHEETS_* environment variables. "
             "Never falls back to local SQLite.",
    )
    parser.add_argument(
        "--source-type",
        choices=["excel", "sheets"],
        default="excel",
        help="Data source type: 'excel' (local CSV/XLSX, default) or "
             "'sheets' (live Google Sheet, production mode only).",
    )
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
    # DIAGNOSTICS (Sep 2026): off unless explicitly asked for, so a normal run
    # never pays for traces or wait accounting it will not read. The old
    # --no-diagnostics is still ACCEPTED and ignored, so every existing command
    # keeps working exactly as written.
    if args.no_diagnostics_legacy:
        print("[speed] note: --no-diagnostics is no longer needed -- "
              "diagnostics are OFF by default now. Pass --enable-diagnostics "
              "when you actually want traces.", flush=True)
    # WW_WAIT_PROFILE, when set in the environment, always wins; otherwise
    # --enable-diagnostics decides, so ONE flag turns on traces + wait timings
    # together for a debug run.
    if os.environ.get(wait_profile.ENV_FLAG) is None:
        wait_profile.PROFILE.enabled = bool(args.enable_diagnostics)
    main(lock_mode=args.lock, refresh_catalog=args.refresh_catalog,
         enable_tracing=args.enable_diagnostics, client=args.client,
         source=args.source, job=args.job, dry_run=args.dry_run,
         skip_done=args.skip_done,
         legacy=args.legacy,
         skip=tuple(item.strip() for item in args.skip.split(",") if item.strip()),
         workers=max(1, args.workers), shard=shard,
         max_parallel=args.max_parallel,
         auto_scale_workers=args.auto_scale_workers,
         housekeeping_only=args.housekeeping_only,
         production=args.production,
         source_type=args.source_type)