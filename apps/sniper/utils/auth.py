"""
auth.py

Light, fully-automated login helper for sniper.ng.

MIGRATION NOTE (Sep 2026): sniper.ng's server-side auth is stateless --
no durable server session the old SessionManager's storage_state
save/restore was originally built around. That said, "stateless" server-
side doesn't mean nothing persists: sniper.ng still hands the browser an
auth token (cookie/localStorage) that stays valid for a while, and
Playwright's storage_state can capture and replay exactly that. So this
module offers both:

  - login(page, ...) -- always logs in fresh via .env credentials. Use
    this if you want zero session caching, every run.
  - login_with_session_cache(browser, ...) -- tries a saved session
    first (skips the login form entirely if it's still valid), falls
    back to login() automatically otherwise. Use this for faster runs;
    it's exactly as reliable as login() since it always falls through to
    the same code path on any failure.

Either way, nothing here writes credentials anywhere -- python-dotenv
only READS the .env file into the process environment; this module never
logs, prints, or persists the email/password themselves. The session
cache file (default session_state.json) holds a browser auth token, not
your password -- still worth keeping out of version control alongside
.env.

.env file (same directory you run the script from, or anywhere on
python-dotenv's search path) needs:

    SNIPER_EMAIL=you@example.com
    SNIPER_PASSWORD=your-password-here

Add a line like `.env` to .gitignore if this project is version
controlled, so credentials never get committed.

Usage (see run_automation.py for the real call site):

    from apps.sniper.utils.auth import login_with_session_cache
    context, page = login_with_session_cache(browser, LOGIN_URL, TARGET_URL, LOGGED_IN_SELECTOR)
"""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv, dotenv_values
from playwright.sync_api import TimeoutError as PWTimeoutError

from apps.sniper.nav_utils import safe_goto

load_dotenv()  # picks up a local .env if present; a harmless no-op otherwise

EMAIL_ENV_VAR = "SNIPER_EMAIL"
PASSWORD_ENV_VAR = "SNIPER_PASSWORD"

DEFAULT_SESSION_STORAGE_PATH = "session_state.json"
DEFAULT_SESSION_MAX_AGE_SECONDS = 12 * 3600

# ---- Selectors, as confirmed against the live login page ----
EMAIL_INPUT_SELECTOR = 'label:has-text("Email") + input'
EMAIL_INPUT_FALLBACK = 'input[type="email"]'
PASSWORD_INPUT_SELECTOR = 'label:has-text("Password") + div input'
PASSWORD_INPUT_FALLBACK = 'input[type="password"]'
LOG_IN_BUTTON_NAME = "Log in"

# Not used by the automated flow below (nothing to click them for), kept
# here only as reference/for future manual-recovery tooling.
SHOW_PASSWORD_BUTTON_NAME = "Show password"
FORGOT_PASSWORD_BUTTON_NAME = "Forgot password?"
SIGN_UP_LINK_NAME = "Sign up"


class MissingCredentialsError(RuntimeError):
    """Raised when SNIPER_EMAIL/SNIPER_PASSWORD aren't set -- a real
    setup problem, never silently worked around."""


def _get_credentials(client_id: str | None = None,
                     credential_reference: str | None = None,
                     env_file: str | None = None,
                     cache_file: str | None = None) -> tuple[str, str]:
    """Resolve credentials without printing them, using JSON cache fallback.

    A client may reference an environment-variable prefix through
    ``credential_reference`` (for example ``env:ACME``), which resolves
    ``ACME_EMAIL`` and ``ACME_PASSWORD``. Without a reference, client IDs use
    ``SNIPER_<CLIENT_ID>_EMAIL/PASSWORD`` before the legacy global variables.
    """
    file_values = dotenv_values(env_file) if env_file and Path(env_file).exists() else {}

    def read_pair(prefix: str) -> tuple[str | None, str | None, bool]:
        email_key = f"{prefix}_EMAIL"
        password_key = f"{prefix}_PASSWORD"
        file_email = file_values.get(email_key)
        file_password = file_values.get(password_key)
        env_email = os.environ.get(email_key)
        env_password = os.environ.get(password_key)
        defined = any(value is not None for value in (
            file_email, file_password, env_email, env_password
        ))
        email = str(file_email if file_email is not None else env_email or "").strip()
        password = str(
            file_password if file_password is not None else env_password or ""
        ).strip()
        return email, password, defined

    def real(email: str | None, password: str | None) -> bool:
        return bool(email and password) and email != "CHANGE_ME" and password != "CHANGE_ME"

    if credential_reference and credential_reference.startswith("env:"):
        prefix = credential_reference[4:].strip()
        email, password, _ = read_pair(prefix)
        if real(email, password):
            return email, password
        if cache_file:
            try:
                cached = json.loads(Path(cache_file).read_text(encoding="utf-8-sig"))
                cached_email = str(cached.get("email") or "").strip()
                cached_password = str(cached.get("password") or "").strip()
                if cached.get("credential_reference") == credential_reference and real(cached_email, cached_password):
                    return cached_email, cached_password
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        raise MissingCredentialsError(
            f"Client {client_id!r} has no usable credentials in .env or its validated cache."
        )

    email, password, defined = read_pair("SNIPER")
    if defined and real(email, password):
        return email, password
    raise MissingCredentialsError(
        f"No usable credentials found for client {client_id!r}. "
        "Fill the client-specific pair in apps/.env."
    )


def _click_resilient(scope, candidates: list[Callable], label: str,
                      timeout_ms: int = 8000) -> None:
    """Same pattern as add_products.click_resilient, kept as a small local
    copy here so this module stays a standalone, dependency-light login
    helper rather than importing from the order-filling module."""
    last_err = None
    for make_locator in candidates:
        try:
            loc = make_locator(scope)
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            loc.first.click(no_wait_after=True)
            return
        except Exception as e:
            last_err = e
            continue
    raise PWTimeoutError(
        f"_click_resilient({label}): none of {len(candidates)} strategy(ies) "
        f"worked within {timeout_ms}ms. Last error: {last_err}"
    )


def _fill_resilient(page, primary: str, fallback: str, value: str, label: str,
                     timeout_ms: int = 8000) -> None:
    for selector in (primary, fallback):
        try:
            loc = page.locator(selector)
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            loc.first.click(no_wait_after=True)
            loc.first.fill(value)
            return
        except Exception:
            continue
    raise PWTimeoutError(
        f"could not find a visible input for {label!r} using either "
        f"{primary!r} or its fallback {fallback!r}"
    )


def login(page, login_url: str, target_url: str, logged_in_selector: str,
          network_wait_timeout_ms: int = 20000,
          client_id: str | None = None,
          credential_reference: str | None = None,
          env_file: str | None = None,
          cache_file: str | None = None) -> None:
    """Fully automated login:
      1. Navigate to `login_url`.
      2. Fill email + password from the environment (.env).
      3. Click 'Log in'.
      4. Wait for the URL to actually leave the login page.
      5. Navigate to `target_url` explicitly.
      6. Wait for `logged_in_selector` there to confirm success.

    Steps 4-6 are deliberately separate: sniper.ng doesn't always land on
    `target_url` right after login (confirmed: it can land on
    /deliveries instead of /orders), so this never assumes
    `logged_in_selector` will already be present wherever login happens
    to redirect to -- it leaves the login page first, THEN explicitly
    goes to `target_url`, THEN checks.

    Raises MissingCredentialsError if the .env values aren't set, or
    Playwright's TimeoutError if any step doesn't complete in time --
    neither is swallowed, since a silent failure here would mean every
    order downstream fails for a confusing reason."""
    email, password = _get_credentials(client_id, credential_reference, env_file, cache_file)

    safe_goto(page, login_url, wait_until="commit")

    _fill_resilient(page, EMAIL_INPUT_SELECTOR, EMAIL_INPUT_FALLBACK,
                     email, "email", network_wait_timeout_ms)
    _fill_resilient(page, PASSWORD_INPUT_SELECTOR, PASSWORD_INPUT_FALLBACK,
                     password, "password", network_wait_timeout_ms)

    _click_resilient(page, [
        lambda s: s.get_by_role("button", name=LOG_IN_BUTTON_NAME, exact=True),
        lambda s: s.locator(f'button:has-text("{LOG_IN_BUTTON_NAME}")'),
    ], "Log in", network_wait_timeout_ms)

    try:
        page.wait_for_url(lambda url: "login" not in url, timeout=network_wait_timeout_ms)
    except PWTimeoutError:
        raise PWTimeoutError(
            f"login: still on a 'login' URL {network_wait_timeout_ms}ms after clicking "
            f"'{LOG_IN_BUTTON_NAME}' -- login likely failed (wrong credentials, a "
            f"validation error on the form, etc.). Check the browser window."
        )

    if target_url:
        safe_goto(page, target_url, wait_until="commit")
    page.wait_for_selector(logged_in_selector, timeout=network_wait_timeout_ms)


def login_with_session_cache(browser, login_url: str, target_url: str,
                              logged_in_selector: str,
                              storage_path: str = DEFAULT_SESSION_STORAGE_PATH,
                              max_age_seconds: int = DEFAULT_SESSION_MAX_AGE_SECONDS,
                              network_wait_timeout_ms: int = 20000,
                              client_id: str | None = None,
                              credential_reference: str | None = None,
                              env_file: str | None = None,
                              cache_file: str | None = None):
    """Tries to reuse a saved browser session first; falls back to a
    fresh credentials login() if there's no saved session, it's older
    than `max_age_seconds`, or it turns out invalid/expired. Returns a
    ready-to-use (context, page).

    Why this works even though sniper.ng's server-side auth is stateless:
    'stateless' almost always just means the SERVER keeps no session --
    the actual auth token still lives client-side, in a cookie or
    localStorage, for as long as it's valid. Playwright's
    context.storage_state() captures exactly that, so replaying it into
    a new context is a legitimate way to skip a fresh login, not a
    workaround for something that doesn't apply here. If sniper.ng turns
    out to genuinely persist nothing retrievable client-side, every
    attempt below just falls through to a fresh login -- harmless, only
    slightly slower than calling login() directly.

    Nothing about this is required: pass through to plain login() at any
    time if you'd rather always log in fresh."""
    storage_file = Path(storage_path)
    metadata_path = storage_file.with_name("metadata.json")

    if storage_file.exists():
        age_s = time.time() - storage_file.stat().st_mtime
        if age_s < max_age_seconds:
            context = browser.new_context(storage_state=str(storage_file))
            page = context.new_page()
            try:
                safe_goto(page, target_url, wait_until="commit")
                page.wait_for_selector(logged_in_selector, timeout=8000)
                _write_auth_metadata(metadata_path, client_id, "reused")
                return context, page  # reused session -- no login needed
            except Exception:
                try:
                    context.close()
                except Exception:
                    pass
                # falls through to a fresh login below

    context = browser.new_context()
    page = context.new_page()
    login(page, login_url, target_url, logged_in_selector, network_wait_timeout_ms,
          client_id=client_id, credential_reference=credential_reference,
          env_file=env_file, cache_file=cache_file)

    try:
        context.storage_state(path=str(storage_file))
    except Exception:
        pass  # saving the cache is a nice-to-have, never a reason to fail the run
    _write_auth_metadata(metadata_path, client_id, "fresh_login")

    return context, page


def _write_auth_metadata(path: Path, client_id: str | None, mode: str) -> None:
    """Record safe session bookkeeping; never record email or tokens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": client_id,
        "mode": mode,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)