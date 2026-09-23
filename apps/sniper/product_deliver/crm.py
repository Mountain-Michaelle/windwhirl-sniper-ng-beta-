"""Page-level primitives: idle waits, viewport, centering, tabs."""

from __future__ import annotations

import re
import time
from apps.sniper.run_upload import (
    LOGGED_IN_SELECTOR,
)
from playwright.sync_api import (
    Error as PWError,
    TimeoutError as PWTimeoutError,
)
from apps.sniper.product_deliver.constants import (
    LOGIN_NETWORK_WAIT_MS,
    MAX_NETWORK_WAIT_S,
    MIN_VIEWPORT,
    NETWORK_PROBE_URL,
)
from apps.sniper.product_deliver.errors import (
    DeliverError,
    ShutdownRequested,
)
from apps.sniper.product_deliver.legacy_store import (
    DeliverStore,
)
from apps.sniper.product_deliver.selectors import (
    TABLE_ROW_DATA,
)



def _confirm_orders_page(page, timeout_ms: int = LOGIN_NETWORK_WAIT_MS) -> None:
    """Extra margin after login_with_session_cache() returns, mirroring
    run_automation.py's own belt-and-suspenders check. A session can report
    success while the orders page is still finishing its first render --
    especially on a cold/first-time session for a client, which has no
    session_state.json yet and must take the slower fresh-login path -- so
    this gives it a second, longer window before anything downstream (row
    search, filters, menus) touches the DOM.

    If the button still isn't there after that full window, one reload +
    retry before giving up: a stuck/half-rendered page is usually fixed by
    a fresh navigation, not by waiting even longer on the same load. If it
    STILL fails after the reload, the PWTimeoutError propagates up exactly
    as before -- this never silently swallows a genuine failure.
    """
    try:
        page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=timeout_ms)
        return
    except PWTimeoutError:
        pass
    page.reload(wait_until="commit")
    page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=timeout_ms)



def _ensure_viewport(page) -> None:
    """Plan doc §1: the CRM UI collapses below `md`, so use a viewport of at
    least 1440x900. Only acts when the context has a fixed viewport (a
    `no_viewport` headful window reports None and is left alone). Best
    effort: never raises."""
    try:
        size = page.viewport_size
        if size and (size["width"] < MIN_VIEWPORT[0]
                     or size["height"] < MIN_VIEWPORT[1]):
            page.set_viewport_size({
                "width": max(size["width"], MIN_VIEWPORT[0]),
                "height": max(size["height"], MIN_VIEWPORT[1]),
            })
    except Exception:
        pass



# ============================ waits =========================================


def _probe_network() -> bool:
    """doc §20: cheap connectivity probe."""
    try:
        import urllib.request
        with urllib.request.urlopen(NETWORK_PROBE_URL, timeout=4):
            return True
    except Exception:
        return False



def wait_for_network(store: DeliverStore, max_wait_s: int = MAX_NETWORK_WAIT_S) -> None:
    """doc §20: pause + poll until connectivity returns, without consuming
    the row's UI retries."""
    waited = 0
    interval = 5
    while waited < max_wait_s:
        if _probe_network():
            return
        time.sleep(interval)
        waited += interval
        if store.shutdown:
            raise ShutdownRequested("shutdown during network wait")
    raise DeliverError("network", f"no connectivity for {max_wait_s}s",
                       retryable=True)



def wait_for_app_idle(page, timeout_ms: int = 9000) -> None:
    """doc §8: wait for application-specific conditions, not networkidle:
    either a DATA row exists (TABLE_ROW_DATA -- the empty-state row is not a
    row, or this returned instantly on an empty table and callers then read
    a stale view), an explicit empty state exists, or a search response has
    landed."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            if page.locator(TABLE_ROW_DATA).count() > 0:
                return
            body_text = page.locator("body").inner_text(timeout=1000)
            if re.search(r"no (orders|results|data)", body_text, re.I):
                return
        except PWError:
            pass
        except Exception:
            return
        page.wait_for_timeout(250)

    # timeout: caller decides (search retry)


def _center(locator, pause_ms: int = 220) -> None:
    """Scrolls the given element to the visual CENTER of the viewport
    (instant jump, then waits for it to settle) before the automation acts on
    it -- so a human watching always sees exactly what's about to be clicked,
    instead of the page sitting still while things change off-screen.

    Best-effort only: if the element can't be measured/scrolled for any
    reason (already detached, mid-animation, etc.), this silently no-ops
    rather than raising -- visual centering is a UX nicety for the human
    watching, never a reason to break the automation flow. The caller's
    own click/hover still runs its normal Playwright auto-scroll-and-wait
    logic immediately after, so correctness never depends on this
    succeeding.
    """
    try:
        # Elements inside dialogs / menus / listboxes / popovers live on
        # position:fixed layers. scrollIntoView() on them cannot move them,
        # it only scrolls the page BEHIND (and makes anchored popovers
        # reposition mid-click), so leave them alone -- Playwright's own
        # click already scrolls when it really has to.
        moved = locator.evaluate(
            """el => new Promise(resolve => {
                if (el.closest('[role="dialog"], [role="alertdialog"], '
                    + '[role="menu"], [role="listbox"], '
                    + '[data-radix-popper-content-wrapper], [cmdk-root]')) {
                    resolve(false);
                    return;
                }
                // 'instant' (not 'smooth'): a smooth scroll runs for several
                // hundred ms on the compositor, so the very next click/hover
                // races it (menus and popovers are anchored to elements that
                // are still sliding) and a page-level CSS `scroll-behavior:
                // smooth` would even turn 'auto' into an animation. The
                // human watching still sees the page jump to the element.
                el.scrollIntoView({block: 'center', inline: 'center',
                                   behavior: 'instant'});
                // Then wait until the element has really stopped moving.
                const box = () => {
                    const r = el.getBoundingClientRect();
                    return r.top + ',' + r.left;
                };
                let last = box(), stable = 0;
                const t0 = performance.now();
                // rAF is paused in a hidden/occluded window: never hang.
                setTimeout(() => resolve(true), 1700);
                const tick = () => {
                    const cur = box();
                    stable = (cur === last) ? stable + 1 : 0;
                    last = cur;
                    if (stable >= 6 || performance.now() - t0 > 1500) {
                        resolve(true);
                    } else {
                        requestAnimationFrame(tick);
                    }
                };
                requestAnimationFrame(tick);
            })""",
            timeout=4000,
        )
        if moved and pause_ms:
            locator.page.wait_for_timeout(pause_ms)
    except Exception:
        pass



def read_tab_count(page, tab) -> int:
    """Badge count of a status tab (e.g. 'Delivered 42'). `tab` may be a
    CSS selector string or an already-resolved Locator."""
    try:
        loc = page.locator(tab).first if isinstance(tab, str) else tab
        text = loc.inner_text(timeout=4000)
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else 0
    except Exception:
        return 0
