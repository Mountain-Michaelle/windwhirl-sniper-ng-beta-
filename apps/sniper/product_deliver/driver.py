"""DeliverDriver: find -> open -> fill -> save -> verify."""

from __future__ import annotations

import json
import re
import time
import traceback
from apps.sniper.product_deliver.store import (
    phone_keys,
    phone_matches,
)
from pathlib import (
    Path,
)
from playwright.sync_api import (
    Error as PWError,
    TimeoutError as PWTimeoutError,
)
from apps.sniper.product_deliver.common import (
    backoff_delay,
)
from apps.sniper.product_deliver.constants import (
    CHIP_ALIASES,
    CHIP_CONFIRM_TIMEOUT_MS,
    CONFIRM_BUTTON_RX,
    DEFAULT_AGENT,
    DEFAULT_PERIOD,
    FILTERS_READY_TIMEOUT_MS,
    FILTER_CHIPS,
    FORM_CHIP,
    GROUP_LABEL_ALIASES,
    MAX_BROWSER_RECOVERIES,
    MODAL_OPEN,
    PER_ORDER_DEADLINE_S,
    ROW_FOUND,
    SAVE_SUBMITTED,
    SUCCESS,
    USER_ENABLE_TIMEOUT_MS,
    VERIFYING,
    VERIFY_ATTEMPTS,
)
from apps.sniper.product_deliver.crm import (
    _center,
    _confirm_orders_page,
    _probe_network,
    read_tab_count,
    wait_for_app_idle,
    wait_for_network,
)
from apps.sniper.product_deliver.errors import (
    BrowserRecoveryNeeded,
    DeliverError,
    ShutdownRequested,
    WatchdogTimeout,
)
from apps.sniper.product_deliver.identity import (
    clean_name,
    is_name_candidate,
    join_name_parts,
    phones_from_hrefs,
)
from apps.sniper.product_deliver.legacy_store import (
    OrderRecord,
)
from apps.sniper.product_deliver.selectors import (
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
    TABLE_ROW_DATA,
    TAB_BASE,
    USER_SENTINELS,
    USER_TRIGGER,
)




# ============================ the CRM driver ================================


class DeliverDriver:
    """Implements the doc §30 worker algorithm with verification hierarchy
    doc §27: Order ID status first, Delivered-tab presence second, counts
    only as a sanity check."""

    def __init__(self, page, store, config: dict, verbose: bool = False):
        self.page = page
        self.store = store
        self.config = config
        self.verbose = verbose
        self.agent = config.get("defaultAgent", DEFAULT_AGENT)
        self.period = config.get("period", DEFAULT_PERIOD)
        self.browser_recoveries = 0
        self.network_recoveries = 0
        self._dumped: set = set()
        # Chip names are config-overridable (plan doc §8 shape).
        self.form_chip = config.get("orderByForms") or FORM_CHIP
        self.filter_chips = tuple(config.get("filterBy") or FILTER_CHIPS)
        # One-line reason for the last non-success outcome; printed by run().
        self.last_detail = ""
        # Short note about the last SUCCESS (e.g. a price overwrite).
        self.last_note = ""
        # clean_name(customer) -> price from the Excel/CSV price column.
        self.prices: dict = {}
        # clean_name(customer) -> [phone key, ...] from the sheet's phone
        # columns; tells two customers who share a name apart (find_row).
        self.phones: dict = {}
        # How the last verification was confirmed: 'row-delivered',
        # 'pending-gone' (fast path) or 'delivered-tab'.
        self.last_verify = ""
        self.overwrite_price = bool(config.get("overwritePrice", True))
        self._store_warned = False
        # fetch/xhr tracking so a search/tab click can be waited on properly.
        self._pending_reqs: dict = {}
        self._req_seq = 0
        self._last_net = time.monotonic()
        self._track_network()

    def _log(self, msg: str) -> None:
        """Verbose-only detail (quiet by default: one line per customer)."""
        if self.verbose:
            print(msg, flush=True)

    def _say(self, msg: str) -> None:
        """Always printed: used only for failure diagnostics."""
        print(msg, flush=True)

    # ---- state-store calls that can never break a customer -------------------
    def _safe_transition(self, rec, state: str, **extra) -> None:
        """rec.transition() with every value reduced to something a SQLite
        parameter can bind (dict/list -> JSON string). A store error is
        reported once and never aborts the flow: by the time SAVE_SUBMITTED
        is recorded the Save has already been clicked, and the CRM
        verification that follows must still run."""
        plain = {k: (json.dumps(v, default=str)
                     if isinstance(v, (dict, list, tuple)) else v)
                 for k, v in extra.items()}
        try:
            rec.transition(state, **plain)
        except Exception as exc:                     # noqa: BLE001
            self._warn_store(state, exc)

    def _safe_checkpoint(self) -> None:
        try:
            self.store.checkpoint()
        except Exception as exc:                     # noqa: BLE001
            self._warn_store("checkpoint", exc)

    def _warn_store(self, what: str, exc: Exception) -> None:
        if not self._store_warned:
            self._store_warned = True
            self._say(f"[deliver] warning: state store rejected '{what}' "
                      f"({type(exc).__name__}: {exc}) -- continuing "
                      "(further store errors are not repeated)")

    # ---- network tracking (search / tab settle) ----------------------------
    def _track_network(self) -> None:
        def _is_api(req) -> bool:
            try:
                return req.resource_type in ("fetch", "xhr")
            except Exception:
                return False

        def _on_request(req) -> None:
            if _is_api(req):
                self._pending_reqs[id(req)] = time.monotonic()
                self._req_seq += 1
                self._last_net = time.monotonic()

        def _on_done(req) -> None:
            if _is_api(req):
                self._pending_reqs.pop(id(req), None)
                self._last_net = time.monotonic()

        try:
            self.page.on("request", _on_request)
            self.page.on("requestfinished", _on_done)
            self.page.on("requestfailed", _on_done)
        except Exception:
            pass

    def _inflight_now(self) -> int:
        cutoff = time.monotonic() - 8.0      # ignore hung/streaming requests
        for key in [k for k, t in self._pending_reqs.items() if t < cutoff]:
            self._pending_reqs.pop(key, None)
        return len(self._pending_reqs)

    def _wait_search_settled(self, seq0: int, min_wait_ms: int = 1500,
                             max_wait_ms: int = 10_000,
                             quiet_ms: int = 700) -> None:
        """Wait until the result of a search / tab click has LANDED.
        wait_for_app_idle() cannot do this: it returns as soon as any row
        exists, which with a full table on screen is instantly (plan doc §6
        #14: wait for the search response, or settle 1500ms).
          * a request started after `seq0`  -> wait until nothing is in
            flight and the network has been quiet for `quiet_ms`;
          * no request ever started         -> settle `min_wait_ms`
            (client-side filter / cached result), then go.
        Hard-capped by `max_wait_ms`."""
        start = time.monotonic()
        while True:
            now = time.monotonic()
            elapsed_ms = (now - start) * 1000
            saw = self._req_seq > seq0
            inflight = self._inflight_now()
            quiet = (now - self._last_net) * 1000 >= quiet_ms
            if saw and inflight == 0 and quiet:
                break
            if not saw and inflight == 0 and elapsed_ms >= min_wait_ms:
                break
            if elapsed_ms >= max_wait_ms:
                self._log("[deliver] search settle hit the "
                          f"{max_wait_ms}ms cap")
                break
            self.page.wait_for_timeout(100)
        self.page.wait_for_timeout(250)      # let React paint the rows

    @staticmethod
    def _clean(text_value: str) -> str:
        return clean_name(text_value)

    # ---- primitives ---------------------------------------------------------
    def _first(self, selector: str, timeout_ms: int = 8000):
        return self.page.locator(selector).first

    def _deadline_left(self, started: float) -> float:
        return PER_ORDER_DEADLINE_S - (time.monotonic() - started)

    @staticmethod
    def _norm(text_value: str) -> str:
        return re.sub(r"\s+", " ", (text_value or "").strip()).lower()

    @staticmethod
    def _rx(*variants: str, label: bool = False):
        """Case/whitespace-tolerant, anchored regex for one or more literal
        spellings. label=True also tolerates a trailing '*' and ':'."""
        alts = [r"\s+".join(re.escape(part) for part in v.split())
                for v in variants if v and v.strip()]
        tail = r"\s*\*?\s*:?\s*$" if label else r"\s*$"
        return re.compile(r"^\s*(?:" + "|".join(alts) + r")" + tail, re.I)

    def _wait_until_enabled(self, locator, timeout_ms: int) -> bool:
        """Poll until a control is enabled (Playwright's own click() would
        block 30s on a disabled control with no useful message)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                if locator.is_enabled(timeout=1000):
                    return True
            except PWError:
                pass
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(250)

    def _escape(self) -> None:
        try:
            self.page.keyboard.press("Escape")
        except PWError:
            pass

    def _dump_filter_debug(self, tag: str) -> None:
        """Failure diagnostics for the filter bar: what labels / comboboxes
        the page actually has (with enabled state), plus a screenshot. Once
        per tag per driver so a retry loop can't spam."""
        if tag in self._dumped:
            return
        self._dumped.add(tag)
        try:
            info = self.page.evaluate(r"""() => {
                const txt = e => (e.innerText || '').replace(/\s+/g, ' ')
                                                    .trim().slice(0, 40);
                return {
                  viewport: [window.innerWidth, window.innerHeight],
                  labels: Array.from(document.querySelectorAll('label'))
                               .map(txt).slice(0, 20),
                  triggers: Array.from(document.querySelectorAll(
                      'button[role="combobox"], button[aria-haspopup]'))
                      .slice(0, 12).map(b => ({
                        text: txt(b), role: b.getAttribute('role'),
                        popup: b.getAttribute('aria-haspopup'),
                        disabled: b.disabled})),
                };
            }""")
            self._say("[deliver:debug] " + tag + ": "
                      + json.dumps(info, ensure_ascii=True))
        except Exception:
            pass
        try:
            root = getattr(self.store, "root", None)
            out_dir = (Path(root) / "screens") if root else Path("deliver_debug")
            out_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", tag)[:60]
            self.page.screenshot(path=str(out_dir / (slug + ".png")),
                                 full_page=True)
        except Exception:
            pass

    # ---- filters (doc §2.1, idempotent) -----------------------------------
    def ensure_filters(self, client: str) -> str:
        """doc §2.1(a)(b). Period = combobox, exact match only. User = cmdk
        popover: skip `Other` (`All` sentinel), first item of the first
        non-`Other` group (client name preferred)."""
        self._set_period(self.period)
        user = self._pick_crm_user(client)
        self._log(f"[deliver:filters] period={self.period} user={user}")
        return user

    # -- generic persistent combobox select --------------------------------
    def _select_combobox(self, trigger, option_selector: str, pick_fn,
                         timeout_ms: int = 6000, max_attempts: int = 5,
                         label: str = "option", step: str = "filter"):
        """A single open->click on a Radix combobox / listbox option can
        close the popup without the trigger ever actually updating -- the
        click can land while the listbox is still animating in, or the app
        can re-render the trigger a beat after the click event fires.

        This opens the trigger, asks `pick_fn(options_locator)` to choose an
        option, clicks it, then reads the trigger's OWN text back and only
        returns once it actually reflects the pick. If it doesn't, the
        whole open/pick/click cycle is retried (with an Escape in between
        to close any stuck popup) up to `max_attempts` times before
        raising.

        pick_fn(options) -> (option_locator, confirm_text, ...extra) or
        None if nothing selectable was rendered. `confirm_text` is matched
        against the trigger's post-click text (exact, then substring) to
        decide whether the click actually registered. Returns whatever
        pick_fn returned once confirmed.
        """
        last_seen = None
        picked = None
        for attempt in range(1, max_attempts + 1):
            _center(trigger)
            trigger.click()
            options = self.page.locator(option_selector)
            try:
                options.first.wait_for(state="visible", timeout=timeout_ms)
            except PWError:
                self._log(f"[deliver:filters] {label} popover did not open "
                          f"(attempt {attempt})")
                self.page.wait_for_timeout(200)
                continue
            picked = pick_fn(options)
            if picked is None:
                seen = []
                try:
                    for i in range(min(options.count(), 20)):
                        try:
                            seen.append(
                                options.nth(i).inner_text(timeout=1000)
                                .strip())
                        except PWError:
                            continue
                except PWError:
                    pass
                raise DeliverError(
                    step, f"no selectable {label} option rendered "
                    f"(saw {len(seen)} option(s): {seen!r})")
            option, confirm_text = picked[0], picked[1]
            _center(option)
            option.click()
            self.page.wait_for_timeout(250)
            try:
                last_seen = trigger.inner_text(timeout=2000).strip()
            except PWError:
                last_seen = None
            want = (confirm_text or "").strip().lower()
            got = (last_seen or "").strip().lower()
            # Digits-only fallback: package/amount confirm_text is often a
            # bare number (e.g. "10000") while the trigger renders it with
            # currency/commas (e.g. "Premium ₦10,000"), which fails a plain
            # substring check even though the selection did register.
            want_digits = re.sub(r"[^\d]", "", want)
            got_digits = re.sub(r"[^\d]", "", got)
            digit_match = bool(want_digits) and want_digits in got_digits
            if want and got and (got == want or want in got or digit_match):
                return picked
            self._log(f"[deliver:filters] {label} trigger did not confirm "
                      f"'{confirm_text}' (attempt {attempt}, showed "
                      f"'{last_seen}')")
            # Escape ONLY when a popup is really still open: with no popup
            # open, Escape goes to the Dialog underneath and CLOSES it.
            try:
                if self.page.locator(option_selector).first.is_visible():
                    self.page.keyboard.press("Escape")
            except PWError:
                pass
            self.page.wait_for_timeout(200)
        raise DeliverError(step, f"{label} trigger never confirmed "
                           "selection" + (f" (last showed '{last_seen}')"
                                          if last_seen else ""))

    def _set_period(self, period: str) -> None:
        """doc §2.1(a): exact match only (first option is `All Time`)."""
        trigger = self._first(PERIOD_TRIGGER)
        try:
            current = trigger.inner_text(timeout=5000).strip()
        except PWError:
            current = ""
        if current.lower() == period.lower():
            return

        def _pick(options):
            want = re.sub(r"\s+", " ", period).strip().lower()
            for i in range(options.count()):
                option = options.nth(i)
                try:
                    shown = option.inner_text(timeout=1500).strip()
                except PWError:
                    continue
                if re.sub(r"\s+", " ", shown).strip().lower() == want:
                    return (option, shown)
            return None

        self._select_combobox(trigger, 'div[role="option"]', _pick,
                              label=f"Period '{period}'", step="filter",
                              max_attempts=5)

    # -- User (cmdk popover: doc §2.1(b) + cmdk spec) -----------------------
    @staticmethod
    def _strip_group(label: str) -> str:
        text_value = re.sub(r"\s+", " ", (label or "").strip())
        match = re.match(r"^(Sales Rep|Other|Admin|Manager|Staff)\s+(.*)$",
                         text_value, re.I)
        return (match.group(2) if match else text_value).strip()

    @staticmethod
    def _preferred_users(client: str) -> list:
        raw = (client or "").strip()
        match = re.match(r"^client_(.+?)_[0-9a-f]{6,}$", raw, re.I)
        name = (match.group(1) if match else raw).strip().lower()
        return [name] if name else []

    @staticmethod
    def _user_option_index(options: list, preferred: list):
        first = None
        for index, (value, shown) in enumerate(options):
            name = (DeliverDriver._strip_group(value)
                    or DeliverDriver._strip_group(shown))
            if not name or name.lower() in USER_SENTINELS:
                continue
            if first is None:
                first = index
            if name.lower() in preferred:
                return index
        return first

    def _open_user_popover(self, trigger):
        _center(trigger)
        trigger.click(timeout=10_000)
        for selector in (CMDK_POPOVER, CMDK_POPOVER_FALLBACK):
            popover = self.page.locator(selector).first
            try:
                popover.wait_for(state="visible", timeout=6000)
                return popover
            except PWError:
                continue
        raise DeliverError("filter", "User popover did not open")

    def _user_options(self, popover):
        for selector in (CMDK_ITEM_NOT_OTHER, CMDK_ITEM_SALES_REP, CMDK_ITEM):
            items = popover.locator(selector)
            try:
                count = min(items.count(), 40)
            except PWError:
                continue
            options, handles = [], []
            for i in range(count):
                item = items.nth(i)
                try:
                    value = (item.get_attribute("data-value") or "").strip()
                    shown = item.inner_text(timeout=1500).strip()
                except PWError:
                    continue
                options.append((value, shown))
                handles.append(item)
            if options:
                return options, handles
        return [], []

    def _user_trigger_shows(self, trigger, label: str) -> bool:
        try:
            span = trigger.locator("span").first
            text_value = (span.inner_text(timeout=3000) if span.count()
                          else trigger.inner_text(timeout=3000))
        except PWError:
            return False
        got = re.sub(r"\s+", " ", text_value.strip()).lower()
        want = re.sub(r"\s+", " ", (label or "").strip()).lower()
        return bool(want) and (got == want or got.endswith(want))

    def _pick_crm_user(self, client: str) -> str:
        trigger = self._first(USER_TRIGGER)
        trigger.wait_for(state="visible", timeout=8000)
        # FIX: the CRM keeps User disabled until a "Filter By" chip is
        # applied. Wait for it to be enabled (one chip re-apply if not)
        # instead of a 30s blind click that dies with "element is not
        # enabled".
        if not self._wait_until_enabled(trigger, USER_ENABLE_TIMEOUT_MS):
            self._say("[deliver:filters] User filter is still disabled -- "
                      "re-applying the 'Filter By' chips once")
            self.ensure_form_chips()
            if not self._wait_until_enabled(trigger, USER_ENABLE_TIMEOUT_MS):
                self._dump_filter_debug("user_disabled")
                have = [c for c in self.filter_chips
                        if self._chip_present(c)]
                raise DeliverError(
                    "filter",
                    "User filter is still disabled -- no 'Filter By' chip "
                    "could be applied (Added By / Assigned By present: "
                    f"{have or 'none'}). See the [deliver:debug] lines above")
        preferred = self._preferred_users(client)
        label = ""
        for attempt in (1, 2):
            popover = self._open_user_popover(trigger)
            options, handles = self._user_options(popover)
            index = self._user_option_index(options, preferred)
            if index is None:
                raise DeliverError(
                    "filter", "no selectable User option (only the `Other`/"
                              "`All` sentinel was rendered)")
            label = (self._strip_group(options[index][0])
                     or self._strip_group(options[index][1]))
            _center(handles[index])
            handles[index].click()
            self.page.wait_for_timeout(300)
            if self._user_trigger_shows(trigger, label):
                return label
            self._log("[deliver:filters] user trigger did not confirm '"
                      + label + "' (attempt " + str(attempt) + ")")
        raise DeliverError("filter", "User trigger never confirmed '"
                           + label + "'")

    # -- chips (doc §2.1(c)(d), idempotent) ---------------------------------
    @staticmethod
    def _chip_variants(chip: str) -> tuple:
        variants = CHIP_ALIASES.get(chip, (chip,))
        return (chip,) + tuple(v for v in variants if v != chip)

    def _group_chips(self, group_label: str) -> tuple:
        if group_label == "Order by Forms":
            return (self.form_chip,)
        return self.filter_chips

    def _chip_present(self, label: str) -> bool:
        """A chip is a <span> rendered INSIDE the group's trigger <button>
        (plan doc §2.1 c/d). Case/whitespace tolerant, alias aware
        ("Assigned To" == "Assigned By"); ignores same-text items inside an
        open popup/listbox."""
        try:
            chips = self.page.locator("button").filter(
                has=self.page.locator("span").filter(
                    has_text=self._rx(*self._chip_variants(label))))
            for i in range(min(chips.count(), 6)):
                in_popup = chips.nth(i).evaluate(
                    "e => !!e.closest('[role=\"dialog\"], [role=\"listbox\"], "
                    "[role=\"menu\"], [cmdk-root]')")
                if not in_popup:
                    return True
            return False
        except PWError:
            return False

    def _group_label(self, group_label: str):
        """The group's caption element, or None. Prefers a real <label>;
        falls back to any non-popup element whose text is the caption."""
        rx = self._rx(*GROUP_LABEL_ALIASES.get(group_label, (group_label,)),
                      label=True)
        try:
            labels = self.page.locator("label").filter(has_text=rx)
            if labels.count() > 0:
                return labels.first
            loose = self.page.get_by_text(rx)
            for i in range(min(loose.count(), 6)):
                el = loose.nth(i)
                in_popup = el.evaluate(
                    "e => !!e.closest('[role=\"option\"], "
                    "[role=\"dialog\"], [role=\"menu\"]')")
                if not in_popup:
                    return el
        except PWError:
            pass
        return None

    def _trigger_from_chip(self, group_label: str):
        """If any chip of this group is already rendered, the trigger IS the
        button that contains it (plan doc §2.1 c/d) -- no label needed."""
        for chip in self._group_chips(group_label):
            if self._chip_present(chip):
                return self.page.locator("button").filter(
                    has=self.page.locator("span").filter(
                        has_text=self._rx(*self._chip_variants(chip)))).first
        return None

    def _trigger_near_label(self, label):
        """label -> its OWN trigger. Closest enclosing cell first, and never
        cross into a container that holds more than one <label> (that is the
        filter grid -- the old (2, 3, 1) ancestor order escaped into it and
        returned the Period trigger). Returns (locator, how) or None."""
        try:
            target_id = label.get_attribute("for", timeout=1000)
        except PWError:
            target_id = None
        if target_id:
            target = self.page.locator('[id="' + target_id + '"]').first
            try:
                if (target.count() > 0
                        and target.evaluate("e => e.tagName") == "BUTTON"):
                    return target, "label-for"
            except PWError:
                pass
        for kinds in (('button[role="combobox"]', 'button[aria-haspopup]'),
                      ("button",)):
            for depth in range(1, 6):
                container = label.locator(
                    "xpath=ancestor::div[" + str(depth) + "]")
                try:
                    if container.count() == 0:
                        break
                    if container.locator("label").count() > 1:
                        break              # crossed into the filter grid
                except PWError:
                    break
                for kind in kinds:
                    found = container.locator(kind)
                    try:
                        if found.count() > 0:
                            return found.first, ("ancestor[" + str(depth)
                                                 + "] " + kind)
                    except PWError:
                        continue
        for kind in ('button[@role="combobox"]', "button[@aria-haspopup]",
                     "button"):
            nxt = label.locator("xpath=following::" + kind + "[1]")
            try:
                if nxt.count() > 0:
                    return nxt.first, "following::" + kind
            except PWError:
                continue
        return None

    def _group_trigger(self, group_label: str):
        """Returns (trigger_locator, how) or None."""
        anchored = self._trigger_from_chip(group_label)
        if anchored is not None:
            return anchored, "chip"
        label = self._group_label(group_label)
        if label is None:
            return None
        return self._trigger_near_label(label)

    def _popup_for_trigger(self, timeout_ms: int = 5000):
        popup = self.page.locator(POPUP_SELECTOR).last
        popup.wait_for(state="visible", timeout=timeout_ms)
        return popup

    def _find_option(self, popup, chip: str):
        """Exact-text option preferred, substring second. Returns
        (option_or_None, texts_seen)."""
        wants = [self._norm(v) for v in self._chip_variants(chip)]
        items = popup.locator(OPTION_SELECTORS)
        exact = partial = None
        seen: list = []
        try:
            count = min(items.count(), 40)
        except PWError:
            count = 0
        for i in range(count):
            item = items.nth(i)
            try:
                shown = self._norm(item.inner_text(timeout=1000))
            except PWError:
                continue
            if not shown:
                continue
            seen.append(shown)
            if shown in wants:
                exact = item
                break
            if partial is None and any(w in shown for w in wants):
                partial = item
        return (exact or partial), seen

    @staticmethod
    def _option_selected(option) -> bool:
        """True if a multi-select item is already ticked (so a retry does
        not click it again and UN-tick it)."""
        try:
            return bool(option.evaluate(
                """e => e.getAttribute('aria-selected') === 'true'
                     || e.getAttribute('aria-checked') === 'true'
                     || e.getAttribute('data-state') === 'checked'
                     || !!e.querySelector('[data-state="checked"], '
                                          + '[aria-checked="true"], '
                                          + 'input:checked')"""))
        except PWError:
            return False

    def _wait_for_chip(self, chip: str, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if self._chip_present(chip):
                return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(150)

    def _close_popups(self) -> None:
        try:
            if self.page.locator(POPUP_OPEN_VISIBLE).count() > 0:
                self._escape()
                self.page.wait_for_timeout(150)
        except PWError:
            pass

    def ensure_form_chips(self) -> bool:
        """Applies 'combo set' + 'Added By' + 'Assigned By' when missing.
        Returns True when at least one 'Filter By' chip is present (that is
        what unlocks the User filter)."""
        self._wait_for_filters_ready()
        for group_label, chips in (("Order by Forms", (self.form_chip,)),
                                   ("Filter By", self.filter_chips)):
            for chip in chips:
                if self._chip_present(chip):
                    continue
                if not self._apply_chip(group_label, chip):
                    print("[deliver] warning: filter chip '" + chip
                          + "' not present under '" + group_label
                          + "' -- continuing", flush=True)
        return any(self._chip_present(c) for c in self.filter_chips)

    def _wait_for_filters_ready(self) -> None:
        """Wait for the filter bar itself to exist before touching any chip
        or combobox in it (it renders as a second wave after 'Add Order').
        Uses the Period trigger as the cheapest reliable "filter bar
        mounted" signal, with one reload+retry, mirroring
        _confirm_orders_page.
        """
        trigger = self._first(PERIOD_TRIGGER)
        try:
            trigger.wait_for(state="visible", timeout=FILTERS_READY_TIMEOUT_MS)
            return
        except PWError:
            self._log("[deliver:filters] filter bar not ready -- "
                      "reloading and retrying")
        self.page.reload(wait_until="commit")
        _confirm_orders_page(self.page)
        trigger = self._first(PERIOD_TRIGGER)
        trigger.wait_for(state="visible", timeout=FILTERS_READY_TIMEOUT_MS)

    def _apply_chip(self, group_label: str, chip: str) -> bool:
        """Open the group's OWN trigger, tick `chip` in its popup, Confirm,
        and only return True once the chip is actually rendered."""
        for attempt in range(1, 4):
            found = self._group_trigger(group_label)
            if found is None:
                self._say("[deliver:filters] cannot find the '" + group_label
                          + "' filter (no matching <label> / chip on the "
                          "page; wanted " + repr(self._group_chips(group_label))
                          + ")")
                self._dump_filter_debug("group_" + group_label)
                return False
            trigger, how = found
            self._log("[deliver:filters] '" + group_label + "' trigger via "
                      + how)
            try:
                if not self._wait_until_enabled(trigger, 6000):
                    raise DeliverError(
                        "chip", "'" + group_label + "' trigger is disabled")
                _center(trigger)
                trigger.click(timeout=8000)
                popup = self._popup_for_trigger()
                option, seen = self._find_option(popup, chip)
                if option is None:
                    # The popup's own search narrows the list; a stored chip
                    # that is missing (or moved) must NOT research forever.
                    # One log line with what the popup actually shows, close
                    # it, and return False so the run continues with the
                    # filters that did apply.
                    self._close_popups()
                    self._say("[deliver:filters] option '" + chip
                              + "' (also tried "
                              + repr(self._chip_variants(chip)[1:])
                              + ") not found under '" + group_label
                              + "' (trigger via " + how
                              + "); popup shows: " + repr(seen)
                              + " -- set `filterBy` in the config to one of"
                              " these names")
                    return False
                if not self._option_selected(option):
                    _center(option)
                    option.click(timeout=6000)
                    self.page.wait_for_timeout(200)
                confirm = self.page.get_by_role("button",
                                                name=CONFIRM_BUTTON_RX)
                if confirm.count() and confirm.first.is_visible():
                    _center(confirm.first)
                    confirm.first.click(timeout=6000)
                    self.page.wait_for_timeout(250)
            except (PWError, DeliverError) as exc:
                self._log("[deliver:filters] chip '" + chip + "' attempt "
                          + str(attempt) + " failed: " + str(exc)[:160])
                self._escape()
                self.page.wait_for_timeout(200)
                continue
            if self._wait_for_chip(chip, CHIP_CONFIRM_TIMEOUT_MS):
                self._close_popups()
                return True
            self._log("[deliver:filters] chip '" + chip + "' under '"
                      + group_label + "' did not render (attempt "
                      + str(attempt) + ")")
            self._escape()
            self.page.wait_for_timeout(200)
            if self._wait_for_chip(chip, 1000):
                return True
        self._dump_filter_debug("chip_" + chip)
        return False

    def _search_button(self):
        """Plan doc §2.1 e: role=button name /^Search$/ first; then the
        icon+text fallback; the old loose has-text selector last."""
        for loc in (self.page.get_by_role(
                        "button", name=re.compile(r"^\s*Search\s*$", re.I)),
                    self.page.locator(SEARCH_BUTTON_FALLBACK)):
            try:
                if loc.count() > 0:
                    return loc.first
            except PWError:
                continue
        return self._first(SEARCH_BUTTON_LEGACY)

    def submit_search(self) -> None:
        btn = self._search_button()
        _center(btn)
        btn.click()
        self.page.wait_for_timeout(800)
        wait_for_app_idle(self.page)

    def wait_for_orders_ready(self, timeout_ms: int = 20_000) -> bool:
        """Make sure the orders LIST has rendered before any tab badge or
        row is read (called right after submit_search()).

        FIX (this revision, "the run that read pending=0 delivered=0"): the
        badges are only painted once the filtered list has come back, so
        reading them too early yielded 0/0 and every later search then
        reported "No orders match your filters" (a false NOT_FOUND for a
        customer who was sitting in Pending the whole time). Waits for a
        data row, the app's empty-state text or the list footer, and
        re-clicks Search once if none of them ever shows up.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if self.page.locator(TABLE_ROW_DATA).count() > 0:
                    return True
                if self._empty_state_text() or self._visible_tab():
                    return True
            except PWError:
                pass
            self.page.wait_for_timeout(250)
        self._log("[deliver] the orders list never confirmed as loaded -- "
                  "clicking Search again")
        try:
            seq0 = self._req_seq
            self.submit_search()
            self._wait_search_settled(seq0, min_wait_ms=1500)
        except PWError:
            pass
        return self.page.locator(TABLE_ROW_DATA).count() > 0

    # ---- tabs (doc 2.2: always click for a fresh load) ----------------------
    def _tab(self, name: str):
        """Plan doc §2.2 F: strict `^Name\\s*\\d*$` first (so a tab like
        'Not Delivered' can never win a has-text substring match); the old
        has-text selector as fallback."""
        strict = self.page.locator(TAB_BASE).filter(
            has_text=re.compile(r"^\s*" + re.escape(name) + r"\s*\d*\s*$",
                                re.I))
        try:
            if strict.count() > 0:
                return strict.first
        except PWError:
            pass
        return self._first(PENDING_TAB if name == "Pending"
                           else DELIVERED_TAB)

    def _visible_tab(self) -> str:
        """Which status tab the table is ACTUALLY showing: 'Pending' /
        'Delivered', or '' when that footer text isn't rendered.

        FIX (this revision, "the Delivered tab showed the Pending row"):
        clicking a tab and settling is not proof the tab switched -- a
        slow/absent lazy fetch left the OTHER tab's rows on screen, and
        reading those could mark a still-Pending order as ALREADY_DELIVERED
        in the state store without ever delivering it. The list footer
        ('Showing 1-1 of 1 match in Pending' / '1 match in Pending') is the
        app's own statement of which tab those rows belong to.
        """
        try:
            body = self.page.locator("body").inner_text(timeout=2000)
        except PWError:
            return ""
        found = re.findall(r"match(?:es)?\s+in\s+(Pending|Delivered)", body,
                           re.I)
        return found[-1].title() if found else ""

    def _open_tab(self, name: str) -> int:
        """Click a status tab and make sure the table really switched to it
        (see _visible_tab): one re-click + re-settle when the footer still
        names the other tab, then refuse (instead of reading the wrong
        tab's rows) if it still won't switch. Returns the badge count.

        Speed: when the footer already names this tab the click is skipped --
        the per-customer search forces a fresh query anyway, so the extra
        tab round trip only cost time.
        """
        if self._visible_tab() == name:
            self._log(f"[deliver] already on the {name} tab -- skipping the "
                      "click")
            return read_tab_count(self.page, self._tab(name))
        seen = ""
        for attempt in (1, 2):
            tab = self._tab(name)
            _center(tab)
            seq0 = self._req_seq
            tab.click()
            self._wait_search_settled(seq0,
                                      min_wait_ms=900 if attempt == 1
                                      else 1500)
            wait_for_app_idle(self.page)
            seen = self._visible_tab()
            if seen == name or not seen:
                break
            self._log(f"[deliver] the {name} tab still shows {seen} "
                      f"(attempt {attempt}) -- clicking it again")
        if seen and seen != name:
            raise DeliverError(
                "tab", f"clicked the {name} tab but the table still shows "
                       f"{seen} -- refusing to read the wrong tab's rows")
        return read_tab_count(self.page, self._tab(name))

    def open_pending_tab(self) -> int:
        return self._open_tab("Pending")

    def open_delivered_tab(self) -> int:
        return self._open_tab("Delivered")

    # ---- search (doc §8/§9) ---------------------------------------------------
    def search_customer(self, name: str) -> int:
        box = self._first(QUICK_SEARCH)
        try:
            box.wait_for(state="visible", timeout=6000)
        except PWTimeoutError:
            box = self._first(QUICK_SEARCH_FALLBACK)
            box.wait_for(state="visible", timeout=6000)
        _center(box)
        box.click()
        box.fill("")
        box.press_sequentially(name[:60], delay=25)
        seq0 = self._req_seq
        box.press("Enter")
        # FIX: wait for the filtered result to LAND. wait_for_app_idle()
        # alone returns instantly when the (stale) full list is on screen.
        self._wait_search_settled(seq0)
        wait_for_app_idle(self.page)
        return self.page.locator(TABLE_ROW_DATA).count()

    def _search_box_value(self) -> str:
        """What the quick-search box holds right now ('' if unreadable)."""
        try:
            return self._first(QUICK_SEARCH).input_value(timeout=2000).strip()
        except PWError:
            return ""

    def _search_box_holds(self, name: str) -> bool:
        """True when the quick-search box really holds this customer's name.

        This is the guard that turns 'the table shows 0 rows' into 'the row
        moved' -- rather than 'the search never landed / the box lost its
        text', which must NOT be read as success.
        """
        typed = self._clean(self._search_box_value())
        wanted = self._clean(name)
        return bool(typed) and (typed == wanted[:60]
                                or wanted.startswith(typed))

    def clear_search(self) -> None:
        """Tab badges are filtered by the quick-search text, so clear it
        before reading a count."""
        box = self._first(QUICK_SEARCH)
        try:
            box.wait_for(state="visible", timeout=4000)
        except PWError:
            box = self._first(QUICK_SEARCH_FALLBACK)
        try:
            if box.input_value(timeout=2000) == "":
                return
        except PWError:
            pass
        _center(box)
        box.click()
        box.fill("")
        seq0 = self._req_seq
        box.press("Enter")
        self._wait_search_settled(seq0)

    @staticmethod
    def _name_in_cell(cell) -> str:
        """Customer name from the Customer Name cell.

        FIX (this revision, "NOT_FOUND for a customer the Pending table DOES
        show"): the name is rendered as one `span.block` PER LINE (see
        join_name_parts), so reading only `.first` truncated it -- the real
        row was on screen, the Excel name "Oladunni folasade Ramat" was
        compared against "Oladunni folasade", the exact match failed and the
        old whole-word fallback only ever looked the other way round.
        ALL blocks are joined now.
        """
        parts = []
        try:
            blocks = cell.locator("span.block")
            count = blocks.count()
            if count > 4:                 # a name cell never carries more
                count = 4
            parts = [blocks.nth(i).inner_text(timeout=1500)
                     for i in range(count)]
        except PWError:
            parts = []
        if parts:
            return join_name_parts(parts)
        # fallback: the name WRAPPER (the `span` that isn't the timestamp),
        # whose inner text carries the name lines for any layout
        try:
            wrap = cell.locator("div.font-medium > span:not(.mono)").first
            if wrap.count() > 0:
                return join_name_parts(
                    wrap.inner_text(timeout=1500).split("\n"))
        except PWError:
            pass
        return cell.inner_text(timeout=2000).split("\n")[0].strip()

    def _cell_name(self, row) -> str:
        try:
            return self._name_in_cell(
                row.locator("td").nth(CUSTOMER_CELL_INDEX))
        except PWError:
            return ""

    def _empty_state_text(self) -> str:
        """The app's own empty-state sentence (e.g. 'No orders match your
        filters'), or '' when the list does have rows."""
        try:
            body = self.page.locator("body").inner_text(timeout=1500)
        except PWError:
            return ""
        match = re.search(r"no (?:orders|results|data)[^\n]{0,40}", body,
                          re.I)
        return match.group(0).strip() if match else ""

    def _table_snapshot(self, limit: int = 6):
        """(row_count, first customer names, empty-state text) -- what the
        table ACTUALLY shows right now, for NOT_FOUND diagnostics. Only DATA
        rows are counted (TABLE_ROW_DATA), so an empty result reads 0 and is
        no longer reported as "1 row(s) but none match"."""
        try:
            rows = self.page.locator(TABLE_ROW_DATA)
            count = rows.count()
            names = [self._cell_name(rows.nth(i))
                     for i in range(min(count, limit))]
        except PWError:
            return 0, [], ""
        return count, names, self._empty_state_text()

    @staticmethod
    def _fmt_snapshot(snap) -> str:
        count, names, empty = snap
        text_value = f"{count} row(s)"
        if names:
            text_value += " " + repr(names) + ("..." if count > len(names)
                                                else "")
        if empty:
            text_value += f" [{empty}]"
        return text_value

    def _search_and_find(self, name: str, phones=None):
        """Search, then look for the row. If the table still has rows but none
        is this customer, the result may not have refreshed (or an older,
        slower response overwrote it) -- search once more before giving up.
        `phones` is passed to find_row() so same-named customers are told
        apart. Returns (row_or_None, snapshot)."""
        snap = (0, [], "")
        for attempt in (1, 2):
            self.search_customer(name)
            row = self.find_row(name, phones)
            snap = self._table_snapshot()
            if row is not None:
                return row, snap
            if snap[0] == 0:
                break                       # explicit empty result
            # Print WHAT the table showed: a name-shape difference (the CRM
            # holding only part of the name) is then visible in the log.
            self._log(f"[deliver] {name}: table shows {snap[0]} row(s) "
                      f"{snap[1]!r} but none match -- re-searching "
                      f"(attempt {attempt})")
        return None, snap

    # ---- row / orderId / status (doc §2/§10) ---------------------------------
    @staticmethod
    def _phones_in_cell(cell) -> list:
        """Phone number(s) in a Contact cell: the `tel:` and `wa.me` links
        (which can be different numbers), else the cell's visible text."""
        hrefs = []
        try:
            hrefs = cell.locator("a").evaluate_all(
                "els => els.map(e => e.getAttribute('href') || '')")
        except PWError:
            hrefs = []
        phones = phones_from_hrefs(hrefs)
        if phones:
            return phones
        try:                        # no links rendered: read the raw text
            text = cell.inner_text(timeout=2000)
        except PWError:
            return []
        return phone_keys(re.findall(r"\+?\d[\d\s()-]{7,}", text))

    def _row_phones(self, cells) -> list:
        try:
            return self._phones_in_cell(cells.nth(CONTACT_CELL_INDEX))
        except PWError:
            return []

    def find_row(self, name: str, phones=None):
        """The row for this customer.

        Exact customer-name match first (whitespace/case-insensitive, plan
        doc §2.4 'exact'). When SEVERAL rows carry that exact name (the CRM
        allows a name to repeat), the Contact number(s) decide: the sheet's
        phone/WhatsApp (load_customer_phones) is compared against each row's
        Contact cell and the matching row wins. If no row's phone matches,
        the FIRST row is used -- the CRM's own order for that name -- and,
        when more than one row was on screen, that is reported.

        The only fallback is a GUARDED partial-name match, accepted only
        when exactly one row on screen qualifies ('Oladunni folasade' for
        the sheet's 'Oladunni folasade Ramat', or the other way round).
        Never a bare substring and never a guess between two candidates:
        this moves real orders.
        """
        want = self._clean(name)
        wanted_phones = phone_keys(phones)
        rows = self.page.locator(TABLE_ROW_DATA)
        n = rows.count()
        exact = []
        candidates = []
        for i in range(min(n, 20)):
            row = rows.nth(i)
            try:
                cells = row.locator("td")
                if cells.count() < 6:
                    continue
                shown = self._clean(
                    self._name_in_cell(cells.nth(CUSTOMER_CELL_INDEX)))
            except PWError:
                continue
            if shown == want:
                if not wanted_phones:
                    return row                  # first exact row wins
                exact.append((row, cells))
                continue
            if len(candidates) < 2 and is_name_candidate(want, shown):
                candidates.append((row, shown))
        if exact:
            return self._pick_by_phone(name, exact, wanted_phones)
        if len(candidates) == 1:
            row, shown = candidates[0]
            self._log(f"[deliver] partial name match: '{name}' -> CRM row "
                      f"shows '{shown}' (only candidate on screen)")
            return row
        if len(candidates) > 1:
            self._say(f"[deliver] '{name}': {len(candidates)} rows could "
                      f"match by name {[c[1] for c in candidates]!r} -- not "
                      "guessing, treating as no match")
        return None

    def _pick_by_phone(self, name: str, exact: list, wanted: list):
        """Rows that carry the exact same name: Contact numbers decide."""
        seen: list = []
        for row, cells in exact:
            row_phones = self._row_phones(cells)
            for phone in row_phones:
                if phone not in seen:
                    seen.append(phone)
            if phone_matches(row_phones, wanted):
                if len(exact) > 1:
                    self._log(f"[deliver] {name}: {len(exact)} rows share "
                              f"this name -- picked the one whose Contact "
                              f"matches the sheet ({wanted[0]})")
                return row
        if len(exact) > 1:
            self._say(f"[deliver] {name}: {len(exact)} rows share this name "
                      f"and none matches the sheet phone(s) {wanted!r} "
                      f"(Contact showed {seen!r}) -- using the first row")
        else:
            self._log(f"[deliver] {name}: row Contact {seen!r} does not "
                      f"match the sheet phone(s) {wanted!r} -- same name, "
                      "continuing")
        return exact[0][0]

    def read_row(self, row) -> dict:
        cells = row.locator("td")
        info = {
            "orderId": "",
            "status": "",
            "amount": 0,
            "customer": "",
            "phone": "",
            "phones": [],
        }
        try:
            info["customer"] = self._name_in_cell(
                cells.nth(CUSTOMER_CELL_INDEX))
        except PWError:
            pass
        try:
            phones = self._phones_in_cell(cells.nth(CONTACT_CELL_INDEX))
            info["phones"] = phones
            info["phone"] = phones[0] if phones else ""
        except PWError:
            pass
        try:
            info["orderId"] = cells.nth(2).inner_text(timeout=2000).strip()
        except PWError:
            pass
        try:
            info["status"] = cells.nth(STATUS_CELL_INDEX).inner_text(
                timeout=2000).strip().split("\n")[0].strip()
        except PWError:
            pass
        try:
            product_text = cells.nth(8).inner_text(timeout=2000)
            # Plan doc §2.8 b: prefer the labeled `Amount: ₦N`; only fall
            # back to the first ₦ figure when no label is present.
            m = (AMOUNT_LABELED_RE.search(product_text)
                 or AMOUNT_RE.search(product_text))
            if m:
                info["amount"] = int(m.group(1).replace(",", ""))
        except PWError:
            pass
        return info


    # ---- menu -> Delivered modal (doc §11/§12) -------------------------------
    def open_move_to_delivered(self, row, started: float) -> None:
        trigger = row.locator(ROW_MENU_TRIGGER).first
        _center(trigger)
        trigger.click()
        menu = self._first(OPEN_MENU)
        menu.wait_for(state="visible", timeout=6000)
        move = self._first('div[role="menuitem"]:has-text("Move to status")')
        _center(move)
        move.hover()
        self._first(SUBMENU).wait_for(state="visible", timeout=6000)
        delivered = self.page.locator(
            f'{SUBMENU} div[role="menuitem"]').filter(
            has_text=re.compile(r"^Delivered\s*$")).first
        _center(delivered)
        delivered.click()
        dialog = self.page.locator(
            f'{MODAL}:has(h2:text-is("{MODAL_TITLE}"))').first
        dialog.wait_for(state="visible", timeout=8000)
        if self._deadline_left(started) <= 0:
            raise WatchdogTimeout("modal open exceeded deadline")

    # ---- modal fields (doc §12-§15) -------------------------------------------
    def _modal_trigger(self, dialog, index: int, caption_rx: str):
        """Modal select trigger: plan doc P (`button[role=combobox]` nth) and,
        because several triggers on this CRM carry NO role (aria-haspopup
        only), a label-anchored fallback."""
        combos = dialog.locator('button[role="combobox"]')
        try:
            if combos.count() > index:
                return combos.nth(index)
        except PWError:
            pass
        try:
            label = dialog.locator("label").filter(
                has_text=re.compile(caption_rx, re.I)).first
            if label.count() > 0:
                found = self._trigger_near_label(label)
                if found is not None:
                    self._log("[deliver] modal trigger via " + found[1])
                    return found[0]
        except PWError:
            pass
        return combos.nth(index)      # let Playwright raise a clear error

    def _pick_agent(self, dialog) -> None:
        """Persistent select + trigger-text confirmation (see
        _select_combobox) -- a click on the Agent option used to be
        trusted blindly, so a click that didn't register left the modal
        with no agent selected and the failure only surfaced much later
        (Save disabled, or a downstream field never populating)."""
        trigger = self._modal_trigger(dialog, 0, r"select from|agent")

        def _pick(options):
            n = options.count()
            exact = None
            fallback = None
            for i in range(n):
                text = options.nth(i).inner_text(timeout=1500).strip()
                if fallback is None:
                    fallback = (options.nth(i), text)
                if text.lower() == self.agent.lower():
                    exact = (options.nth(i), text)
                    break
            if exact is not None:
                return exact
            if fallback is not None:
                print(f"[deliver] agent fallback used -> {fallback[1]}",
                      flush=True)      # doc §12 fallback, logged
                return fallback
            return None

        self._select_combobox(
            trigger,
            'div[role="option"], div[role="listbox"] div[role="option"]',
            _pick, label="Agent", step="agent")

    def _pick_package(self, dialog, expected_amount: int) -> dict:
        """doc §13: exact, else smallest >= expected, else first + warn.
        Persistent select + trigger-text confirmation (see
        _select_combobox) -- previously the click was trusted blindly and
        only caught much later (and only indirectly) by
        _total_amount_ok() failing to populate."""
        trigger = self._modal_trigger(dialog, 1, r"select package|package")
        info: dict = {}

        def _pick(options):
            n = options.count()
            parsed = []
            for i in range(n):
                text = options.nth(i).inner_text(timeout=1500)
                m = AMOUNT_RE.search(text)
                amt = int(m.group(1).replace(",", "")) if m else None
                parsed.append((options.nth(i), amt, text.strip()[:50]))
            exact = next(((o, a, t) for o, a, t in parsed
                          if a == expected_amount and expected_amount > 0),
                         None)
            ge = sorted(((o, a, t) for o, a, t in parsed
                         if a is not None and expected_amount
                         and a >= expected_amount), key=lambda x: x[1])
            chosen = exact or (ge[0] if ge else None) or (
                parsed[0] if parsed else None)
            if chosen is None:
                return None
            option, amt, text = chosen
            info["expectedAmount"] = expected_amount
            info["selectedPackageAmount"] = amt
            info["packageFallback"] = exact is None
            info["packageLabel"] = text
            confirm_text = str(amt) if amt is not None else text[:20]
            return (option, confirm_text)

        self._select_combobox(
            trigger,
            'div[role="option"], div[role="listbox"] div[role="option"]',
            _pick, label="Package", step="package")
        if not info:
            raise DeliverError("package", "no package options rendered")
        return info

    def _total_amount_ok(self, dialog) -> bool:
        try:
            value = dialog.locator('input[type="number"]').first.input_value(
                timeout=4000)
            return bool(value) and float(value) > 0
        except Exception:
            return False


    # ---- save + verify (doc §15/§16) -------------------------------------------
    def _assert_modal_open(self, dialog, after: str) -> None:
        """The dialog must still be there after every field. If it isn't,
        say so right away instead of failing later on a detached element."""
        try:
            if dialog.count() > 0:
                return
        except PWError:
            return
        note = self._visible_alerts()
        raise DeliverError(
            "modal", f"the 'Mark order as Delivered' modal closed "
            f"unexpectedly after the {after} step"
            + (f"; page says: {note}" if note else ""))

    def _save_button(self, dialog):
        """The Save button INSIDE the dialog (last match): exact role+name
        first, then the exact-text and `bg-primary` variants."""
        for loc in (dialog.get_by_role(
                        "button", name=re.compile(r"^\s*Save\s*$", re.I)),
                    dialog.locator('button:text-is("Save")'),
                    dialog.locator('button.bg-primary:has-text("Save")')):
            try:
                if loc.count() > 0:
                    return loc.last
            except PWError:
                continue
        return dialog.get_by_role("button", name="Save").last

    def _modal_state(self, dialog) -> str:
        """One line describing what the modal currently holds."""
        def _txt(loc) -> str:
            try:
                return loc.inner_text(timeout=1000).strip().replace(
                    "\n", " ")[:60]
            except PWError:
                return "?"
        parts = []
        try:
            parts.append("agent=" + repr(_txt(
                self._modal_trigger(dialog, 0, r"select from|agent"))))
            parts.append("package=" + repr(_txt(
                self._modal_trigger(dialog, 1, r"select package|package"))))
            parts.append("total=" + repr(dialog.locator(
                'input[type="number"]').first.input_value(timeout=1000)))
        except PWError:
            pass
        try:
            save = self._save_button(dialog)
            parts.append("save=" + ("enabled" if save.is_enabled(timeout=1000)
                                    else "DISABLED"))
        except PWError:
            pass
        note = self._visible_alerts()
        if note:
            parts.append("page says: " + note)
        return ", ".join(parts)

    def _wait_popups_closed(self, timeout_ms: int = 3000) -> None:
        """A select popup that is still closing can swallow the Save click."""
        deadline = time.monotonic() + timeout_ms / 1000
        selector = '[role="listbox"]:visible, [cmdk-root]:visible'
        while time.monotonic() < deadline:
            try:
                if self.page.locator(selector).count() == 0:
                    return
            except PWError:
                return
            self.page.wait_for_timeout(150)
        try:                                   # still open: close only that
            if self.page.locator(selector).count() > 0:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(250)
        except PWError:
            pass

    @staticmethod
    def _same_amount(text_value: str, price) -> bool:
        digits = re.sub(r"[^\d.]", "", (text_value or "").replace(",", ""))
        try:
            return abs(float(digits) - float(price)) < 0.005
        except ValueError:
            return False

    def _apply_price(self, dialog, price, source: str) -> dict:
        """Make the Total Amount field equal `price`.
        No Backspace / Delete (the form validates on every keystroke and
        races on the empty intermediate state): the field is focused and its
        whole content SELECTED, then the price is inserted in ONE paste-style
        input event that replaces the selection. Verified after a settle
        pause, and re-checked once more before returning."""
        field = dialog.locator('input[type="number"]').first
        try:
            before = field.input_value(timeout=4000)
        except PWError:
            before = ""
        info = {"formTotalBefore": before, "targetPrice": price,
                "priceSource": source, "priceOverwritten": False}
        if self._same_amount(before, price):
            return info
        want = str(int(price)) if float(price) == int(price) else str(price)
        shown = before
        for attempt in (1, 2, 3):
            field.click(timeout=5000)
            if attempt < 3:
                field.select_text(timeout=3000)
                self.page.keyboard.insert_text(want)      # replaces selection
            else:
                field.fill(want)                          # last resort
            self.page.wait_for_timeout(500)               # validation settles
            try:
                shown = field.input_value(timeout=2000)
            except PWError:
                shown = ""
            if self._same_amount(shown, price):
                self.page.wait_for_timeout(400)           # ...and stays put
                try:
                    shown = field.input_value(timeout=2000)
                except PWError:
                    shown = ""
                if self._same_amount(shown, price):
                    info["priceOverwritten"] = True
                    return info
        raise DeliverError(
            "price", f"could not set Total Amount to {want} "
            f"(form total was '{before}', now '{shown}')")

    def fill_and_save(self, info: dict, target_price=0,
                      price_source: str = "crm") -> dict:
        dialog = self.page.locator(
            f'{MODAL}:has(h2:text-is("{MODAL_TITLE}"))').first
        
        self._pick_agent(dialog)
        self._assert_modal_open(dialog, "agent")
        
        expected = target_price or info.get("amount", 0)
        # Order matters (Save stays disabled until all three are valid):
        # 1) Agent  2) Package  3) Total Amount / price  4) Save.
        
        package_info = self._pick_package(dialog, expected)
        self._assert_modal_open(dialog, "package")
        # doc §14: total must auto-populate. When we are going to set the
        # price ourselves an empty field is not a problem.
        will_set_price = bool(self.overwrite_price and target_price)
        if not will_set_price and not self._total_amount_ok(dialog):
            self.page.wait_for_timeout(1200)
            if not self._total_amount_ok(dialog):
                package_info = self._pick_package(dialog, expected)
                self.page.wait_for_timeout(1200)
                if not self._total_amount_ok(dialog):
                    raise DeliverError("total_amount",
                                       "did not populate after reselect")
        if will_set_price:
            self.page.wait_for_timeout(400)      # let the auto-fill land
            package_info.update(self._apply_price(dialog, target_price,
                                                  price_source))
            self._assert_modal_open(dialog, "price")
        save = self._save_button(dialog)
        save.wait_for(state="visible", timeout=6000)
        if not self._wait_until_enabled(save, 5000):
            raise DeliverError(
                "save", "save_button_disabled -- " + self._modal_state(dialog))
        self._wait_popups_closed()
        self._assert_modal_open(dialog, "final check")
        self._log("[deliver] modal ready: " + self._modal_state(dialog))
        # doc §15: persist READY_TO_SAVE, then click Save. No _center() here:
        # the dialog is a fixed layer; Playwright scrolls only if it must.
        self._safe_checkpoint()
        save.click(timeout=8000)
        try:
            dialog.wait_for(state="detached", timeout=9000)
        except PWError:
            # The modal is still open: Save was rejected (validation error,
            # server error, ...). Report the page's own words and close it
            # so the next step is not blocked by an overlay.
            note = self._visible_alerts()
            self._dismiss_modal(dialog)
            raise DeliverError(
                "save", "modal still open 9s after Save"
                + (f"; page says: {note}" if note else ""))
        # The modal closed on its own: the CRM accepted the Save. That is
        # the evidence verify_delivered() uses for its fast path (see there).
        package_info["modalClosed"] = True
        return package_info

    def _dismiss_modal(self, dialog) -> None:
        """Escape first; if the modal is still open, its Close / Cancel."""
        self._escape()
        self.page.wait_for_timeout(300)
        for selector in (None, 'button[aria-label="Close"]',
                         'button:has(span.sr-only:text-is("Close"))',
                         'button:has-text("Cancel")'):
            try:
                if dialog.count() == 0:
                    return
                if selector is None:
                    continue
                btn = dialog.locator(selector).first
                if btn.count() > 0:
                    btn.click(timeout=3000)
                    self.page.wait_for_timeout(300)
            except PWError:
                continue

    def _visible_alerts(self) -> str:
        """Text of visible alerts/toasts/validation messages (best effort)."""
        try:
            texts = self.page.evaluate(r"""() => Array.from(document.querySelectorAll(
                '[role="alert"], [role="status"], [data-sonner-toast], '
                + '[data-radix-toast-viewport] *, .text-destructive, .text-red-500'))
                .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
                .filter(t => t && t.length < 240)""")
            seen: list = []
            for t in texts:
                if t not in seen:
                    seen.append(t)
            return " | ".join(seen[:4])
        except Exception:
            return ""

    def verify_delivered(self, name: str, started: float,
                         save_confirmed: bool = True) -> bool:
        """doc §16/§27: prove the move on the CRM itself, CHEAPEST check
        first.

        FIX (this revision, "the clean Save is already the proof"): once
        Save has been clicked and the modal closed on its own, the row
        leaving the Pending tab IS the CRM's own answer that the order
        moved -- no Delivered-tab round trip and no re-query loop (that trip
        was most of the per-customer verification time). The slower checks
        are kept exactly where they are still needed:
          * the row is still Pending -> a real failure (safe retry path);
          * the name is gone WITHOUT a clean Save (the idempotency
            pre-check) -> the Delivered tab is checked;
          * the search box no longer holds the name -> the empty table
            cannot be trusted, so Delivered is checked.
        Sets self.last_verify to how it was confirmed: 'row-delivered',
        'pending-gone' or 'delivered-tab'.
        """
        self.last_verify = ""
        phones = self.customer_phones(name)
        for attempt in range(VERIFY_ATTEMPTS):
            if self._deadline_left(started) <= 0:
                raise WatchdogTimeout("verification exceeded deadline")
            self.open_pending_tab()
            row, snap = self._search_and_find(name, phones)
            if row is not None:
                status = self.read_row(row)["status"].lower()
                if status.startswith("delivered"):
                    self.last_verify = "row-delivered"
                    return True
                if status.startswith("pending"):
                    if save_confirmed:
                        # the list can lag the Save by a beat: one more look
                        self.page.wait_for_timeout(700)
                        row2 = self.find_row(name, phones)
                        if row2 is not None and self.read_row(
                                row2)["status"].lower().startswith(
                                    "delivered"):
                            self.last_verify = "row-delivered"
                            return True
                    return False            # doc §6: safe retry path
                continue                    # unexpected status: look again
            # The name is gone from the Pending tab entirely.
            if save_confirmed and snap[0] == 0 and self._search_box_holds(
                    name):
                self._log(f"[deliver] {name}: gone from Pending right after "
                          "a clean Save -> Delivered")
                self.last_verify = "pending-gone"
                return True
            self.open_delivered_tab()
            self.search_customer(name)
            if self.find_row(name, phones) is not None:
                self.last_verify = "delivered-tab"
                return True
            time.sleep(min(1.5, backoff_delay(attempt + 1)))
        return False


    # ---- one customer, full recovery envelope (doc §18-§22, §28) -------------
    def process_customer(self, name: str) -> str:
        """Returns the final classification for one customer:
        SUCCESS / ALREADY_DELIVERED / NOT_FOUND / FAILED_FINAL / SKIPPED.
        All browser/network failures are contained here; nothing escapes
        to the top level (doc §31)."""
        started = time.monotonic()
        self.last_detail = ""
        self.last_note = ""
        sheet_phones = self.customer_phones(name)
        rec = self.store.record(name, phone=sheet_phones)
        if rec.state == SUCCESS and self.store.is_done(name, sheet_phones):
            self._log(f"[deliver] {name}: already completed -- skipping")
            return "SKIPPED"
        try:
            return self._process_customer_inner(name, rec, started)
        except ShutdownRequested:
            raise
        except WatchdogTimeout as exc:
            self.store.evidence(self.page, rec, "watchdog", str(exc))
            self.store.mark_failed(rec, "watchdog", str(exc), final=False)
            self.last_detail = f"watchdog: {exc}"
            self._say(f"[deliver] {name}: FAILED (watchdog) -> {exc}")
            return "FAILED"
        except (PWError, DeliverError) as exc:
            # doc §21: browser-level failures get dedicated recovery
            step = getattr(exc, "step", "browser")
            reason = str(exc)
            short = ((getattr(exc, "reason", None) or reason).strip()
                     .splitlines() or [""])[0][:300]
            self.last_detail = f"{step}: {short}"
            if self._is_browser_failure(exc) and (
                    self.browser_recoveries < MAX_BROWSER_RECOVERIES):
                self.browser_recoveries += 1
                self.store.evidence(self.page, rec, step, f"browser: {reason}")
                self.store.mark_failed(rec, step, f"browser: {reason}",
                                       final=False)
                raise BrowserRecoveryNeeded(reason) from exc
            if self._is_network_failure(exc):
                self.network_recoveries += 1
                self.store.evidence(self.page, rec, step, f"network: {reason}")
                self.store.mark_failed(
                    rec, step, f"network: {reason}", final=False)
                print(f"[deliver] {name}: network issue at {step} ({short}) "
                      f"-> waiting (recovery #{self.network_recoveries})",
                      flush=True)
                wait_for_network(self.store)
                return "RETRY_NETWORK"
            self.store.evidence(self.page, rec, step, reason)
            self.store.mark_failed(rec, step, reason, final=False)
            self._say(f"[deliver] {name}: FAILED at {step} -> {short}")
            return "FAILED"
        except Exception as exc:                     # noqa: BLE001
            # Anything unexpected (store bug, parsing bug, ...) stays inside
            # this customer: report it, keep the run alive. If a Save had
            # already been clicked, the next round finds the order Delivered
            # on the CRM and records ALREADY_DELIVERED.
            short = f"{type(exc).__name__}: {str(exc)[:240]}"
            self.last_detail = f"unexpected: {short}"
            self._say(f"[deliver] {name}: FAILED (unexpected) -> {short}")
            if self.verbose:
                traceback.print_exc()
            try:
                self.store.mark_failed(rec, "unexpected", short, final=False)
            except Exception:                        # noqa: BLE001
                pass
            return "FAILED"

    @staticmethod
    def _is_browser_failure(exc: Exception) -> bool:
        text = str(exc).lower()
        markers = ("target closed", "page closed", "browser disconnected",
                   "execution context destroyed", "browser has been closed")
        return isinstance(exc, PWError) and any(m in text for m in markers)

    @staticmethod
    def _is_network_failure(exc: Exception) -> bool:
        """Hard network markers always count. A bare Playwright timeout
        (selector not found, control disabled, ...) only counts when the
        connectivity probe really fails -- otherwise it is a normal step
        failure and must be reported as such, not as a 'network issue'."""
        if not isinstance(exc, PWError):
            return False
        text = str(exc).lower()
        hard = ("net::err", "ns_error", "err_connection", "connection",
                "network")
        if any(m in text for m in hard):
            return True
        if "timeout" in text or "timed out" in text:
            return not _probe_network()
        return False

    def _close_stray_layers(self) -> None:
        """Close a leftover row menu / Delivered modal after a failed step."""
        try:
            dialog = self.page.locator(
                f'{MODAL}:has(h2:text-is("{MODAL_TITLE}"))').first
            if dialog.count() > 0:
                self._dismiss_modal(dialog)
            elif self.page.locator(OPEN_MENU).count() > 0:
                self._escape()
                self.page.wait_for_timeout(200)
        except Exception:                            # noqa: BLE001
            pass

    def customer_phones(self, name: str) -> list:
        """The sheet's phone number(s) for this customer (may be empty).

        The second half of a customer's identity: two different people can
        share a name, so the Contact number decides which row is theirs and
        whether an earlier success was really theirs.
        """
        return list(self.phones.get(clean_name(name), []))

    def _target_price(self, name: str, info: dict):
        """(price, source): the Excel price when the workbook has a price
        column, else the order's Amount from the CRM row, else (0, 'none')."""
        excel = self.prices.get(clean_name(name))
        if excel:
            if info.get("amount") and info["amount"] != excel:
                self._log(f"[deliver] {name}: Excel price {excel} differs "
                          f"from the CRM row Amount {info['amount']} -- "
                          "using the Excel price")
            return excel, "excel"
        if info.get("amount"):
            return info["amount"], "crm"
        return 0, "none"

    def _process_customer_inner(self, name: str, rec: OrderRecord,
                                started: float) -> str:
        sheet_phones = self.customer_phones(name)
        # doc §10: idempotency pre-check on the CRM itself
        self.open_pending_tab()
        row, snap_pending = self._search_and_find(name, sheet_phones)
        if row is None:
            # The name is NOT in Pending: only now is the Delivered tab worth
            # a visit (it costs a tab click plus a search).
            self.open_delivered_tab()
            row_d, snap_delivered = self._search_and_find(name, sheet_phones)
            if row_d is not None:
                info_d = self.read_row(row_d)
                order_id = info_d["orderId"]
                self.store.mark_success(
                    rec, order_id or f"name:{name}",
                    phone=info_d.get("phones") or sheet_phones)
                self._log(f"[deliver] {name}: already Delivered on CRM "
                          f"(id={order_id})")
                return "ALREADY_DELIVERED"
            self.last_detail = (
                f"searched '{name}' -> Pending tab: "
                f"{self._fmt_snapshot(snap_pending)}; Delivered tab: "
                f"{self._fmt_snapshot(snap_delivered)}")
            self._say(f"[deliver] {name}: NOT_FOUND -- {self.last_detail}")
            self.store.evidence(self.page, rec, "not_found", self.last_detail)
            return "NOT_FOUND"
        info = self.read_row(row)
        order_id = info["orderId"]
        row_phones = info.get("phones") or sheet_phones
        rec.data["orderId"] = order_id
        self._safe_checkpoint()
        if info["status"].lower().startswith("delivered"):
            self.store.mark_success(rec, order_id, phone=row_phones)
            self._log(f"[deliver] {name}: row already Delivered "
                      f"(id={order_id})")
            return "ALREADY_DELIVERED"
        if not info["status"].lower().startswith("pending"):
            self.last_detail = (f"unexpected status '{info['status']}' "
                                f"(id={order_id}) -- not touching")
            self._say(f"[deliver] {name}: SKIPPED -- {self.last_detail}")
            return "SKIPPED"
        # happy path (doc §30)
        target_price, price_source = self._target_price(name, info)
        self._safe_transition(rec, ROW_FOUND, orderId=order_id)
        try:
            self.open_move_to_delivered(row, started)
            self._safe_transition(rec, MODAL_OPEN)
            package_info = self.fill_and_save(info, target_price,
                                              price_source)
        except BaseException:
            # A half-finished step must not leave a menu or the modal open:
            # the overlay would block every later click of this session.
            self._close_stray_layers()
            raise
        if package_info.get("priceOverwritten"):
            self.last_note = (
                f"price {package_info.get('formTotalBefore') or '?'} -> "
                f"{package_info['targetPrice']} ({price_source})")
            self._say(f"[deliver] {name}: Total Amount {self.last_note}")
        # Save has been clicked from here on: nothing below may abort the
        # verification against the CRM (dicts become JSON text, see
        # _safe_transition).
        self._safe_transition(rec, SAVE_SUBMITTED, package=package_info)
        self._safe_checkpoint()
        self._safe_transition(rec, VERIFYING)
        # The modal closed on its own -> the CRM took the Save, so a Pending
        # search that no longer shows this customer is already the answer
        # (see verify_delivered: no Delivered-tab trip needed).
        save_confirmed = bool(package_info.get("modalClosed"))
        if self.verify_delivered(name, started,
                                 save_confirmed=save_confirmed):
            self.store.mark_success(rec, order_id, phone=row_phones)
            self._log(f"[deliver] {name}: verified Delivered on CRM "
                      f"(id={order_id}, via {self.last_verify or 'verify'}, "
                      f"package={package_info['packageLabel']})")
            return "SUCCESS"
        self.store.mark_failed(rec, "verify",
                               "crm never showed Delivered", final=False)
        self.last_detail = ("verify: Save clicked but the CRM never showed "
                            f"Delivered (id={order_id})")
        self._say(f"[deliver] {name}: FAILED -- {self.last_detail}")
        return "FAILED"
