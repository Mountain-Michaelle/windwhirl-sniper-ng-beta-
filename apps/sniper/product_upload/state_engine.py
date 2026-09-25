"""
state_engine.py

Implements business logic spec section 11: the Excel "State" column is not
trusted. The state to select in the CRM's #state dropdown is inferred from
the free-text customer address instead, exactly like a human reading the
address would do it.

REWRITE (Oct 2026, speed pass): all web-lookup machinery removed. The
former infer_state_with_search()/_web_lookup_state()/prewarm_state_cache()
pipeline (DuckDuckGo queries, 2-attempt retries, disk-backed
state_cache.json, --refresh-state-cache) is gone entirely:

  - a state found in the local tables -> selected in the form as before;
  - NO state inferable -> the caller now SKIPS the state field entirely
    and saves (the form allows it), instead of paying a web lookup and
    landing on a Lagos fallback;
  - a BLANK address -> the caller flags it for a human (first run) or
    proceeds with its own default (second run) -- never this module's job.

A tiny in-process memo keeps repeated identical addresses in big (200+ row)
files from paying the regex work twice. No network is ever touched.
"""

from __future__ import annotations

import re
import threading
from typing import Dict, Optional

NIGERIAN_STATES = [
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue",
    "Borno", "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu",
    "Gombe", "Imo", "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi",
    "Kwara", "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun", "Oyo",
    "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara", "Abuja",  
]

_STATE_ALIASES = {
    "abuja": "Abuja",
    "fct": "Abuja",
    "federal capital territory": "Abuja",
    "akwaibom": "Akwa Ibom",
    "crossriver": "Cross River",
    "river state": "Rivers",
    "Lafia": "Nasarawa",
    "benin": "Edo",
    "jalingo": "Taraba",
    "port harcourt": "Rivers",
    "nsukka": "Enugu",
    "uyo":"Akwa Ibom",
    "ibadan":"Oyo",
    "abeokuta":"Ogun",
    "umuahia": "Abia",
    "porthacourt": "Rivers",
    "jos": "Plateau",
}

_LANDMARK_TO_STATE = {
    "nsukka": "Enugu",
    "mararaba": "Nasarawa",
    "university of nigeria": "Enugu",
    "unn": "Enugu",
    "alvan ikoku": "Imo",
    "owerri": "Imo",
    "futo": "Imo",
    "ikeja": "Lagos",
    "meiran": "Lagos",
    "lekki": "Lagos",
    "yaba": "Lagos",
    "surulere": "Lagos",
    "unilag": "Lagos",
    "ikorodu": "Lagos",
    "badagry": "Lagos",
    "ibadan": "Oyo",
    "ui ": "Oyo",
    "uniben": "Edo",
    "benin city": "Edo",
    "nnamdi azikiwe": "Anambra",
    "unizik": "Anambra",
    "awka": "Anambra",
    "obosi": "Anambra",
    "achina": "Anambra",
    "port harcourt": "Rivers",
    "portharcourt": "Rivers",
    "uniport": "Rivers",
    "omoku": "Rivers",
    "abuja": "Abuja",
    "fct":"Abuja",
    "kano city": "Kano",
    "ahmadu bello": "Kaduna",
    "abu zaria": "Kaduna",
    "zaria": "Kaduna",
    "calabar": "Cross River",
    "unical": "Cross River",
    "ikom": "Cross River",
    "ilorin": "Kwara",
    "unilorin": "Kwara",
    "warri": "Delta",
    "asaba": "Delta",
    "agbor": "Delta",
    "ughoton": "Delta",
    "abakaliki": "Ebonyi",
    "ebonyi state": "Ebonyi",
    "federal capital territory": "Abuja",
    "akwaibom": "Akwa Ibom",
    "lafia":"Nasarawa",
    "Benin":"Edo",    
}


def _normalize(text) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text.lower().strip())


def infer_state(address) -> Optional[str]:
    if not isinstance(address, str) or not address.strip():
        return None
    norm = _normalize(address)
    if not norm:
        return None

    for state in NIGERIAN_STATES:
        pattern = r"\b" + re.escape(state.lower()) + r"\b"
        if re.search(pattern, norm):
            return state

    for alias, canonical in _STATE_ALIASES.items():
        if alias in norm:
            return canonical

    for landmark, canonical in _LANDMARK_TO_STATE.items():
        if landmark in norm:
            return canonical

    return None


# ==============================================================================
# Local-only inference (Oct 2026 speed pass)
# ==============================================================================
# The web-lookup pipeline that used to live below this line
# (_web_lookup_state, infer_state_with_search, prewarm_state_cache,
# load/save_state_cache, state_cache.json, --refresh-state-cache) has been
# REMOVED. Contract with callers:
#
#   - infer_state() hits  -> caller selects that state in the form (unchanged);
#   - infer_state() miss  -> caller SKIPS the state field entirely and saves
#     (the form allows saving without a state) -- no network, no fallback;
#   - blank address       -> caller's own first/second-run human-flag flow.
#
# A small in-process memo means a 200-row file containing the same
# misspelled address 40 times pays the regex work once. It survives --lock
# session relaunches (same process) and costs nothing to maintain.

_MEMO: Dict[str, Optional[str]] = {}
_MEMO_LOCK = threading.Lock()


def infer_state_memoized(address) -> Optional[str]:
    """infer_state() with an in-process memo on the normalized address.
    Identical results, no network, and repeated identical addresses in a
    big file stop re-paying the alias/landmark scans."""
    if not isinstance(address, str) or not address.strip():
        return None
    norm = _normalize(address)
    if not norm:
        return None
    with _MEMO_LOCK:
        if norm in _MEMO:
            return _MEMO[norm]
    state = infer_state(address)
    with _MEMO_LOCK:
        _MEMO[norm] = state
    return state


def clear_memo() -> None:
    """Test helper: wipe the in-process memo."""
    with _MEMO_LOCK:
        _MEMO.clear()
