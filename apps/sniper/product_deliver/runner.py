"""The per-client job: locking, logging tee, run loop."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from apps.sniper.client_workspace import (
    JobLock,
    WorkspaceManager,
)
from apps.sniper.product_deliver.inbox import (
    distribute as distribute_inbox,
)
from apps.sniper.product_deliver.store import (
    SqliteDeliverStore,
    phone_keys,
)
from apps.sniper.run_upload import (
    LOGGED_IN_SELECTOR,
    LOGIN_URL,
    MissingCredentialsError,
    TARGET_URL,
    login_with_session_cache,
)
from pathlib import (
    Path,
)
from playwright.sync_api import (
    sync_playwright,
)
from apps.sniper.product_deliver.common import (
    atomic_write_json,
    backoff_delay,
    now_iso,
)
from apps.sniper.product_deliver.constants import (
    DEFAULT_PERIOD,
    LOGIN_NETWORK_WAIT_MS,
    MAX_BROWSER_RECOVERIES,
    SUCCESS_OUTCOMES,
)
from apps.sniper.product_deliver.crm import (
    _confirm_orders_page,
    _ensure_viewport,
)
from apps.sniper.product_deliver.driver import (
    DeliverDriver,
)
from apps.sniper.product_deliver.errors import (
    BrowserRecoveryNeeded,
    ShutdownRequested,
    StopRun,
)
from apps.sniper.product_deliver.identity import (
    clean_name,
)
from apps.sniper.product_deliver.sheet import (
    PRICE_HEADERS,
    export_not_found,
    load_config,
    load_customer_phones,
    load_customer_prices,
    load_customers,
)
from apps.sniper.product_deliver import sheet as _sheet



class _Tee:
    """Fan one stream's writes out to several (console + client log)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        written = 0
        for stream in self._streams:
            try:
                stream.write(data)
                written = len(data)
            except Exception:                 # noqa: BLE001
                pass
        return written

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:                 # noqa: BLE001
                pass



def _install_job_log(manager: WorkspaceManager, canonical: str) -> Path | None:
    """Tee console output into <client>/state/deliver/deliver.log.

    Every client keeps its ENTIRE consign inside its own folder (state,
    db, events, screens, report, not_found) -- this adds the narrative
    log, so nothing depends on where the operator redirects stdout.
    """
    try:
        paths = manager.client_paths(manager.resolve_client_id(canonical))
        log_path = paths.state_dir / "deliver" / "deliver.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8", buffering=1)
    except Exception as exc:                  # noqa: BLE001
        print(f"[deliver] warning: per-client job log unavailable: {exc}",
              flush=True)
        return None
    sys.stdout = _Tee(sys.stdout, handle)
    sys.stderr = _Tee(sys.stderr, handle)
    return log_path




# ============================ main worker (doc §30) =========================


def _install_signal_handlers(store) -> None:
    """doc §29, two-stage: first Ctrl+C = finish the current customer and
    exit cleanly (state persisted); second Ctrl+C = stop immediately."""
    import signal

    def _handler(signum, _frame):
        if getattr(store, "shutdown", False):
            raise KeyboardInterrupt
        store.shutdown = True
        print("\n[deliver] stopping after the current customer "
              "(press Ctrl+C again to stop now)", flush=True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass



def run(client: str, customers: list[str], config: dict,
        headful: bool = True, max_success: int | None = None,
        verbose: bool = False, prices: dict | None = None,
        not_found_csv: Path | None = None, source_name: str = "",
        phones: dict | None = None) -> dict:
    """doc §30 worker: lock -> load state -> browser -> auth -> filters ->
    per-customer loop with recovery -> reconcile -> report (doc §26).

    Quiet by default (one line per customer + summary). SQLite store at
    <client>/state/deliver/deliver_progress.db. --lock relaunching lives
    in main(); max_success stops the loop gracefully via StopRun.

    prices: {clean_name(customer): price} from the Excel price column (see
    load_customer_prices); phones: {clean_name(customer): [phone, ...]} from
    the Excel phone columns (see load_customer_phones) -- the second half of
    a customer's identity, so a repeated name is never mistaken for the same
    person; not_found_csv: where names found in neither Pending nor
    Delivered are exported for a human (default
    <client>/state/deliver/not_found.csv).
    """
    period = config.get("period", DEFAULT_PERIOD)
    manager = WorkspaceManager()
    canonical = manager.resolve_client_id(client)
    paths = manager.client_paths(canonical)
    store = SqliteDeliverStore(canonical, period, paths.state_dir / "deliver")
    price_map = {clean_name(k): v for k, v in (prices or {}).items()}
    phone_map = {clean_name(k): phone_keys(v)
                 for k, v in (phones or {}).items()}
    if not_found_csv is None:
        cfg_csv = config.get("notFoundCsv")
        not_found_csv = (Path(cfg_csv) if cfg_csv
                         else paths.state_dir / "deliver" / "not_found.csv")

    def _new_driver(page_):
        drv = DeliverDriver(page_, store, config, verbose=verbose)
        drv.prices = price_map
        drv.phones = phone_map
        return drv

    _install_signal_handlers(store)
    try:
        manager.validate_client_ready(canonical)
    except Exception as exc:
        print(f"[deliver] client not ready: {exc}", flush=True)
        raise

    report = {
        "runId": f"deliver_{now_iso()}",
        "client": canonical,
        "period": period,
        "startedAt": now_iso(),
        "total": len(customers),
        "success": 0,
        "failed": 0,
        "alreadyDelivered": 0,
        "skipped": 0,
        "notFound": 0,
        "networkRecoveries": 0,
        "browserRecoveries": 0,
        "notFoundExported": [],
        "notFoundCsv": str(not_found_csv),
        "customersWithPhone": sum(
            1 for name in customers if phone_map.get(clean_name(name))),
        "results": {},
    }
    atomic_write_json(store.report_path, report)

    with JobLock(store.lock_path):
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not headful,
                args=["--disable-background-timer-throttling",
                      "--disable-backgrounding-occluded-windows",
                      "--disable-renderer-backgrounding"],
            )
            context = None
            driver = None
            try:
                context, page = login_with_session_cache(
                    browser, LOGIN_URL, TARGET_URL, LOGGED_IN_SELECTOR,
                    storage_path=str(paths.auth_dir / "session_state.json"),
                    network_wait_timeout_ms=LOGIN_NETWORK_WAIT_MS,
                    client_id=canonical,
                    credential_reference=manager.credential_reference(canonical),
                    env_file=str(manager.shared_credentials_env_path()),
                    cache_file=str(paths.auth_dir / "credentials.json"),
                )
                # Second, independent readiness check (mirrors
                # run_automation.py) -- covers the case where login
                # reported success but the orders page is still rendering.
                _confirm_orders_page(page)
                _ensure_viewport(page)
                print("[deliver] logged in and on the orders page.",
                      flush=True)
                driver = _new_driver(page)
                # The User filter trigger is disabled by the CRM until a
                # 'Filter By' chip (Added By / Assigned By) has been picked
                # first, so the chips MUST be applied before ensure_filters()
                # touches User. (ensure_filters() now also waits for User to
                # become enabled and fails fast with a diagnostic.)
                driver.ensure_form_chips()
                driver.ensure_filters(canonical)
                driver.submit_search()
                # FIX (this revision, "the run that read pending=0
                # delivered=0"): the badges are painted with the filtered
                # list, so wait for the list itself before reading them --
                # reading too early made every later search report "No
                # orders match your filters" (false NOT_FOUND).
                driver.wait_for_orders_ready()
                pending_before = driver.open_pending_tab()
                delivered_before = driver.open_delivered_tab()
                print(f"[deliver] pending={pending_before} "
                      f"delivered={delivered_before}", flush=True)
                if not pending_before and not delivered_before:
                    print("[deliver] warning: both tab badges read 0 -- the "
                          "orders list may not have loaded, so searches can "
                          "report NOT_FOUND for orders that are really "
                          "there.", flush=True)

                indexed = [
                    (num, name) for num, name in enumerate(customers, 1)
                    if not store.is_done(name, phone_map.get(clean_name(name)))
                ]
                skipped_pre = len(customers) - len(indexed)
                if skipped_pre and verbose:
                    print(f"[deliver] {skipped_pre} already done -- skipping",
                          flush=True)
                for num, name in indexed:
                    if store.shutdown:
                        raise ShutdownRequested("shutdown requested")
                    started = time.monotonic()
                    outcome = "FAILED"
                    for round_no in range(1, 4):        # §18: bounded rounds
                        try:
                            outcome = driver.process_customer(name)
                        except BrowserRecoveryNeeded as exc:
                            # doc §21: persist, rebuild browser + session,
                            # re-verify, resume (never lose state)
                            report["browserRecoveries"] += 1
                            atomic_write_json(store.report_path, report)
                            print(f"[deliver] browser recovery: {exc}",
                                  flush=True)
                            try:
                                if context is not None:
                                    context.close()
                            except Exception:
                                pass
                            context, page = login_with_session_cache(
                                browser, LOGIN_URL, TARGET_URL,
                                LOGGED_IN_SELECTOR,
                                storage_path=str(
                                    paths.auth_dir / "session_state.json"),
                                network_wait_timeout_ms=LOGIN_NETWORK_WAIT_MS,
                                client_id=canonical,
                                credential_reference=(
                                    manager.credential_reference(canonical)),
                                env_file=str(
                                    manager.shared_credentials_env_path()),
                                cache_file=str(
                                    paths.auth_dir / "credentials.json"),
                            )
                            # Same second readiness check on the recovery
                            # path, since a rebuilt session is exactly as
                            # likely to need the extra margin as the
                            # initial login.
                            _confirm_orders_page(page)
                            _ensure_viewport(page)
                            driver = _new_driver(page)
                            driver.network_recoveries = (
                                report["networkRecoveries"])
                            driver.browser_recoveries = (
                                report["browserRecoveries"])
                            # Same ordering as the initial login -- Filter By
                            # chips before the User trigger.
                            driver.ensure_form_chips()
                            driver.ensure_filters(canonical)
                            driver.submit_search()
                            outcome = "RETRY_BROWSER"
                            continue
                        if outcome in ("SUCCESS", "ALREADY_DELIVERED",
                                       "SKIPPED", "NOT_FOUND"):
                            break
                        if outcome in ("RETRY_NETWORK", "RETRY_BROWSER"):
                            continue
                        time.sleep(backoff_delay(round_no))
                    report["results"][name] = outcome
                    key = {"SUCCESS": "success",
                           "ALREADY_DELIVERED": "alreadyDelivered",
                           "SKIPPED": "skipped",
                           "NOT_FOUND": "notFound"}.get(outcome)
                    if key:
                        report[key] += 1
                    elif outcome in ("FAILED", "RETRY_NETWORK",
                                     "RETRY_BROWSER"):
                        report["failed"] += 1
                    report["networkRecoveries"] = max(
                        report["networkRecoveries"],
                        driver.network_recoveries)
                    report["browserRecoveries"] = max(
                        report["browserRecoveries"],
                        driver.browser_recoveries)
                    atomic_write_json(store.report_path, report)
                    row = store.row(name) or {}
                    dur = time.monotonic() - started
                    detail = ""
                    if outcome not in SUCCESS_OUTCOMES and driver.last_detail:
                        detail = f" -- {driver.last_detail}"
                    elif outcome == "SUCCESS" and driver.last_note:
                        detail = f" [{driver.last_note}]"
                    if outcome == "NOT_FOUND":
                        # Not in Pending, not in Delivered: a human has to
                        # find out why (name never uploaded / misspelt).
                        try:
                            if export_not_found(
                                    not_found_csv, canonical, name,
                                    driver.last_detail,
                                    price_map.get(clean_name(name)),
                                    source_name, report["runId"]):
                                report["notFoundExported"].append(name)
                        except Exception as exc:     # noqa: BLE001
                            print(f"[deliver] warning: could not write "
                                  f"{not_found_csv}: {exc}", flush=True)
                    print(f"[deliver] {num}/{len(customers)} {name} -> "
                          f"{outcome} (id={row.get('orderId') or '?'}, "
                          f"{dur:.0f}s){detail}", flush=True)
                    if driver.browser_recoveries >= MAX_BROWSER_RECOVERIES:
                        print("[deliver] browser recovery budget exhausted "
                              "-- stopping this run", flush=True)
                        break
                    if (max_success is not None
                            and report["success"] >= max_success):
                        print(f"[deliver] max-success {max_success} reached "
                              f"-- stopping", flush=True)
                        raise StopRun()

                # doc V2: Delivered-tab count sanity check. FIX: only rows
                # this run actually MOVED (SUCCESS) change the Delivered
                # count; ALREADY_DELIVERED rows were already in
                # `delivered_before`, so counting them raised a false
                # WARNING on every run that contained one.
                driver.clear_search()
                delivered_after = driver.open_delivered_tab()
                moved = sum(1 for v in report["results"].values()
                            if v == "SUCCESS")
                report["deliveredBefore"] = delivered_before
                report["deliveredAfter"] = delivered_after
                report["countSanity"] = (
                    delivered_after - delivered_before == moved)
                if not report["countSanity"]:
                    print(f"[deliver] WARNING: delivered delta "
                          f"({delivered_after - delivered_before}) != "
                          f"moved this run ({moved}) -- see results map",
                          flush=True)
            except (ShutdownRequested, StopRun, KeyboardInterrupt):
                print("[deliver] stopped -- state saved.", flush=True)
            finally:
                report["finishedAt"] = now_iso()
                atomic_write_json(store.report_path, report)
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
    if report["notFoundExported"]:
        print(f"[deliver] {len(report['notFoundExported'])} name(s) not in "
              f"Pending or Delivered -> exported for a human check: "
              f"{not_found_csv}", flush=True)
    print(f"[deliver] {report['success']} success, "
          f"{report['alreadyDelivered']} already-delivered, "
          f"{report['failed']} failed, {report['notFound']} not-found, "
          f"{report['skipped']} skipped "
          f"(recoveries: net={report['networkRecoveries']}, "
          f"browser={report['browserRecoveries']}) "
          f"-- db {store.db_path}", flush=True)
    if verbose:
        print(f"[deliver] store states: {store.counts()}", flush=True)
        print(json.dumps(report, indent=2, default=str), flush=True)
    store.close()
    return report



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move matching PENDING orders to DELIVERED (verified on "
                    "the CRM before success is recorded).")
    parser.add_argument("--client", required=True,
                        help="client name or canonical client id")
    parser.add_argument("--mark-deliver", action="store_true",
                        help="actually run the deliver flow (required)")
    parser.add_argument("--lock", action="store_true",
                        help="resilient mode: relaunch the browser session "
                             "on any crash and keep going until Ctrl+C")
    parser.add_argument("--customers", default="",
                        help="path to .txt/.csv/.json/.xlsx of customer names "
                             "(default: latest file in the client's "
                             "jobs/status dir)")
    parser.add_argument("--config", default="",
                        help="path to deliver_config.json")
    parser.add_argument("--period", default=DEFAULT_PERIOD,
                        help="Period filter value (default: This Month)")
    parser.add_argument("--headless", action="store_true",
                        help="run the browser headless")
    parser.add_argument("--max-success", type=int, default=None,
                        help="stop gracefully after this many successes")
    parser.add_argument("--limit", type=int, default=None,
                        help="attempt at most this many customers")
    parser.add_argument("--price-column", default="",
                        help="header of the price column in the .xlsx/.csv "
                             "(default: auto-detect Amount/Price/Total...)")
    parser.add_argument("--not-found-csv", default="",
                        help="where to export names found in neither "
                             "Pending nor Delivered (default: <client>/"
                             "state/deliver/not_found.csv)")
    parser.add_argument("--verbose", action="store_true",
                        help="detailed step-by-step logging")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:                       # a cp1252 console/pipe can't print "₦"
            stream.reconfigure(errors="replace")
        except Exception:                             # noqa: BLE001
            pass

    if not args.mark_deliver:
        print("[deliver] nothing to do -- add --mark-deliver. Example:")
        print("  python run_deliver.py --client michael "
              "--mark-deliver --lock")
        return

    manager = WorkspaceManager()
    distribute_inbox(manager)

    config = load_config(Path(args.config) if args.config else None)
    if args.period and args.period != DEFAULT_PERIOD:
        config["period"] = args.period

    canonical = manager.resolve_client_id(args.client)
    job_log = _install_job_log(manager, canonical)
    if job_log:
        print(f"[deliver] job log: {job_log}", flush=True)
    if args.customers:
        source = Path(args.customers)
    else:
        source = _latest_status_file(canonical)
        if source is None:
            print(f"[deliver] no customer file: pass --customers, or drop "
                  f"a file with '{manager.name_key_for(canonical)}' in the "
                  f"name into apps/sniper/status_inbox/", flush=True)
            return
    names = load_customers(source)
    if args.limit:
        names = names[:args.limit]
    if not names:
        print(f"[deliver] no customer names in {source}"
              + (f" -- {_sheet._selection_note}" if _sheet._selection_note else ""),
              flush=True)
        return
    price_col = args.price_column or config.get("priceColumn", "")
    prices, price_header, conflicts = {}, "", set()
    try:
        prices, price_header, conflicts = load_customer_prices(
            source, price_col)
    except Exception as exc:                          # noqa: BLE001
        print(f"[deliver] warning: could not read prices from "
              f"{source.name}: {exc}", flush=True)
    if prices:
        print(f"[deliver] price column '{price_header}': "
              f"{len(prices)} customer price(s)"
              + (f"; {len(conflicts)} name(s) with conflicting prices use "
                 "the CRM row Amount" if conflicts else ""), flush=True)
    else:
        print(f"[deliver] no price column found in {source.name} "
              f"(looked for {', '.join(PRICE_HEADERS[:4])}, ...) -- using "
              "each order's Amount from the CRM row; pass --price-column "
              "NAME to point at the right column", flush=True)

    phones, phone_headers = {}, ()
    try:
        phones, phone_headers = load_customer_phones(source)
    except Exception as exc:                          # noqa: BLE001
        print(f"[deliver] warning: could not read phone numbers from "
              f"{source.name}: {exc}", flush=True)
    if phones:
        print(f"[deliver] phone column(s) "
              f"{', '.join(phone_headers)!r}: {len(phones)} customer "
              "phone(s) -- used to tell same-named customers apart and to "
              "skip only that customer's own earlier success", flush=True)
    else:
        print(f"[deliver] no phone column in {source.name} -- same-named "
              "customers can only be told apart by name", flush=True)

    if _sheet._selection_note:
        print(f"[deliver] sheet status: {_sheet._selection_note}", flush=True)
    print(f"[deliver] {canonical}: {len(names)} customer(s) from "
          f"{source.name} (period={config.get('period', DEFAULT_PERIOD)})",
          flush=True)
    session = 0
    while True:
        try:
            run(canonical, names, config, headful=not args.headless,
                max_success=args.max_success, verbose=args.verbose,
                prices=prices, source_name=source.name, phones=phones,
                not_found_csv=(Path(args.not_found_csv)
                               if args.not_found_csv else None))
            break
        except MissingCredentialsError:
            raise
        except KeyboardInterrupt:
            # graceful two-stage Ctrl+C is handled inside run(); a raw
            # KeyboardInterrupt here (before/after sessions) just stops.
            print("[deliver] stopped by user.", flush=True)
            return
        except Exception as exc:
            session += 1
            if args.verbose:
                traceback.print_exc()
            if not args.lock or session >= 5:
                print(f"[deliver] run ended with error after "
                      f"{session} session(s): {type(exc).__name__}: {exc}",
                      flush=True)
                return
            print(f"[deliver] session crashed "
                  f"({type(exc).__name__}: {exc}) -- relaunching "
                  f"(Ctrl+C to stop)", flush=True)
            time.sleep(min(30.0, backoff_delay(session)))

    # (run() already prints the report when --verbose is set)


def _latest_status_file(canonical: str):
    """Newest supported customer file in the client's jobs/status dir."""
    status_dir = (WorkspaceManager().client_paths(canonical).jobs_dir
                  / "status")
    if not status_dir.exists():
        return None
    candidates = [p for p in status_dir.iterdir()
                  if p.is_file()
                  and p.suffix.lower() in {".xlsx", ".xls", ".csv",
                                           ".json", ".txt"}]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
