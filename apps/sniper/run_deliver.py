"""
run_deliver.py

Pending -> Delivered bulk status automation for the sniper.ng orders page.

For one client (User filter) and a list of customer names, moves every
matching PENDING order to DELIVERED via the row action menu ->
"Move to status" -> Delivered modal, then verifies the result on the CRM
itself before recording success.

Core design rules (doc: ORDER AUTOMATION PLAN):
- Order ID is the identity (customer name is only a search key).
- Persistent state machine per order, atomic checkpoint writes.
- Never mark SUCCESS before the CRM's own status confirms Delivered.
- Idempotent: re-running skips fingerprinted successes and rows that are
    already Delivered on the CRM.
- Crash during Save is safe: recovery re-queries the CRM; Delivered =>
    SUCCESS without another Save; Pending => retry.
- Browser/network failures have dedicated recovery states; the process
    can never hang indefinitely on one customer (per-order watchdog).
- Background/cron friendly: state is file-backed; SIGINT/SIGTERM persist
    and release cleanly.

CLI:
python run_deliver.py --client Michael --customers customers.txt
python run_deliver.py --client Michael --config deliver_config.json

FIX (earlier revision, "login succeeds but the orders page isn't ready yet"):
login_with_session_cache()'s own internal readiness check
(apps/sniper/utils/auth.py -> login()) only waits
`network_wait_timeout_ms` (default 20000ms) for LOGGED_IN_SELECTOR
("Add Order" button) before raising. run_automation.py survives slow/cold
page loads because it does a SECOND, independent wait right after
login_with_session_cache() returns. run_deliver.py now does the same:
1. Pass network_wait_timeout_ms=LOGIN_NETWORK_WAIT_MS into
    login_with_session_cache().
2. Call _confirm_orders_page(page) right after -- a second wait, with one
    reload+retry if the button still isn't there.
Applied at every point this module logs in (initial login AND the
browser-recovery re-login). DeliverDriver's constructor is unchanged:
DeliverDriver(page, store, config, verbose=verbose).

FIX (this revision, "filter chips never apply -> User combobox stays
disabled forever"):
Log signature:
    warning: filter chip 'combo set' / 'Added By' / 'Assigned By' not present
    Locator.click: Timeout 30000ms ... <button disabled ... role="combobox">
    (User)  - "element is not enabled"
Playwright says "not enabled", not "outside of viewport" / "intercepts
pointer events", so this was never a scrolling problem (and _center() is
best-effort and cannot raise). The User selector is also correct (it
matches the plan doc + the cmdk spec verbatim, and it resolved to the right
<button>). The real cause is upstream, in the selector HELPER that finds the
"Order by Forms" / "Filter By" trigger:

* _group_trigger() walked `ancestor::div[N]` in the order (2, 3, 1) and
    took the FIRST `button[role=combobox]` under whichever ancestor it hit.
    The four filters live in ONE grid (plan doc §2.1:
    `div.grid ... lg:grid-cols-4`). For a filter whose <label> is a direct
    child of its cell (exactly like the User filter,
    `div:has(> label:text-is("User"))`) ancestor::div[1] is the cell but
    ancestor::div[2] is the WHOLE GRID -- so `.first` returned the PERIOD
    trigger. The code then opened the Period list, waited for an option
    containing "combo set", never found it, retried, gave up, and warned.
    Three chips, same wrong trigger, same warning; no chip ever applied, so
    the CRM never enabled User.
* The label text was matched with `text-is` (exact + case-sensitive) and a
    miss returned None silently (no diagnostic at all).
* Retrying re-clicked an option that was already ticked, which UN-ticks a
    multi-select item.

Fixes:
1. _group_trigger(): (a) if a chip of the group is already rendered, the
    trigger IS the button that contains it (plan doc §2.1 c/d P selector);
    (b) otherwise resolve label -> trigger with the CLOSEST enclosing cell
    first and never cross into a container holding more than one <label>
    (i.e. never escape into the grid); case/whitespace-tolerant label match
    with aliases; `for=` and `following::` fallbacks.
2. _apply_chip(): popup-scoped option lookup (exact text preferred),
    skips options that are already ticked, tolerant Confirm button, waits
    for the chip to render instead of a fixed 250ms, Escape only between
    attempts, and prints WHAT it saw (labels / comboboxes / options) when
    something can't be found.
3. _pick_crm_user(): waits for the User trigger to become ENABLED (with one
    chip re-apply) and fails fast with a clear reason + debug dump instead
    of a 30s blind click on a disabled button.
FIX (this revision, second live log): the chips now apply, but three more
things showed up.
* "Assigned By" does not exist on the live page -- the option is called
    "Assigned To" (popup: Added By / Processed By / Updated By / Assigned
    To / On Hold By). The plan doc is out of date on that one. The default
    is now ("Added By", "Assigned To"); "Assigned By" is accepted as an
    alias, and `filterBy` / `orderByForms` in the config file override it.
* NOT_FOUND for a customer that may well exist: search_customer() pressed
    Enter and immediately called wait_for_app_idle(), which returns as soon
    as ANY table row exists. With 189 pending rows already on screen it
    returned before the server-filtered result landed, so find_row() looked
    at the OLD list (first 20 rows). Now the search waits for the network
    to go quiet (fetch/xhr) and re-searches once if the table still has rows
    but none match. NOT_FOUND now prints what the table actually showed.
* "delivered delta (-1)": the tab badges are filtered by the quick-search
    text, so the final Delivered count was read with the last customer still
    typed in the box (0). The box is now cleared before the count.
* Failure reasons are always printed (not only with --verbose); a Playwright
    timeout is only treated as a network outage if the connectivity probe
    actually fails (before, ANY timeout was reported as "network issue");
    Save that leaves the modal open now reports the page's own error text.

FIX (third live log, "Mark order as Delivered" modal):
* `Error binding parameter 7: type 'dict' is not supported` killed the run
    right after the modal step. The only dict this module ever handed the
    state store was `package=package_info` (SAVE_SUBMITTED). Every value
    passed to the store is now a plain str/int (dict/list -> JSON string),
    a store hiccup after Save can no longer skip verification, and an
    unexpected exception inside one customer is contained (FAILED + reason)
    instead of ending the whole run.
* The modal step is hardened: _center() no longer scroll-jumps inside
    dialogs/menus/listboxes (they are position:fixed layers -- smooth
    scrolling the page behind them is what looked like "going out of the
    modal"); Escape is only sent when a select popup is really open (an
    Escape with no popup open CLOSES the dialog); the modal is checked to
    still be open after every field; the Save button has a dedicated,
    exact locator (`Save` inside the dialog, last match); Save waits for
    enabled + for select popups to be gone; a disabled Save reports the
    agent / package / total / validation text it saw.
* Price rule: order is Agent -> Package -> Total. If the Total field
    differs from the target price (the Excel price when the workbook has a
    price column, else the order's Amount from the CRM row) the field is
    SELECTED and REPLACED in one paste-style input event (select-all +
    insertText: no Backspace/Delete, so no empty intermediate state for the
    form's validation to race on), verified, then Save.
* NOT_FOUND names (not in Pending, not in Delivered) are appended to a CSV
    for a human to fix (default: <client>/state/deliver/not_found.csv).

FIX (this revision, "NOT_FOUND for a customer the Pending table DOES
show" -- surfaced by `--limit 2`, and NOT caused by the limit):
Log signature:
    Oladunni folasade Ramat: table shows 1 row(s) but none match --
        re-searching (attempt 1/2)
    Oladunni folasade Ramat: NOT_FOUND -- Pending tab: 1 row(s)
        ['Oladunni folasade']; Delivered tab: 1 row(s) ['']
The right row WAS on screen (the search returned it, and the phone in that
row's HTML matches the sheet's +2348082508065). The CRM renders a customer
name as one `span.block` PER LINE, and only the FIRST block was read, so
the sheet name "Oladunni folasade Ramat" was compared against the CRM's
"Oladunni folasade": the exact match failed, and the old whole-word
fallback only ever looked for the WANTED name inside the shown text, never
the other way round. --limit 1 only ever processed sheet row 2 ("Ewumi
Elizabeth", a single block), which is why it looked like the limit broke
it.
Fixes:
  1. _name_in_cell() joins EVERY `span.block` of the name cell
     (join_name_parts), so wrapped names match. This also un-breaks
     verify_delivered(), which compares the same way after Save.
  2. find_row() keeps the exact match and adds a GUARDED partial-name
     fallback, accepted only when exactly ONE row on screen qualifies
     (never a bare substring, never a guess between candidates, and never
     a single-word CRM fragment standing in for a longer name).
  3. The empty state ("No orders match your filters") is a
     `<tr><td colspan="28">` row inside the same tbody: `tbody tr` counted
     it as a data row, so an empty result was never recognised as empty
     ("table shows 1 row(s) but none match" twice) and wait_for_app_idle()
     returned instantly on an empty table. Data rows are now selected with
     `tbody tr:has(td:nth-child(6))` (TABLE_ROW_DATA) for row counting,
     snapshots and find_row().
  4. The status tab is now VERIFIED to be the one on screen (the footer's
     "match in Pending/Delivered"); a tab click that didn't switch is
     re-clicked and, if it still won't switch, that customer fails with a
     clear reason instead of reading the other tab's rows -- which could
     otherwise record a still-Pending order as ALREADY_DELIVERED.
  5. The orders list is confirmed to have rendered (data row / empty state
     / footer) before the tab badges are read, and a 0/0 badge reading is
     warned about instead of silently making every search "find nothing".
  6. The re-search log line prints the names the table actually showed.
Also: this file had grown TWO copies of the whole module (a stale revision
first, whose `if __name__ == "__main__":` body had been replaced by this
docstring, then the live one). The stale copy was removed -- one `main()`
guard remains.

FIX (this revision, "run the whole file: faster flow + a second identifier
for the customer"):
  1. FAST VERIFICATION. A Save the CRM accepted closes the modal on its own;
     that, plus the customer no longer appearing in the Pending tab (0 data
     rows while the quick-search box still holds the name), IS the answer:
     the order moved. verify_delivered() returns on that first check instead
     of walking the Delivered tab and re-querying up to VERIFY_ATTEMPTS
     times -- that round trip was most of the per-customer time. Success
     lines now say HOW it was confirmed: via row-delivered / pending-gone /
     delivered-tab. The slower checks stay exactly where they are needed: a
     row that is STILL Pending is a real failure (safe retry path, with one
     extra look for a lagging list), and a name that is missing WITHOUT a
     clean Save (the idempotency pre-check) still gets the Delivered tab
     checked -- which is now the only place that tab is consulted.
  2. PHONE IDENTITY. A name alone is not a fingerprint: the CRM lets a name
     repeat and two different people can share one. The Contact cell's
     `tel:` (phone) and `wa.me` (WhatsApp) numbers are read and normalised
     (normalize_phone / phone_keys / phone_matches):
       * the sheet's Phone/WhatsApp columns (load_customer_phones) are
         matched against the rows, so when several rows carry one name the
         one whose number matches is the one moved; when nothing matches,
         the FIRST row is used and the log says so;
       * the number is stored with the customer (deliver_progress.db gained
         a migratable `phone` column), so is_done() no longer counts a
         success recorded for the same NAME against a DIFFERENT number --
         that is a different person, and the row is processed;
       * the number is kept on ALREADY_DELIVERED/SUCCESS records, so a later
         run can tell which of two same-named customers is already done.
  3. A status tab the table is already showing is no longer re-clicked (the
     per-customer search forces a fresh query anyway), and the report
     records how many customers came with a phone number
     ("customersWithPhone").

FIX (this revision, "the sheet's Status column decides what is work, and
every client keeps its own log"):
  1. SHARED STATUS RULE (_select_work_rows): load_customers(),
     load_customer_prices() and load_customer_phones() now select rows
     through ONE helper, so names, prices and phone fingerprints always
     come from the exact same rows. The rule (corrected after the first
     version: the sheet's own marking is the INSTRUCTION -- Delivered
     means "move this one", not "already done"):
       * a real data row is keyed by the filtered s/n column
         ("Filtered #" / "S/N") when the sheet has one, else by the
         name column; other empty cells never disqualify a row;
       * "blank" is judged ONLY on the status column of those real rows;
       * no status column, or every real status blank -> every real row
         is work (the whole file is ready to be moved to delivered);
       * ANY status marked -> the marked column is the tracker: ONLY
         rows whose status starts with "delivered" are work. Pending /
         Scheduled / Cancelled / blank rows are ignored even when they
         are the majority (Michael.xlsx: 6 delivered taken, 8 pending +
         11 other + 6 blank ignored), and a marked column with zero
         Delivered rows means NOTHING to process -- no silent fall back
         to "everything" (the old loaders silently processed every row
         in that case: each one a wasted CRM round trip).
     The selection is printed once per run ("sheet status: 6 delivered,
     19 other (...), 6 blank -> processing 6"), and the "no customer
     names" line explains itself with the same note.
  2. PER-CLIENT JOB LOG: main() tees stdout+stderr into
     <client>/state/deliver/deliver.log, so each client's consign
     (state, db, events, screens, report, not_found, log) is complete
     inside its own folder and no longer depends on where the operator
     redirects the console.
  3. PRICE SANITY: a Price (₦) cell holding a phone number parses to
     billions of naira (live example: sheet cell 7049528233 = the
     customer's own 07049528233). load_customer_prices() now ignores any
     price above MAX_PLAUSIBLE_PRICE (5,000,000) so the CRM row Amount
     is used instead of saving a phone number as the order Total.

Plan-doc conformance hardening (values the doc pins down, with the old
selector kept as a fallback):
* Search button: role=button name /^Search$/ (doc §2.1 e) first.
* Status tabs: strict /^Pending\\s*\\d*$/ (doc §2.2 F) first.
* Expected package amount: `Amount: N` (doc §2.8 b) before "first N".
* Viewport >= 1440x900 (doc §1) when the context has a fixed viewport.
* countSanity compares the Delivered delta with SUCCESS only
    (ALREADY_DELIVERED rows never change the delta).
"""


from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import (
    Error as PWError,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
import sys  # noqa: E402

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper.product_deliver import sheet as _sheet  # noqa: E402,F401
from apps.sniper.product_deliver.constants import (  # noqa: E402,F401
    CHIP_ALIASES,
    CHIP_CONFIRM_TIMEOUT_MS,
    CONFIRM_BUTTON_RX,
    DEFAULT_AGENT,
    DEFAULT_PERIOD,
    DISCOVERED,
    FAILED_FINAL,
    FAILED_RETRYABLE,
    FIELDS_SELECTED,
    FILTERS_READY_TIMEOUT_MS,
    FILTER_CHIPS,
    FORM_CHIP,
    GROUP_LABEL_ALIASES,
    LOGIN_NETWORK_WAIT_MS,
    LOGIN_TIMEOUT_MS,
    MAX_BROWSER_RECOVERIES,
    MAX_NETWORK_WAIT_S,
    MENU_OPEN,
    MIN_VIEWPORT,
    MODAL_OPEN,
    NAV_TIMEOUT_MS,
    NETWORK_PROBE_URL,
    PER_ORDER_DEADLINE_S,
    READY_TO_SAVE,
    ROW_FOUND,
    SAVE_SUBMITTED,
    STATUS_MENU_OPEN,
    SUCCESS,
    SUCCESS_OUTCOMES,
    TARGET_STATUS,
    USER_ENABLE_TIMEOUT_MS,
    VERIFYING,
    VERIFY_ATTEMPTS,
    WAITING_FOR_BROWSER,
    WAITING_FOR_NETWORK,
)
from apps.sniper.product_deliver.selectors import (  # noqa: E402,F401
    AMOUNT_LABELED_RE,
    AMOUNT_RE,
    CMDK_ITEM,
    CMDK_ITEM_NOT_OTHER,
    CMDK_ITEM_SALES_REP,
    CMDK_POPOVER,
    CMDK_POPOVER_FALLBACK,
    CONTACT_CELL_INDEX,
    CUSTOMER_CELL_INDEX,
    DELIVERED_TAB,
    MODAL,
    MODAL_TITLE,
    OPEN_MENU,
    OPTION_SELECTORS,
    PENDING_TAB,
    PERIOD_TRIGGER,
    POPUP_OPEN_VISIBLE,
    POPUP_SELECTOR,
    QUICK_SEARCH,
    QUICK_SEARCH_FALLBACK,
    ROW_MENU_TRIGGER,
    SEARCH_BUTTON_FALLBACK,
    SEARCH_BUTTON_LEGACY,
    STATUS_CELL_INDEX,
    SUBMENU,
    TABLE_ROW,
    TABLE_ROW_DATA,
    TAB_BASE,
    USER_SENTINELS,
    USER_TRIGGER,
)
from apps.sniper.product_deliver.common import (  # noqa: E402,F401
    atomic_write_json,
    backoff_delay,
    fingerprint,
    load_json,
    now_iso,
)
from apps.sniper.product_deliver.identity import (  # noqa: E402,F401
    clean_name,
    is_name_candidate,
    join_name_parts,
    names_match,
    phones_from_hrefs,
)
from apps.sniper.product_deliver.sheet import (  # noqa: E402,F401
    MAX_PLAUSIBLE_PRICE,
    NOT_FOUND_COLUMNS,
    PHONE_HEADERS,
    PRICE_HEADERS,
    _cell_text,
    _norm_header,
    _select_work_rows,
    export_not_found,
    load_config,
    load_customer_phones,
    load_customer_prices,
    load_customers,
    parse_price,
)
from apps.sniper.product_deliver.legacy_store import (  # noqa: E402,F401
    DeliverStore,
    OrderRecord,
)
from apps.sniper.product_deliver.errors import (  # noqa: E402,F401
    BrowserRecoveryNeeded,
    DeliverError,
    ShutdownRequested,
    StopRun,
    WatchdogTimeout,
)
from apps.sniper.product_deliver.crm import (  # noqa: E402,F401
    _center,
    _confirm_orders_page,
    _ensure_viewport,
    _probe_network,
    read_tab_count,
    wait_for_app_idle,
    wait_for_network,
)
from apps.sniper.product_deliver.driver import (  # noqa: E402,F401
    DeliverDriver,
)
from apps.sniper.product_deliver.runner import (  # noqa: E402,F401
    _Tee,
    _install_job_log,
    _install_signal_handlers,
    _latest_status_file,
    main,
    run,
)

from apps.sniper.client_workspace import (  # noqa: E402,F401
    JobLock,
    WorkspaceManager,
)
from apps.sniper.product_deliver.store import (  # noqa: E402,F401
    SqliteDeliverStore,
    normalize_phone,
    phone_keys,
    phone_matches,
)
from apps.sniper.product_deliver.inbox import (  # noqa: E402,F401
    distribute as distribute_inbox,
)
from apps.sniper.run_upload import (  # noqa: E402,F401
    LOGGED_IN_SELECTOR,
    LOGIN_URL,
    TARGET_URL,
    MissingCredentialsError,
    login_with_session_cache,
)

def __getattr__(name: str):
    """Live alias: _selection_note is mutated inside
    product_deliver.sheet; a plain re-export would freeze its
    import-time value."""
    if name == "_selection_note":
        return _sheet._selection_note
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}")



if __name__ == "__main__":
    main()
