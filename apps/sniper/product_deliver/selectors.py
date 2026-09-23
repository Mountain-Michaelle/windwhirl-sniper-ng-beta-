"""Every CRM CSS/XPath selector in one place (doc section 2)."""

from __future__ import annotations

import re


# ============================ selectors =====================================
# Radix portals: scope to visible/open elements; NEVER generated radix ids.

TAB_BASE = "div.group.relative.inline-flex.cursor-pointer"

PENDING_TAB = 'div.group.relative.inline-flex.cursor-pointer:has-text("Pending")'

DELIVERED_TAB = (
    'div.group.relative.inline-flex.cursor-pointer:has-text("Delivered")'
)

QUICK_SEARCH = 'input[placeholder^="Quick search"]'

QUICK_SEARCH_FALLBACK = 'input[enterkeyhint="search"]'

SEARCH_BUTTON_FALLBACK = 'button:has(svg.lucide-search):has-text("Search")'

SEARCH_BUTTON_LEGACY = 'button:has-text("Search")'

ROW_MENU_TRIGGER = 'td:nth-child(2) button[aria-haspopup="menu"]'

OPEN_MENU = 'div[role="menu"][data-state="open"]'

SUBMENU = 'div[data-side="right"][role="menu"][data-state="open"]'

MODAL = 'div[role="dialog"][data-state="open"]'

MODAL_TITLE = "Mark order as Delivered"

STATUS_CELL_INDEX = 9      # 0-based: Order Status column

CUSTOMER_CELL_INDEX = 5    # 0-based: Customer Name column

CONTACT_CELL_INDEX = 6     # 0-based: Contact column (tel: + wa.me links)

AMOUNT_RE = re.compile(r"[₦#]\s*([\d,]+)")

# Plan doc §2.8 b: expected amount = `Amount: ₦N` in the Product cell (the
# cell can also carry other ₦ figures, so "first ₦ number" can be wrong).
AMOUNT_LABELED_RE = re.compile(r"Amount:?\s*[₦#]\s*([\d,]+)", re.I)


TABLE_ROW = "tbody tr"

# FIX (this revision, "NOT_FOUND for a customer the Pending table DOES
# show"): the empty state ("No orders match your filters") is a
# `<tr><td colspan="28">` row INSIDE the same tbody, so `tbody tr` counted it
# as a data row -- an explicitly empty result was never recognised as empty
# and wait_for_app_idle() returned instantly on an empty table. Every REAL
# data row has >= 6 <td> cells (find_row() relies on the same rule).
TABLE_ROW_DATA = TABLE_ROW + ":has(td:nth-child(6))"

PERIOD_TRIGGER = 'div:has(> div > label:text-is("Period")) button[role="combobox"]'

USER_TRIGGER = 'div:has(> label:text-is("User")) button[role="combobox"]'

CMDK_POPOVER = 'div[data-side="top"][role="dialog"][data-state="open"]:has([cmdk-root])'

CMDK_POPOVER_FALLBACK = 'div[role="dialog"][data-state="open"]:has([cmdk-root])'

CMDK_ITEM = '[cmdk-item][role="option"]'

CMDK_ITEM_NOT_OTHER = '[cmdk-item][role="option"]:not([data-value^="Other "])'

CMDK_ITEM_SALES_REP = '[data-value^="Sales Rep "]'

USER_SENTINELS = ("all", "all users", "all time", "select", "any", "all staff")


# FIX: multi-select popup used by "Order by Forms" / "Filter By".
POPUP_SELECTOR = '[role="dialog"][data-state="open"], [role="listbox"], [cmdk-root]'

POPUP_OPEN_VISIBLE = '[role="dialog"][data-state="open"]:visible'

OPTION_SELECTORS = ('[cmdk-item], [role="option"], [role="menuitemcheckbox"], '
                    '[role="checkbox"], label')
