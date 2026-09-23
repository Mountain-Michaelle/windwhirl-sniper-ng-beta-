"""
smoke_test.py - not part of the deliverable, just proof the recorder
actually filters noise correctly against a local mock of the form
before pointing it at the real SniperCRM site.

Uses bare inputs with no id/name/label (matching the real production
markup, per the original recording), plus a short pause after page
load before interacting -- long enough for the poller's baseline pass
to run at least once, same as it would on the live site.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright
from workflow_recorder import BusinessWorkflowRecorder

HTML_PATH = Path(__file__).with_name("test_page.html").resolve()

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(f"file://{HTML_PATH}")

    recorder = BusinessWorkflowRecorder(page, output_dir="test_out", session_name="smoke")
    recorder.start()

    page.wait_for_timeout(600)  # let the poller's first baseline pass complete

    customer_input = page.locator("input.form-control").nth(0)
    phone_input = page.locator("input.form-control").nth(1)

    # Simulate a user typing (with repeated keystrokes -> should debounce to ONE step)
    customer_input.type("Chinelo", delay=20)
    customer_input.blur()

    phone_input.type("2348141666361", delay=20)
    phone_input.blur()

    # Simulate repeated dropdown clicks before a real selection (should collapse)
    page.click("#selpro")
    page.click("#selpro")
    page.select_option("#selpro", label="Sadoer Collagen Serum")

    page.select_option("#pricevar", label="30ml & 100g NGN29500")
    page.select_option("#state", label="Anambra")
    page.select_option("select.form-control", label="Cash")

    # Simulate rapid double-click on Save (should dedupe to one step)
    page.click("#submit")
    page.click("#submit")

    page.wait_for_timeout(700)  # let debounce timers + mutation observer fire

    result = recorder.stop_and_save()
    browser.close()

print(open(result["text_path"]).read())
print("----")
print("Step count:", len(recorder.steps))