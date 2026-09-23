# ==============================================================
# SELECTOR FINDER — Run this to find the correct search bar
# ==============================================================
# PATH: apps/find_selectors.py  (temporary file, delete after use)
#
# RUN WITH:
#   python find_selectors.py
#
# WHAT IT DOES:
#   Opens WhatsApp Web in a visible browser using your saved session.
#   Waits 5 seconds for the page to fully load.
#   Then scans every interactive element and prints:
#     - Its tag name
#     - Its aria-label
#     - Its data-tab attribute
#     - Its role
#     - Its placeholder
#   You can see exactly what selector to use for the search bar.
#   Also takes a screenshot so you can see the state of the page.
#
# ALSO FIXES:
#   Resets all FAILED and FAILED_FINAL records to PENDING
#   so your customers are ready to send to again.
# ==============================================================

import asyncio
import sys
from pathlib import Path

# Path fix — same as main.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.config import AppConfig


async def find_selectors():
    from playwright.async_api import async_playwright

    cfg      = AppConfig()
    sess_dir = Path(".sessions") / "whatsapp_session"

    print("=" * 60)
    print("  WHATSAPP WEB SELECTOR FINDER")
    print("=" * 60)

    pw  = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(sess_dir),
        headless=False,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="Africa/Lagos",
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    print("Navigating to WhatsApp Web...")
    await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

    print("Waiting 8 seconds for full load...")
    await asyncio.sleep(50)

    # Take a screenshot of the current state
    Path("screenshots").mkdir(exist_ok=True)
    ss_path = "screenshots/selector_finder.png"
    await page.screenshot(path=ss_path)
    print(f"\nScreenshot saved: {ss_path}")
    print("(Check this to see what WhatsApp Web is showing)\n")

    # ── Scan for search-related elements ───────────────────────
    print("=" * 60)
    print("  SEARCH BAR CANDIDATES")
    print("=" * 60)

    # Run JavaScript to find all potentially clickable elements
    # with search-related attributes
    elements_data = await page.evaluate("""
        () => {
            const results = [];
            const all = document.querySelectorAll(
                'input, [contenteditable], [role="textbox"], ' +
                '[role="searchbox"], [data-tab], [aria-label*="search" i], ' +
                '[aria-label*="Search" i], [placeholder*="search" i], ' +
                '[title*="search" i], [title*="Search" i]'
            );

            all.forEach((el, idx) => {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    results.push({
                        index:       idx,
                        tag:         el.tagName.toLowerCase(),
                        id:          el.id || '',
                        class:       el.className.toString().substring(0, 60),
                        ariaLabel:   el.getAttribute('aria-label') || '',
                        dataTab:     el.getAttribute('data-tab') || '',
                        role:        el.getAttribute('role') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        title:       el.getAttribute('title') || '',
                        contentEdit: el.getAttribute('contenteditable') || '',
                        dataTestId:  el.getAttribute('data-testid') || '',
                        visible:     rect.width > 0 && rect.height > 0,
                        x:           Math.round(rect.x),
                        y:           Math.round(rect.y),
                        width:       Math.round(rect.width),
                        height:      Math.round(rect.height),
                    });
                }
            });
            return results;
        }
    """)

    print(f"Found {len(elements_data)} search-related elements:\n")

    for el in elements_data:
        print(f"  Element #{el['index']}")
        print(f"    Tag:          <{el['tag']}>")
        if el['ariaLabel']:
            print(f"    aria-label:   {el['ariaLabel']}")
        if el['dataTab']:
            print(f"    data-tab:     {el['dataTab']}")
        if el['role']:
            print(f"    role:         {el['role']}")
        if el['placeholder']:
            print(f"    placeholder:  {el['placeholder']}")
        if el['title']:
            print(f"    title:        {el['title']}")
        if el['contentEdit']:
            print(f"    contenteditable: {el['contentEdit']}")
        if el['dataTestId']:
            print(f"    data-testid:  {el['dataTestId']}")
        print(f"    Position:     x={el['x']}, y={el['y']}, "
              f"w={el['width']}, h={el['height']}")
        print()

    # ── Also scan for ALL aria-labels on the page ───────────────
    print("=" * 60)
    print("  ALL ARIA-LABELS ON PAGE")
    print("=" * 60)

    all_labels = await page.evaluate("""
        () => {
            const seen = new Set();
            const results = [];
            document.querySelectorAll('[aria-label]').forEach(el => {
                const label = el.getAttribute('aria-label');
                const rect  = el.getBoundingClientRect();
                if (!seen.has(label) && rect.width > 0) {
                    seen.add(label);
                    results.push({
                        label:     label,
                        tag:       el.tagName.toLowerCase(),
                        dataTab:   el.getAttribute('data-tab') || '',
                        role:      el.getAttribute('role') || '',
                        x:         Math.round(rect.x),
                        y:         Math.round(rect.y),
                    });
                }
            });
            return results.slice(0, 40);  // First 40 unique labels
        }
    """)

    print(f"Found {len(all_labels)} unique aria-labels:\n")
    for item in all_labels:
        extra = ""
        if item['dataTab']:
            extra += f" [data-tab={item['dataTab']}]"
        if item['role']:
            extra += f" [role={item['role']}]"
        print(f"  <{item['tag']}> aria-label=\"{item['label']}\"{extra}"
              f"  @ ({item['x']}, {item['y']})")

    # ── Try clicking likely search candidates ───────────────────
    print()
    print("=" * 60)
    print("  TESTING CLICK ON LIKELY SEARCH ELEMENTS")
    print("=" * 60)

    test_selectors = [
        'div[aria-label="Search input textbox"]',
        'div[aria-label="Search or start new chat"]',
        'div[aria-label="Search"]',
        'div[role="searchbox"]',
        'div[data-tab="3"]',
        'div[contenteditable="true"][data-tab="3"]',
        'div[title="Search or start new chat"]',
        'input[type="text"]',
        '[data-testid="chat-list-search"]',
        'div[aria-label*="Search" i]',
        'div[aria-label*="search" i]',
    ]

    for sel in test_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                visible = await el.is_visible()
                box     = await el.bounding_box()
                print(f"  ✅ FOUND: {sel}")
                print(f"     visible={visible}, box={box}")
            else:
                print(f"  ❌ Not found: {sel}")
        except Exception as e:
            print(f"  ❌ Error for {sel}: {e}")

    print()
    print("=" * 60)
    print("  COPY THE ✅ SELECTOR FROM ABOVE")
    print("  That is your correct search bar selector")
    print("=" * 60)

    await ctx.close()
    await pw.stop()


async def reset_all_failed():
    """Reset FAILED and FAILED_FINAL records to PENDING."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from apps.config import AppConfig
    from apps.core.db.database import Database, SendLog, SendStatus

    cfg = AppConfig()
    db  = Database(cfg.database_url)
    db.init()

    with db._session() as session:
        rows = session.query(SendLog).filter(
            SendLog.status.in_(["FAILED", "FAILED_FINAL"])
        ).all()

        for row in rows:
            row.status        = SendStatus.PENDING
            row.attempt_count = 0
            row.error_message = None

        session.commit()
        print(f"\n✅ Reset {len(rows)} records to PENDING")
        print("   (Both FAILED and FAILED_FINAL)\n")


if __name__ == "__main__":
    print("\nStep 1: Resetting failed DB records...")
    asyncio.run(reset_all_failed())

    print("\nStep 2: Finding correct selectors...")
    asyncio.run(find_selectors())