"""Timeouts, order states, and the CRM's fixed vocabulary."""

from __future__ import annotations

import re


# ============================ constants =====================================

LOGIN_TIMEOUT_MS = 50_000

NAV_TIMEOUT_MS = 40_000


# Used by both the initial login and the browser-recovery re-login, plus the
# _confirm_orders_page() second check.
LOGIN_NETWORK_WAIT_MS = 100_000


PER_ORDER_DEADLINE_S = 90          # watchdog: never sit on one customer

MAX_BROWSER_RECOVERIES = 3

MAX_NETWORK_WAIT_S = 600           # total pause for one network outage

NETWORK_PROBE_URL = "https://www.google.com/generate_204"

VERIFY_ATTEMPTS = 4


# The filter bar (chips, Period, User) renders as a SECOND wave after the
# base orders page ("Add Order" button) is ready.
FILTERS_READY_TIMEOUT_MS = 20_000


# FIX: the User trigger is disabled by the CRM until a "Filter By" chip is
# applied. Wait for it to become enabled instead of a blind 30s click.
USER_ENABLE_TIMEOUT_MS = 15_000

# FIX: how long a chip gets to render after its option/Confirm was clicked.
CHIP_CONFIRM_TIMEOUT_MS = 2_500

# Plan doc §1: the UI collapses below `md`; use >= 1440x900.
MIN_VIEWPORT = (1440, 900)


TARGET_STATUS = "Delivered"

DEFAULT_AGENT = "Lagos"

DEFAULT_PERIOD = "This Month"

FORM_CHIP = "combo set"

# Live label is "Assigned To" (the plan doc says "Assigned By"); either
# spelling is accepted wherever a chip is looked up (see CHIP_ALIASES).
FILTER_CHIPS = ("Added By", "Assigned To")

CHIP_ALIASES = {
    "Assigned To": ("Assigned To", "Assigned By"),
    "Assigned By": ("Assigned To", "Assigned By"),
}


# FIX: label spellings each multi-select filter group may be rendered with
# (matched case-insensitively).
GROUP_LABEL_ALIASES = {
    "Order by Forms": ("Order by Forms", "Order by Form", "Forms", "Form"),
    "Filter By": ("Filter By", "Filters", "Filter"),
}

CONFIRM_BUTTON_RX = re.compile(r"^\s*(?:confirm|apply|done|ok)\s*$", re.I)


# Durable state machine (doc §3)
DISCOVERED = "DISCOVERED"

ROW_FOUND = "ROW_FOUND"

MENU_OPEN = "MENU_OPEN"

STATUS_MENU_OPEN = "STATUS_MENU_OPEN"

MODAL_OPEN = "MODAL_OPEN"

FIELDS_SELECTED = "FIELDS_SELECTED"

READY_TO_SAVE = "READY_TO_SAVE"

SAVE_SUBMITTED = "SAVE_SUBMITTED"

VERIFYING = "VERIFYING"

SUCCESS = "SUCCESS"

FAILED_RETRYABLE = "FAILED_RETRYABLE"

WAITING_FOR_NETWORK = "WAITING_FOR_NETWORK"

WAITING_FOR_BROWSER = "WAITING_FOR_BROWSER"

FAILED_FINAL = "FAILED_FINAL"



SUCCESS_OUTCOMES = ("SUCCESS", "ALREADY_DELIVERED")
