"""
date_utils.py — formats an order date into a natural English string.

Input:  a datetime object or a string like "25-08-2025 17:14:20"
Output: "25th August 2025"

Usage:
    from apps.core.lib.utils.date_utils import format_order_date

    format_order_date(customer["order_date"])  → "25th August 2025"
"""

import datetime
import logging

log = logging.getLogger(__name__)


def _ordinal(day: int) -> str:
    """Return day number with correct suffix: 1st, 2nd, 3rd, 4th ... 25th."""
    if 11 <= day <= 13:
        # Special case — 11th, 12th, 13th (not 11st, 12nd, 13rd)
        return f"{day}th"
    suffixes = {1: "st", 2: "nd", 3: "rd"}
    return f"{day}{suffixes.get(day % 10, 'th')}"


def format_order_date(order_date, fallback: str = "recently") -> str:
    """
    Format an order date into a natural readable string.

    Args:
        order_date: datetime object, or string in format "DD-MM-YYYY HH:MM:SS"
                    or "YYYY-MM-DD HH:MM:SS". Accepts both.
        fallback:   Returned as-is if the date cannot be parsed.
                    Defaults to "recently" so messages still read naturally.

    Returns:
        e.g. "25th August 2025"
        or the fallback string if parsing fails.

    Examples:
        format_order_date(datetime(2025, 8, 25))     → "25th August 2025"
        format_order_date("25-08-2025 17:14:20")     → "25th August 2025"
        format_order_date("2025-08-25 17:14:20")     → "25th August 2025"
        format_order_date(None)                       → "recently"
    """
    if order_date is None:
        return fallback

    # If it's already a datetime, use it directly
    if isinstance(order_date, datetime.datetime):
        dt = order_date

    elif isinstance(order_date, str):
        order_date = order_date.strip()
        # Try DD-MM-YYYY HH:MM:SS (your Excel format)
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(order_date, fmt)
                break
            except ValueError:
                continue
        else:
            log.warning("[date_utils] Could not parse date string: '%s'", order_date)
            return fallback

    else:
        log.warning("[date_utils] Unexpected date type: %s", type(order_date))
        return fallback

    return f"{_ordinal(dt.day)} {dt.strftime('%B')} {dt.year}"


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        "25-08-2025 17:14:20",
        "01-01-2025 09:00:00",
        "11-03-2024 00:00:00",
        "12-06-2023 12:30:00",
        "13-09-2022 08:15:00",
        "2025-08-25 17:14:20",
        datetime.datetime(2025, 3, 2),
        None,
        "garbage",
    ]

    print()
    for t in tests:
        print(f"  {str(t):<30}  →  {format_order_date(t)}")
    print()