# ==============================================================
# OMS DIAGNOSTICS
# PATH: windwhirl/apps/oms_diagnostics.py
# RUN:  python oms_diagnostics.py
#
# PURPOSE:
#   Three independent checks:
#   1. Session persistence — why cookies=False every run
#   2. DOM container scan — what containers actually exist
#   3. Page state check — what WhatsApp Web is showing
#
# Run this INSTEAD of oms_runner.py to get raw diagnostic data.
# ==============================================================

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

async def main():
    from playwright.async_api import async_playwright

    sess_dir = Path(".sessions/oms_session").resolve()

    print("\n" + "=" * 60)
    print("  OMS DIAGNOSTICS")
    print("=" * 60)

    # ── CHECK 1: Session directory contents ───────────────────────
    print("\n[1] SESSION DIRECTORY")
    print(f"    Path: {sess_dir}")
    print(f"    Exists: {sess_dir.exists()}")

    if sess_dir.exists():
        total_bytes = 0
        file_count  = 0
        cookie_files = []
        for f in sess_dir.rglob("*"):
            if f.is_file():
                file_count  += 1
                total_bytes += f.stat().st_size
                if "cookie" in f.name.lower():
                    cookie_files.append(str(f.relative_to(sess_dir)))

        print(f"    Total size: {total_bytes / (1024*1024):.2f} MB")
        print(f"    File count: {file_count}")
        print(f"    Cookie files found: {cookie_files or 'NONE'}")

        # The specific file that WhatsApp session cookies live in
        cookies_db = sess_dir / "Default" / "Cookies"
        network_db = sess_dir / "Default" / "Network" / "Cookies"
        print(f"    Default/Cookies exists: {cookies_db.exists()}")
        print(f"    Default/Network/Cookies exists: {network_db.exists()}")
        if cookies_db.exists():
            print(f"    Default/Cookies size: {cookies_db.stat().st_size} bytes")
    else:
        print("    SESSION DIRECTORY DOES NOT EXIST")

    # ── CHECK 2: Launch browser and scan the DOM ──────────────────
    print("\n[2] BROWSER + DOM SCAN")
    print("    Launching browser with existing session...")

    pw  = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(sess_dir),
        headless=False,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="Africa/Lagos",
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    print("    Navigating to WhatsApp Web...")
    await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

    print("    Waiting 10 seconds for WhatsApp to load...")
    await asyncio.sleep(10)

    # Screenshot of current state
    ss_path = Path("screenshots")
    ss_path.mkdir(exist_ok=True)
    await page.screenshot(path=str(ss_path / "diagnostic_state.png"))
    print(f"    Screenshot: screenshots/diagnostic_state.png")

    # ── CHECK 3: What is currently visible on the page ────────────
    print("\n[3] PAGE STATE")
    page_url = page.url
    page_title = await page.title()
    print(f"    URL:   {page_url}")
    print(f"    Title: {page_title}")

    body_text = (await page.inner_text("body")).lower()[:200]
    print(f"    Body preview: {body_text!r}")

    # Check for known WhatsApp Web states
    chat_list = await page.query_selector('div[aria-label="Chat list"]')
    qr_code   = await page.query_selector('canvas[aria-label="Scan me!"]')
    search    = await page.query_selector('input[aria-label="Search or start a new chat"]')
    print(f"    Chat list visible:   {chat_list is not None}")
    print(f"    QR code visible:     {qr_code is not None}")
    print(f"    Search bar visible:  {search is not None}")

    if qr_code:
        print("\n    ⚠️  QR CODE IS SHOWING — session not saved.")
        print("    This means cookies are not being persisted to disk.")

    if chat_list:
        print("\n    ✅ Logged in. Looking for group...")

        # Try to open the target group
        if search:
            await search.click()
            await asyncio.sleep(0.5)
            await page.keyboard.type("General Order Group", delay=80)
            await asyncio.sleep(2)

        # ── CHECK 4: DOM container scan ───────────────────────────
        print("\n[4] DOM CONTAINER SCAN")
        print("    Scanning for WhatsApp message containers...")

        containers = await page.evaluate("""
            () => {
                const selectors = [
                    '#main div[role="application"]',
                    'div[data-testid="conversation-panel-messages"]',
                    'div[data-testid="msg-container"]',
                    '#main .copyable-area',
                    '#main div[tabindex="-1"]',
                    'div[data-tab="8"]',
                    '#main',
                    'div[data-testid="conversation-panel"]',
                    'div[role="application"]',
                    '.app-wrapper-web',
                    '#app',
                ];
                const results = {};
                selectors.forEach(sel => {
                    const el = document.querySelector(sel);
                    results[sel] = el ? {
                        found: true,
                        tag: el.tagName,
                        children: el.children.length,
                        visible: el.offsetWidth > 0,
                    } : { found: false };
                });
                return results;
            }
        """)

        for sel, info in containers.items():
            if info["found"]:
                print(
                    f"    ✅ {sel}\n"
                    f"       tag={info['tag']}, "
                    f"children={info['children']}, "
                    f"visible={info['visible']}"
                )
            else:
                print(f"    ❌ {sel}")

        # ── CHECK 5: All aria-labels (to find the right one) ──────
        print("\n[5] ALL VISIBLE ARIA-LABELS ON PAGE")
        labels = await page.evaluate("""
            () => {
                const seen = new Set();
                const results = [];
                document.querySelectorAll('[aria-label]').forEach(el => {
                    const label = el.getAttribute('aria-label');
                    const rect  = el.getBoundingClientRect();
                    if (!seen.has(label) && rect.width > 0) {
                        seen.add(label);
                        results.push({
                            label: label,
                            tag:   el.tagName.toLowerCase(),
                            x:     Math.round(rect.x),
                            y:     Math.round(rect.y),
                        });
                    }
                });
                return results.slice(0, 30);
            }
        """)
        for item in labels:
            print(f"    <{item['tag']}> aria-label={item['label']!r} @ ({item['x']}, {item['y']})")

    print("\n[6] INSTRUCTIONS")
    print("    1. Look at screenshots/diagnostic_state.png")
    print("       What do you see? QR code? Chat list? Something else?")
    print()
    print("    2. If QR code showing every run:")
    print("       The session isn't saving. Possible causes on Windows:")
    print("       a) .sessions/oms_session/ is being cleared between runs")
    print("       b) Antivirus blocking Chromium writing to that folder")
    print("       c) The folder path has a space or special character")
    print("       d) Playwright version mismatch with Chromium")
    print()
    print("    3. If logged in but container not found:")
    print("       The group chat panel isn't rendering.")
    print("       Check the ✅ containers above — paste them back.")
    print()
    print("    Press Ctrl+C to close the browser.")

    try:
        await asyncio.sleep(300)  # Keep open for 5 minutes
    except KeyboardInterrupt:
        pass

    await ctx.close()
    await pw.stop()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())