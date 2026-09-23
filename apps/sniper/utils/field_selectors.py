"""
field_selectors.py

Static mapping of confirmed selectors for the sniper.ng "Add Order" wizard
modal. This is the "recipe" that add_products.py follows -- no selector
strings live anywhere else in the codebase so the site can be
re-verified/updated in exactly one place.

------------------------------------------------------------------------
MIGRATION NOTE (Sep 2026): the old site (app.snipercrm.io) that this file
used to target is down; the business has moved to sniper.ng. The old UI
was a single-page form of native <select>/<input> elements addressable by
id/name (#selpro, #pricevar, [name="cname"], #submit, etc.). The new UI is
a 4-step modal wizard (Products -> Charges -> Customer -> Summary) built
on shadcn/ui-style components:
  - "combobox" fields are NOT native <select>s. They're a trigger
    <button role="combobox"> that, on click, opens a <div role="listbox">
    of <div role="option" data-value="...">  children. Selecting one is a
    3-step interaction (click trigger -> wait for listbox -> click the
    matching option), never a single .select_option() call.
  - Every field lives inside <div role="dialog">, so every locator in the
    new section below is meant to be resolved off a `dialog` Locator
    (`page.locator(DIALOG_SELECTOR)`), not off `page` directly. The one
    exception is the very first click that OPENS the dialog in the first
    place (nothing to scope to yet).
  - The Summary screen's final submit button has the SAME accessible
    name ("Add Order") as the button that opens the dialog from the
    orders list. Always resolve it via `dialog.get_by_role(...)`, never
    `page.get_by_role(...)`, or Playwright will raise a strict-mode
    "multiple elements matched" error once the dialog is open (the
    original trigger button is still technically in the DOM, just
    visually behind the modal overlay).

All old app.snipercrm.io selectors below this point have been removed --
they no longer apply to anything and left in place would just be a
foot-gun if some other module still imported them.
------------------------------------------------------------------------

CONFIRMED against real HTML (Sep 2026, second pass):
  - PAYMENT_METHOD / ORDER_SOURCE triggers do follow the
    `label:has-text(X) + button[role="combobox"]` pattern -- confirmed.
  - Product lines are NOT reliably 2-comboboxes-each. Confirmed instead:
    each product line lives in its own `.rounded-md` container directly
    under a `.space-y-3` wrapper (added top-to-bottom as "Add next
    product" is clicked), and that container holds EITHER 1 combobox
    (product selection and price-bundle are the same dropdown, price
    embedded in the option label) OR 2 (product, then a separate
    price-bundle dropdown) -- this can vary, so add_products.py checks
    the actual combobox count inside each block at runtime instead of
    assuming one layout. See PRODUCT_BLOCK_SELECTOR below and
    _select_product_and_price() in add_products.py.
  - A dedicated "Close" (X) button exists on the dialog, separate from
    the Products screen's "Cancel" button -- useful for abandoning the
    wizard without submitting (e.g. during catalog scraping).
"""

from __future__ import annotations

# ==============================================================================
# Orders-list page (before the modal is open -- resolve off `page`, not `dialog`)
# ==============================================================================
ADD_ORDER_BUTTON_NAME = "Add Order"
# The very first click of the run. Nothing else is on-screen yet, so an
# exact accessible-name match off `page` is safe and unambiguous here.

# ==============================================================================
# "Add Order" type-picker (first thing shown after the click above)
# ==============================================================================
MULTI_PRODUCT_BUTTON_NAME = "Multiple products (one order)"

# ==============================================================================
# The modal itself
# ==============================================================================
DIALOG_SELECTOR = 'div[role="dialog"]'

# ==============================================================================
# Screen 1 -- "Add Order — multiple products (Products)"
# ==============================================================================
OWNING_FORM_LABEL = "Owning Form"
# CONFIRMED business rule: this selection never varies order-to-order.
OWNING_FORM_VALUE = "combo set"

QUANTITY_INPUT_SELECTOR = 'label:has-text("Quantity") + input[type="number"]'
AMOUNT_INPUT_SELECTOR = 'label:has-text("Amount (₦)") + input[type="number"]'

# One container per product line, in DOM order -- confirmed from the
# live HTML. `.nth(index)` reliably targets line `index` (0-based) since
# blocks are only ever appended, never reordered/removed. Everything
# specific to one product line (its combobox(es), Quantity, Amount)
# should be scoped through this rather than flat-indexed across the
# whole dialog, since the combobox COUNT per block isn't fixed (see the
# migration note above).
PRODUCT_BLOCK_SELECTOR = '.space-y-3 > .rounded-md'

ADD_NEXT_PRODUCT_BUTTON_NAME = "Add next product"
NEXT_BUTTON_NAME = "Next"
BACK_BUTTON_NAME = "Back"
CANCEL_BUTTON_NAME = "Cancel"      # Products screen only
CLOSE_BUTTON_NAME = "Close"        # the dialog's own X -- present on every screen

# ==============================================================================
# Screen 2 -- "Add Order — multiple products (Charges)"
# ==============================================================================
# UNCONFIRMED: inferred from the same label+combobox pattern used
# everywhere else in this wizard (Owning Form, State). Verify against the
# live site -- if the trigger doesn't open, this is the first place to
# check.
PAYMENT_METHOD_LABEL = "Payment Method"
PAYMENT_METHOD_VALUE = "Cash"          # business rule 12: this NEVER changes

ORDER_SOURCE_LABEL = "Order Source"
ORDER_SOURCE_VALUE = "WhatsApp"        # business rule: this NEVER changes

DISCOUNT_INPUT_SELECTOR = 'label:has-text("Discount (₦)") + input'
DELIVERY_FEE_INPUT_SELECTOR = 'label:has-text("Delivery fee (₦)") + input'
# Neither is filled by this automation -- no Excel column carries this
# data, so both are left at their 0 defaults, same as before.

# ==============================================================================
# Screen 3 -- "Add Order — multiple products (Customer)"
# ==============================================================================
FULL_NAME_INPUT_SELECTOR = 'label:has-text("Full Name") + input'
PHONE_INPUT_SELECTOR = 'label:has-text("Phone") + input'
WHATSAPP_INPUT_SELECTOR = 'label:has-text("WhatsApp") + input'
EMAIL_INPUT_SELECTOR = 'label:has-text("Email") + input'
ADDRESS_INPUT_SELECTOR = 'label:has-text("Delivery Address") + input'
STATE_LABEL = "State"
# State options were confirmed to need CONTAINS matching, not exact
# (e.g. data-value might be "Lagos State" rather than plain "Lagos") --
# see combo_option(..., match_mode="contains") below.

# ==============================================================================
# Screen 4 -- "Add Order — multiple products (Summary)"
# ==============================================================================
# SAME accessible name as ADD_ORDER_BUTTON_NAME above -- see the
# MIGRATION NOTE at the top of this file. Always resolve this off the
# open `dialog` locator, never `page`.
FINAL_SUBMIT_BUTTON_NAME = "Add Order"

# Confirmed by business: clicking this shows a loading state on the
# button but produces NO success/error popup afterward. Outcome is
# inferred by whether the dialog closes (success) vs. stays open with
# inline error text (failure) -- see detect_save_outcome() in
# add_products.py.


# ==============================================================================
# Helper builders -- every combobox and option in this wizard follows the
# same two patterns, so build locator strings from these instead of
# hand-writing each one.
# ==============================================================================
def combo_trigger(label_text: str) -> str:
    """CSS locator (Playwright's extended selector syntax) for a
    shadcn/ui Select trigger button, given the visible <label> text
    immediately before it. Resolve this off a `dialog` Locator, e.g.
    `dialog.locator(combo_trigger(STATE_LABEL))`."""
    return f'label:has-text("{label_text}") + button[role="combobox"]'


def combo_option(value: str, match_mode: str = "exact") -> str:
    """CSS locator for an option inside an already-open
    [role="listbox"]. match_mode="exact" -> [data-value="X"],
    "contains" -> [data-value*="X"] (needed for State -- see note
    above)."""
    op = "=" if match_mode == "exact" else "*="
    safe_value = value.replace('"', '\\"')
    return f'[role="option"][data-value{op}"{safe_value}"]'


LISTBOX_SELECTOR = '[role="listbox"]'
OPTION_SELECTOR = '[role="option"]'

# CONFIRMED from live DOM (Sep 2026): every combobox in this wizard is a
# cmdk-based command-palette component, not a plain native/Radix select.
# Opening the trigger reveals a search <input cmdk-input> above the
# [role="listbox"], and that input live-filters the option list as you
# type. Lists can be long (100+ items for Owning Form / Product) and can
# contain genuine duplicate labels (confirmed: e.g. two separate options
# both labeled "Rolex New"), so add_products.py types the target value
# into this search box to narrow the list BEFORE clicking a
# `[role="option"][data-value=...]` match, rather than trusting a blind
# click against the full unfiltered list.
#
# CRITICAL, also confirmed from live DOM: the popover holding that search
# input + listbox is PORTALED outside the outer "Add Order" dialog's own
# DOM subtree (Radix renders it as a separate top-level node, not nested
# inside the modal), and the popover's own wrapper can itself carry
# role="dialog". So while every combobox TRIGGER button lives inside the
# outer dialog (safe to find via `dialog.locator(...)`), the search
# input / listbox / options that appear after clicking it do NOT -- they
# must be located from `page` (the whole document), never scoped to
# `dialog`, or the lookup will silently search the wrong subtree and
# time out even though the popover is genuinely open on screen.
COMBOBOX_SEARCH_INPUT_SELECTOR = '[cmdk-input]'