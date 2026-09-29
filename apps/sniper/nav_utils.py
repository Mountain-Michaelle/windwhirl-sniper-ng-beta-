"""
nav_utils.py

`page.goto(url, wait_until="domcontentloaded")` fails with
`net::ERR_ABORTED` whenever the app's own client-side redirect fires
while our navigation is still resolving -- the browser cancels our
navigation in favor of the redirect it just triggered. This is common
right after a login flow (the app often bounces you from the login URL
to a dashboard, or does an internal SPA-route redirect on first load of
a protected page). It is NOT a real network failure and should not be
treated as one.

safe_goto() fixes this two ways:
  1. wait_until="commit" -- resolves as soon as a navigation is
     committed, instead of waiting for DOMContentLoaded on a document
     that might get replaced out from under it a moment later.
  2. Retries on ERR_ABORTED and on plain timeouts (page just loading
     slowly -- vendor scripts, slow admin panel, etc.), since only a
     genuine DNS/connection failure (a different error entirely --
     ERR_NAME_NOT_RESOLVED, ERR_CONNECTION_REFUSED, and so on) should
     be treated as fatal and raised immediately.
"""

from __future__ import annotations

import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.sniper.product_upload import wait_profile


def safe_goto(page, url: str, retries: int = 3, wait_until: str = "commit",
              settle_ms: int = 300, dom_load_timeout_ms: int = 15000,
              tolerate_aborted_redirect: bool = True) -> None:   # NEW param, default matches intent
    # PERF (Phase 0): measure the two time costs here separately -- the
    # settle sleep (only ever non-zero on the recorder/session-manager
    # paths; the upload path passes settle_ms=0) and the genuine
    # navigation-failure backoff. That separation is what lets Phase 0
    # prove the failure backoff is NOT on the normal-path critical path
    # before anyone is tempted to "optimise" it away.
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until=wait_until)
            page.wait_for_load_state("domcontentloaded", timeout=dom_load_timeout_ms)
            if settle_ms > 0:
                t_settle = time.perf_counter()
                time.sleep(settle_ms / 1000)
                wait_profile.add("nav_settle_sleep",
                                 (time.perf_counter() - t_settle) * 1000.0)
            return
        except PlaywrightError as e:
            last_error = e
            is_aborted = "ERR_ABORTED" in str(e)
            is_slow_load = isinstance(e, PlaywrightTimeoutError)
            if (is_aborted or is_slow_load) and attempt < retries:
                t_backoff = time.perf_counter()
                time.sleep(0.5 * attempt)
                wait_profile.add("nav_failure_backoff",
                                 (time.perf_counter() - t_backoff) * 1000.0)
                continue

            # NEW: retries exhausted. If this was ERR_ABORTED, the site's
            # own redirect is what cancelled us -- not a real failure. If
            # the browser actually landed somewhere real, don't crash.
            if is_aborted and tolerate_aborted_redirect:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=dom_load_timeout_ms)
                except Exception:
                    pass
                if page.url and page.url != "about:blank":
                    return   # redirect completed -- treat as success
            raise
    if last_error:
        raise last_error