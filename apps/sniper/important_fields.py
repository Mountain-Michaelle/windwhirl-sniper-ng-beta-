# important_fields.py
"""
Pure configuration for the recorder -- no logic lives here. Tune these
per site/session; nothing above this file should ever need editing to
change what gets captured.

The recorder is now site-agnostic by design (see workflow_recorder.py /
recorder.js): these lists are for NOISE REDUCTION, not for deciding
whether something "looks like a business field". Adding a keyword here
will never make something get captured -- capture is gated purely by
whether the element is interactive. The one place vocabulary still
matters is FRIENDLY_LABEL_OVERRIDES below, and even that's cosmetic.
"""

# URLs the recorder should NEVER capture on, regardless of anything
# else -- checked as case-insensitive regexes against the full page
# URL. Use this for login pages, payment pages, or anything with
# sensitive fields beyond what the password-field block already
# covers. Empty list = capture everywhere.
EXCLUDE_URL_PATTERNS = [
    r"/login",
    r"/signin",
    r"/sign-in",
]

# How many NEW interactive elements have to appear at once (a modal
# opening, a wizard step rendering, a tab switching) before the
# recorder takes a fresh full-page "structure" snapshot. Lower = more
# snapshots, catches smaller UI changes. Higher = fewer, coarser ones.
STRUCTURE_SNAPSHOT_THRESHOLD = 3

# HTTP methods worth logging as "Network Call" steps. GET is excluded
# by default (page loads/polling produce a lot of noise); add "GET"
# here if you specifically need to see data-loading calls too.
NETWORK_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# Any request URL containing one of these substrings is skipped
# outright, before the method check even runs. Generic analytics/
# tracking/error-reporting noise, not tied to any one site.
NETWORK_EXCLUDE_DOMAINS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com/tr",
    "hotjar.com",
    "sentry.io",
    "intercom.io",
    "segment.io",
    "mixpanel.com",
)

# Static asset extensions -- never worth a "Network Call" step.
NETWORK_EXCLUDE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".woff", ".woff2", ".ttf", ".map",
)

# Where captured network calls go, relative to the field/action steps
# that actually drive automation. A single-page app (Supabase, Firebase,
# GraphQL backends, etc.) commonly fires several housekeeping POST/PUT
# calls per page load or route change (auth/role checks, counters) that
# have nothing to do with what you clicked or typed. Filtering every
# such backend by NETWORK_EXCLUDE_DOMAINS/PATTERNS is a losing game of
# whack-a-mole, and it's easy for these to outnumber your real steps
# 10-to-1 in the log you're trying to build an automation script from.
#   "separate" (default) -- still captured (nothing is lost), but
#       written to its own {session}_network.json instead of being
#       interleaved into the numbered step list that .txt/.json/the
#       generated Playwright script are built from.
#   "inline"  -- old behavior: interleaved into the same numbered step
#       list as fields/actions.
#   "action"  -- attach minimal response verification to the most recent
#       UI step only (method, path, status); never add a backend step.
#   "off"     -- don't capture network calls at all.
NETWORK_CAPTURE_MODE = "action"

# Optional, additional noise filter for NETWORK_CAPTURE_MODE="inline"
# or when you still want fewer entries in the separate network file:
# regexes checked against the full request URL (case-insensitive), same
# semantics as EXCLUDE_URL_PATTERNS. Empty by default -- nothing here
# affects field/action capture, only which network calls get logged at
# all. Example, if you find a specific backend's housekeeping calls are
# never useful to you:
#   NETWORK_EXCLUDE_PATH_PATTERNS = [r"rpc/has_role", r"rpc/current_app_user"]
NETWORK_EXCLUDE_PATH_PATTERNS = []

# OPTIONAL, purely cosmetic: friendly relabeling for the human-readable
# log, keyed (kind, action_type, keyword-found-in-description) ->
# friendly label. This NEVER decides whether something is captured --
# capture is gated by interactivity in recorder.js. Leave empty for a
# fully generic log with raw computed labels. Kept a few of your old
# SniperCRM ones here as an example of the opt-in shape:
FRIENDLY_LABEL_OVERRIDES = {
    ("field", "type", "customer"): "Search Customer",
    ("field", "select", "customer"): "Select Customer",
    ("field", "type", "phone"): "Fill Phone",
    ("field", "select", "product"): "Choose Product",
    ("field", "select", "variant"): "Choose Variant",
    ("field", "select", "state"): "Choose State",
    ("field", "select", "payment"): "Payment Method",
    ("action", "click", "save"): "Save Order",
    ("action", "click", "submit"): "Submit",
    ("action", "submit", "form"): "Submit Form",
}