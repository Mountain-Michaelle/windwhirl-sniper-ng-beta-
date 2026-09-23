"""
session_manager.py

Small, isolated helper that saves/restores a Playwright `storage_state`
so the recorder can skip the login page on every run. It knows nothing
about the workflow recorder, and the recorder knows nothing about how
sessions are stored -- they're wired together in run_recorder.py only.

Usage:

    from playwright.sync_api import sync_playwright
    from session_manager import SessionManager

    session = SessionManager(
        storage_path="session_state.json",
        login_url="https://app.snipercrm.io/login",
        target_url="https://app.snipercrm.io/add_multi_order",
        logged_in_selector="#selpro",          # something only visible when logged in
        login_indicator_selector="#login-form", # something only visible on the login page
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context, page = session.load_session(browser)
        if page is None:
            # no valid session -- caller does the normal login flow, then:
            context = browser.new_context()
            page = context.new_page()
            page.goto(session.login_url)
            # ... perform login ...
            session.save_session(context)
            page.goto(session.target_url)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from nav_utils import safe_goto


@dataclass
class SessionManager:
    storage_path: str
    login_url: str
    target_url: str
    logged_in_selector: str
    login_indicator_selector: Optional[str] = None
    max_age_seconds: Optional[int] = None  # e.g. 6 * 3600 to force re-login every 6h
    validation_timeout_ms: int = 20000  # generous: real admin pages with many
                                         # vendor scripts/CSS can be slow to settle
    verbose: bool = True

    def __post_init__(self):
        # Tracks whether the last failed validity check was a DEFINITIVE
        # rejection (the server itself bounced us to login, or the file
        # was deliberately expired) vs. an INCONCLUSIVE one (a timeout or
        # transient error). Only definitive rejections should delete the
        # saved session -- deleting it on every ambiguous failure is what
        # was previously turning one slow page load into a permanent
        # forced-relogin, since the file never got a chance to be reused
        # once removed.
        self._last_rejection_definitive = True

    # ---------- public API ----------

    def session_is_valid(self, browser) -> bool:
        """Best-effort check: does the saved storage_state file exist,
        isn't stale, and actually gets us past the login page when used?
        Always explains WHY it rejected a session (when verbose=True,
        the default) -- silently falling back to a fresh login with no
        explanation is exactly what makes this kind of bug impossible to
        diagnose."""
        self._last_rejection_definitive = True

        if not self._file_exists():
            self._log("no saved session file yet -- first run, this is expected.")
            return False
        if self._is_stale():
            self._log("saved session is older than max_age_seconds -- treating as expired.")
            return False

        context = browser.new_context(storage_state=self.storage_path)
        self._restore_session_storage(context)
        page = context.new_page()
        try:
            safe_goto(page, self.target_url, wait_until="commit")

            # URL-based check first: this doesn't depend on any guessed
            # selector being correct, and is the strongest possible signal
            # that the site itself bounced us back to login.
            page.wait_for_load_state("domcontentloaded", timeout=self.validation_timeout_ms)
            if "login" in page.url.lower() and "login" not in self.target_url.lower():
                self._log(f"redirected to {page.url!r} instead of staying on the "
                           "target page -- the saved session was rejected by the server.")
                return False

            if self.login_indicator_selector:
                login_visible = page.locator(self.login_indicator_selector).count() > 0
                if login_visible:
                    self._log(f"login_indicator_selector ({self.login_indicator_selector!r}) "
                               "is present on the page -- looks like the login form.")
                    return False

            if self.logged_in_selector:
                try:
                    page.locator(self.logged_in_selector).first.wait_for(
                        state="attached", timeout=self.validation_timeout_ms
                    )
                except Exception as e:
                    # Inconclusive: the page may just be slow, or the
                    # selector may be off. Do NOT delete the session over
                    # this -- only a definitive rejection above should.
                    self._last_rejection_definitive = False
                    self._log(
                        f"logged_in_selector ({self.logged_in_selector!r}) never "
                        f"appeared within {self.validation_timeout_ms}ms on {page.url!r}. "
                        "Keeping the saved session file (not deleting it) since this "
                        "could just be a slow load rather than an actually-expired "
                        "session -- if the page really does load slowly, raise "
                        f"validation_timeout_ms. Underlying error: {e}"
                    )
                    return False

            self._log("saved session is valid -- reusing it, login skipped.")
            return True
        except Exception as e:
            self._last_rejection_definitive = False
            self._log(f"inconclusive error while validating session (keeping the "
                       f"saved file, not deleting it): {e}")
            return False
        finally:
            context.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[session] {message}", flush=True)

    def load_session(self, browser) -> Tuple[Optional[object], Optional[object]]:
        """If the saved session is valid, return (context, page) already
        navigated to target_url with the login page skipped entirely.
        Returns (None, None) if there's no usable session -- caller
        should fall back to the normal login flow and then call
        save_session()."""
        if not self.session_is_valid(browser):
            if self._last_rejection_definitive:
                self.clear_session()
            return None, None

        context = browser.new_context(storage_state=self.storage_path)
        self._restore_session_storage(context)
        page = context.new_page()
        safe_goto(page, self.target_url, wait_until="commit")
        return context, page

    def save_session(self, context, page: Optional[object] = None) -> None:
        """Persist the current context's storage_state (cookies + localStorage)
        to disk, to be reused on the next run. If `page` is given, also
        captures sessionStorage -- Playwright's storage_state deliberately
        never includes sessionStorage (it's tab-scoped by spec), but some
        SPAs keep auth tokens there instead of cookies/localStorage. Pass
        the page you just logged in on so that's covered too."""
        os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)) or ".", exist_ok=True)
        context.storage_state(path=self.storage_path)
        self._touch_metadata()

        if page is not None:
            try:
                data = page.evaluate("() => JSON.stringify(window.sessionStorage)")
                with open(self._session_storage_path(), "w") as f:
                    f.write(data)
            except Exception as e:
                self._log(f"couldn't capture sessionStorage (non-fatal): {e}")

    def _restore_session_storage(self, context) -> None:
        """Re-inject any captured sessionStorage into every page this
        context opens, before the target site's own scripts run."""
        path = self._session_storage_path()
        if not os.path.exists(path):
            return
        with open(path) as f:
            data = f.read()
        # `data` is already a JSON object literal (from JSON.stringify),
        # safe to embed directly as a JS expression.
        script = (
            "try { const __ss = " + data + "; "
            "for (const k in __ss) window.sessionStorage.setItem(k, __ss[k]); "
            "} catch (e) {}"
        )
        context.add_init_script(script=script)

    def _session_storage_path(self) -> str:
        return self.storage_path + ".sessionstorage.json"

    def clear_session(self) -> None:
        """Delete the stored session (expired / invalid)."""
        for path in (self.storage_path, self._metadata_path(), self._session_storage_path()):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ---------- internals ----------

    def _file_exists(self) -> bool:
        return os.path.exists(self.storage_path) and os.path.getsize(self.storage_path) > 0

    def _metadata_path(self) -> str:
        return self.storage_path + ".meta.json"

    def _touch_metadata(self) -> None:
        with open(self._metadata_path(), "w") as f:
            json.dump({"saved_at": time.time()}, f)

    def _is_stale(self) -> bool:
        if self.max_age_seconds is None:
            return False
        meta_path = self._metadata_path()
        if not os.path.exists(meta_path):
            return False  # unknown age -- let the live validation check decide
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            return (time.time() - meta.get("saved_at", 0)) > self.max_age_seconds
        except (OSError, json.JSONDecodeError):
            return False