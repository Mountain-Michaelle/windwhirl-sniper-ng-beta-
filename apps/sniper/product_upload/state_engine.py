"""
state_engine.py

Implements business logic spec section 11: the Excel "State" column is not
trusted. The state to select in the CRM's #state dropdown is inferred from
the free-text customer address instead, exactly like a human reading the
address would do it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
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
# Light, safe web lookup for addresses the local tables cannot resolve
# ==============================================================================
# Oct 2026 business rule: "there should always be a very light safe search
# which will say which state in Nigeria is <address>. The search should be
# exact, the first known-state result should be used, and if it is still not
# known after a second attempt, fall back to Lagos."
#
# Design constraints, in priority order:
#   1. NEVER slow a normal run down: this runs ONLY when infer_state() above
#      already returned None (the rare path), and every address is cached, so
#      repeated identical addresses cost exactly one lookup per run.
#   2. NEVER raise and NEVER hang: one short HTTP timeout per attempt, all
#      exceptions swallowed, hard cap of ATTEMPTS web calls per address.
#   3. NEVER guess a wrong state: the response text is scanned for a
#      word-boundary match against the known state names/aliases. If nothing
#      known appears, the attempt is a miss -- the caller keeps its existing
# One DDG-lite lookup costs a single HTTP round trip and returns a small HTML
# page; a state name either appears in the first result set or it never
# will. Two attempts exist only for TRANSPORT errors (DNS/timeout/reset) --
# a clean "no known state mentioned" is final and is NOT retried. Retrying a
# clean miss immediately never changes the answer but costs a full timeout.
# Attempt counts live here (not on the caller) so every resolver path --
# sequential planning, parallel pre-warming, tests -- shares one policy.
SEARCH_ATTEMPTS = 2
# PERF (Oct 2026, "this stage is very long waiting" + "when i run 200 orders
# it will take time"): DDG-lite answers in well under a second on a live
# connection. 1.5 s keeps the worst case per unresolvable address at ~3 s
# even when both attempts fire, and the job-lifecycle disk cache below means
# a miss is only ever paid ONCE per address per job -- every later run is a
# local file read.
SEARCH_TIMEOUT_S = 1.5
# In-memory cache: normalized address -> resolved state (or None when the web
# lookup missed twice / errored). Survives --lock session relaunches inside
# one process; the disk cache below survives across processes/runs.
_SEARCH_CACHE: Dict[str, Optional[str]] = {}
# Per-address provenance for logging + disk persistence: normalized address
# -> {"state": str|None, "source": "web"|"web-miss", "resolved_at": iso}.
# Local-inference hits are instant and never stored -- only web outcomes are
# persisted, so a re-run never re-pays a lookup.
_SEARCH_CACHE_META: Dict[str, Dict[str, object]] = {}
_CACHE_LOCK = threading.Lock()
# Set by _web_lookup_state on every call so infer_state_with_search can tell
# a TRANSPORT error (retry once) from a clean "no state mentioned" (final,
# no retry). Thread-local because parallel pre-warming resolves on threads.
_lookup_error_local = threading.local()
# Job-lifecycle temporal cache: one small JSON file per job state dir, reused
# for the whole life of the job across runs. Mirrors the RetryCache atomic
# write pattern (tmp + os.replace) so a killed process never corrupts it.
STATE_CACHE_FILENAME = "state_cache.json"
# Module-level quiet flag for parallel pre-warming: worker threads get their
# OWN thread-local storage, so the per-thread `quiet` above cannot silence
# them -- this global covers the whole pool while it runs (set/reset only
# around pool.map, which completes before reset, so no leakage).
_PREWARM_QUIET = False

_NIGERIAN_STATE_TOKENS: list[tuple[str, str]] = sorted(
    [(s.lower(), s) for s in NIGERIAN_STATES]
    + [(alias, canonical) for alias, canonical in _STATE_ALIASES.items()],
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def _state_from_search_text(text: str) -> Optional[str]:
    """First known state whose name appears (word-boundary) in the result
    text. Longest tokens are checked first so 'cross river' wins over the
    shorter 'river', and 'akwa ibom' over 'ibom'."""
    norm = _normalize(text)
    if not norm:
        return None
    for token, canonical in _NIGERIAN_STATE_TOKENS:
        if re.search(r"\b" + re.escape(token) + r"\b", norm):
            return canonical
    return None


def _web_lookup_state(address: str) -> Optional[str]:
    """One light HTTP lookup, fully guarded. Returns a state or None.

    Records whether THIS call failed at the transport layer (DNS, timeout,
    reset) on `_lookup_error_local.had_error` so the caller retries only
    transport errors -- a clean "no known state mentioned" is final and is
    NOT retried (retrying it immediately never changes the answer)."""
    import urllib.parse
    import urllib.request

    _lookup_error_local.had_error = False
    query = urllib.parse.quote_plus(f"which state in Nigeria is {address}")
    url = f"https://html.duckduckgo.com/html/?q={query}"
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; order-uploader/1.0)",
        })
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_S) as response:
            body = response.read().decode("utf-8", errors="replace")
    except Exception:
        _lookup_error_local.had_error = True
        return None
    return _state_from_search_text(body)


def _remember_web_result(key: str, address: str, found: Optional[str]) -> None:
    """Store a web outcome in memory (+ provenance for logging/disk)."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _CACHE_LOCK:
        _SEARCH_CACHE[key] = found
        _SEARCH_CACHE_META[key] = {
            "state": found,
            "source": "web" if found is not None else "web-miss",
            "resolved_at": stamp,
            "address": address[:120],
        }


def cache_lookup_source(address) -> Optional[str]:
    """Provenance of a cached address for per-row logging: 'local' when the
    local tables resolve it, 'web'/'web-miss' (+', disk' when it came from
    the job-lifecycle file rather than a fresh lookup this run), else None.
    Never touches the network."""
    if infer_state(address) is not None:
        return "local"
    key = _normalize(address) if isinstance(address, str) else ""
    if not key:
        return None
    with _CACHE_LOCK:
        meta = _SEARCH_CACHE_META.get(key)
    if meta is None:
        return None
    source = str(meta.get("source", "web"))
    if meta.get("from_disk"):
        source += ", disk"
    return source


def load_state_cache(progress_dir) -> int:
    """Seed the in-memory cache from the job's state_cache.json. Returns the
    number of entries loaded. Missing/corrupt file -> 0, never raises (a
    re-run simply re-pays the lookups exactly once more)."""
    try:
        raw = Path(progress_dir) / STATE_CACHE_FILENAME
        data = json.loads(raw.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    loaded = 0
    with _CACHE_LOCK:
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            state = entry.get("state")
            if state is not None and not isinstance(state, str):
                continue
            _SEARCH_CACHE[key] = state
            meta = dict(entry)
            meta["from_disk"] = True
            _SEARCH_CACHE_META[key] = meta
            loaded += 1
    return loaded


def save_state_cache(progress_dir) -> int:
    """Persist web outcomes to the job's state_cache.json (atomic tmp +
    os.replace, same pattern as RetryCache). Best-effort: never raises, so a
    cache write can never fail a run. Returns entries written."""
    try:
        directory = Path(progress_dir)
        directory.mkdir(parents=True, exist_ok=True)
        with _CACHE_LOCK:
            snapshot = {k: dict(v) for k, v in _SEARCH_CACHE_META.items()}
        for meta in snapshot.values():
            meta.pop("from_disk", None)
        # Merge with whatever is already on disk (another --workers shard
        # child may have saved its own disjoint addresses concurrently):
        # last-write-wins PER KEY, never per file, so concurrent savers only
        # ever add keys, never wipe each other.
        merged: Dict[str, Dict[str, object]] = {}
        try:
            disk = json.loads((directory / STATE_CACHE_FILENAME).read_text(
                encoding="utf-8"))
            if isinstance(disk, dict):
                for k, v in disk.items():
                    if isinstance(v, dict):
                        merged[k] = v
        except Exception:
            pass
        merged.update(snapshot)
        merged.update(snapshot)
        tmp = directory / (STATE_CACHE_FILENAME + ".tmp")
        tmp.write_text(json.dumps(merged, indent=1), encoding="utf-8")
        import os
        os.replace(tmp, directory / STATE_CACHE_FILENAME)
        return len(merged)
    except Exception:
        return 0


def infer_state_with_search(address) -> Optional[str]:
    """infer_state() first (local, instant, no network). Only if that misses,
    consult the job-lifecycle cache, else run the light web lookup: attempt 1
    always fires; attempt 2 fires ONLY on a transport error (timeout/DNS),
    never on a clean "no known state mentioned". Returns None when everything
    misses -- the caller's existing Lagos fallback then applies, so behaviour
    for every address the local tables already resolve is completely
    unchanged. Thread-safe: parallel pre-warming shares this path."""
    local = infer_state(address)
    if local is not None:
        return local

    if not isinstance(address, str) or not address.strip():
        return None

    key = _normalize(address)
    with _CACHE_LOCK:
        if key in _SEARCH_CACHE:
            return _SEARCH_CACHE[key]

    # VISIBILITY (Oct 2026, "very long waiting" with zero output): planning
    # runs silently row after row, so a slow network looks like a hang. One
    # line before the lookup fires turns the wait into visible progress.
    # (Suppressed during parallel pre-warming -- that path logs one summary
    # line for the whole batch instead of one line per address. The global
    # covers pool worker threads, which get fresh thread-local storage.)
    global _PREWARM_QUIET
    if (not getattr(_lookup_error_local, "quiet", False)
            and not _PREWARM_QUIET):
        print(f"[plan] address not locally resolvable -- 1 quick web lookup: "
              f"{address[:80]!r}...", flush=True)

    found: Optional[str] = None
    for _attempt_no in range(SEARCH_ATTEMPTS):
        # Reset first: a monkeypatched backend (tests) never sets the flag,
        # so without this a stale True from an earlier transport error in
        # this thread would wrongly earn a clean miss a second attempt.
        _lookup_error_local.had_error = False
        try:
            candidate = _web_lookup_state(address)
            errored = bool(getattr(_lookup_error_local, "had_error", False))
        except Exception:
            candidate = None
            errored = True
        if candidate is not None:
            # Defensive: a monkeypatched or future backend may hand back raw
            # result text instead of a parsed state -- extract the first known
            # state exactly as _web_lookup_state does for live HTTP bodies.
            parsed = _state_from_search_text(candidate)
            found = parsed if parsed is not None else candidate
            if found is not None:
                break
        # Clean miss (HTTP worked, no state mentioned): final -- a second
        # identical query cannot change the answer. Only a transport error
        # earns attempt 2.
        if not errored:
            break
        # attempt_no == 0 + errored -> loop runs attempt 2 automatically.

    _remember_web_result(key, address, found)
    return found


def prewarm_state_cache(addresses, max_workers: int = 8) -> Dict[str, Optional[str]]:
    """Resolve many unresolvable addresses in PARALLEL (one thread each, I/O
    bound -- urllib socket waits release the GIL). Only addresses missing
    from BOTH the local tables and the cache cost a lookup; the rest are
    instant local returns. Returns {address: state-or-None}. Never raises."""
    from concurrent.futures import ThreadPoolExecutor

    unique: list = []
    seen: set = set()
    for address in addresses:
        if not isinstance(address, str) or not address.strip():
            continue
        key = _normalize(address)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(address)
    results: Dict[str, Optional[str]] = {}
    to_resolve = [a for a in unique if cache_lookup_source(a) is None]
    if not to_resolve:
        for address in unique:
            results[address] = infer_state_with_search(address)
        return results
    if len(to_resolve) == 1:
        results[to_resolve[0]] = infer_state_with_search(to_resolve[0])
    else:
        global _PREWARM_QUIET
        _PREWARM_QUIET = True
        try:
            print(f"[plan] {len(to_resolve)} address(es) need a web lookup -- "
                  f"resolving in parallel (x{max(1, max_workers)}).",
                  flush=True)
            with ThreadPoolExecutor(max_workers=max(1, max_workers),
                                    thread_name_prefix="state-lookup") as pool:
                for address, state in zip(to_resolve,
                                          pool.map(infer_state_with_search,
                                                   to_resolve)):
                    results[address] = state
        finally:
            _PREWARM_QUIET = False
    for address in unique:
        if address not in results:
            results[address] = infer_state_with_search(address)
    return results