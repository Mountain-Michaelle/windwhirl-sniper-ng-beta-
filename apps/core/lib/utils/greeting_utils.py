"""
greeting_utils.py — timezone-aware greeting utility.

Fetches authoritative local time from timeapi.io.
Falls back to system clock + zoneinfo if the API fails or returns UTC.
"""

import datetime
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 6
DEFAULT_TIMEZONE = "Africa/Lagos"


def _hour_to_greeting(hour: int) -> str:
    if 0 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    return "Good evening"


def _try_timeapi(timezone: str) -> Optional[int]:
    """
    Fetch local hour from timeapi.io.
    Returns None if the call fails OR if the API ignores the timezone
    (detected by comparing against UTC — all zones returning the same
    hour is a sign the timezone param was silently ignored).
    """
    try:
        resp = requests.get(
            "https://timeapi.io/api/time/current/zone",
            params={"timeZone": timezone},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # timeapi.io returns both the requested timezone name and the datetime
        # If timeZone in response doesn't match what we sent, distrust it
        returned_tz = data.get("timeZone", "")
        if returned_tz.lower() != timezone.lower():
            log.warning(
                "[timeapi.io] timezone mismatch: requested '%s', got '%s' — discarding",
                timezone, returned_tz,
            )
            return None

        hour = int(data["hour"])
        log.info("[timeapi.io] %s → hour=%02d", timezone, hour)
        return hour

    except Exception as exc:
        log.warning("[timeapi.io] failed for %s: %s", timezone, exc)
        return None


def _system_clock_hour(timezone: str) -> int:
    """
    Derive local hour from the system clock + Python's zoneinfo.
    Accurate as long as the IANA timezone database is present (it always
    is on Linux servers; on Windows, install tzdata: pip install tzdata).
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone)
    except Exception:
        try:
            import pytz
            tz = pytz.timezone(timezone)
        except Exception:
            log.error("Cannot load timezone '%s', using UTC.", timezone)
            tz = datetime.timezone.utc

    now = datetime.datetime.now(tz=tz)
    log.info("[system clock + zoneinfo] %s → hour=%02d", timezone, now.hour)
    return now.hour


def get_hour(timezone: str = DEFAULT_TIMEZONE) -> int:
    """Return current local hour (0-23) for the given IANA timezone."""
    hour = _try_timeapi(timezone)
    if hour is not None:
        return hour
    # API failed or returned bad data — fall back to system clock with zoneinfo.
    # On a properly configured server this is perfectly accurate.
    return _system_clock_hour(timezone)


def get_greeting(timezone: str = DEFAULT_TIMEZONE) -> str:
    """Return 'Good morning', 'Good afternoon', or 'Good evening'."""
    return _hour_to_greeting(get_hour(timezone))


def get_full_greeting(first_name: str, timezone: str = DEFAULT_TIMEZONE) -> str:
    """Return a ready-to-send greeting, e.g. 'Good morning Blessing!'"""
    return f"{get_greeting(timezone)} {first_name}!"


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    zones = sys.argv[1:] or [
        "Africa/Lagos",
        "Europe/London",
        "America/New_York",
        "Asia/Tokyo",
    ]

    print()
    for tz in zones:
        hour = get_hour(tz)
        greeting = f"{_hour_to_greeting(hour)} Blessing!"
        print(f"  {tz:<30}  hour={hour:02d}  →  {greeting}")
    print()