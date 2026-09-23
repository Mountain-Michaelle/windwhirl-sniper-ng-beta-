# run_recorder.py
"""
Entry point. Wires SessionManager + BusinessWorkflowRecorder together.

Two separate concepts, easy to conflate:
  - TARGET_URL (below) is about SESSION MANAGEMENT: where to land after
    a valid/restored login. Unrelated to capture.
  - EXCLUDE_URL_PATTERNS (important_fields.py) is about CAPTURE SCOPE:
    URLs to never record on (e.g. the login page itself). The recorder
    now captures everywhere else by default -- it's not restricted to
    a single target page/prefix anymore.

Run:
    pip install playwright
    playwright install chromium
    python run_recorder.py
"""

from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

from session_manager import SessionManager
from workflow_recorder import BusinessWorkflowRecorder
from important_fields import EXCLUDE_URL_PATTERNS, STRUCTURE_SNAPSHOT_THRESHOLD
from nav_utils import safe_goto

LOGIN_URL = "https://sniper.ng/login"
TARGET_URL = "https://sniper.ng/orders"   # confirm this is still correct for your login flow

LOGGED_IN_SELECTOR = "#selpro"
LOGIN_INDICATOR_SELECTOR = "#login-form"


def manual_login(page) -> None:
    safe_goto(page, LOGIN_URL, wait_until="commit")
    print("Please log in manually in the opened browser window.")
    print("Waiting for the app to reach a logged-in page...")
    page.wait_for_url(lambda url: "login" not in url, timeout=0)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


def main() -> None:
    session = SessionManager(
        storage_path="session_state.json",
        login_url=LOGIN_URL,
        target_url=TARGET_URL,
        logged_in_selector=LOGGED_IN_SELECTOR,
        login_indicator_selector=LOGIN_INDICATOR_SELECTOR,
        max_age_seconds=12 * 3600,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context, page = session.load_session(browser)
        if page is None:
            print("No valid session found -- starting normal login flow.")
            context = browser.new_context()
            page = context.new_page()
            manual_login(page)
            session.save_session(context, page)
            safe_goto(page, TARGET_URL, wait_until="commit")
        else:
            print("Restored saved session -- skipped login, opened straight on target page.")

        recorder = BusinessWorkflowRecorder(
            page,
            output_dir="recordings",
            session_name=f"session_{int(time.time())}",
            debug=("--debug" in sys.argv),
            exclude_url_patterns=EXCLUDE_URL_PATTERNS,
            structure_threshold=STRUCTURE_SNAPSHOT_THRESHOLD,
        )
        recorder.start()

        session_dropped = {"flag": False}

        def _watch_for_midsession_logout(frame) -> None:
            if frame != page.main_frame:
                return
            if "login" in frame.url.lower() and "login" not in TARGET_URL.lower():
                session_dropped["flag"] = True

        page.on("framenavigated", _watch_for_midsession_logout)

        print("Recording started. Work the browser normally -- any site, any flow.")
        print("Each captured field/action/structure change will print here as it happens.")
        print("Press Ctrl+C in this terminal when you're done to save the log.\n")

        elapsed = 0
        warned_empty = False
        try:
            while True:
                time.sleep(1)
                elapsed += 1

                if session_dropped["flag"]:
                    print(
                        "\n  Session expired mid-recording -- redirected to the login "
                        "page. Recording is paused (nothing on excluded URLs is ever "
                        "captured). Please log in again; you'll be returned automatically.\n",
                        flush=True,
                    )
                    while "login" in page.url.lower():
                        time.sleep(1)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    session.save_session(context, page)
                    safe_goto(page, TARGET_URL, wait_until="commit")
                    session_dropped["flag"] = False
                    elapsed = 0
                    warned_empty = False
                    print(f"  Logged in again -- back on {TARGET_URL}, recording resumed.\n", flush=True)
                    continue

                if elapsed == 20 and not recorder.steps and not warned_empty:
                    warned_empty = True
                    print(
                        "\n  ...20s in, 0 steps captured yet. Stop this (Ctrl+C) and "
                        "re-run with:\n      python run_recorder.py --debug\n"
                        "  to see every click/input/change reported live, and check "
                        "for a '[debug] installed' line -- if that never shows up, "
                        "the script isn't running in the page at all (wrong frame, "
                        "blocked injection). Still listening for now.\n"
                    )
        except KeyboardInterrupt:
            pass

        print("\nFinishing up (letting any in-flight events land)...", flush=True)
        try:
            for _ in range(5):
                page.evaluate("1")
                time.sleep(0.3)
        except Exception:
            pass

        paths = recorder.stop_and_save()
        print(f"\nSaved human-readable log: {paths['text_path']}", flush=True)
        print(f"Saved structured log:     {paths['json_path']}", flush=True)
        print(f"Saved structure snapshots: {paths['structure_path']}", flush=True)
        if "network_path" in paths:
            print(f"Saved network log:        {paths['network_path']}  (kept separate -- see NETWORK_CAPTURE_MODE in important_fields.py)", flush=True)
        if "script_path" in paths:
            print(f"Saved starter automation script: {paths['script_path']}", flush=True)

        if recorder.steps:
            print(f"\n{len(recorder.steps)} step(s) recorded:\n", flush=True)
            print(open(paths["text_path"], encoding="utf-8").read(), flush=True)
        else:
            print(
                "\nWARNING: 0 steps were recorded. Check the exclude patterns in "
                "important_fields.py aren't accidentally matching the page you "
                "worked on, and re-run with --debug to see the raw event stream.",
                flush=True,
            )

        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()