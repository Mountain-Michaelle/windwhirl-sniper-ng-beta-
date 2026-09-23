"""
gender_utils.py — detects gender from a first name using genderize.io.

Defaults to Nigeria (NG) for best accuracy with Nigerian names.
Pass any ISO 3166-1 alpha-2 country code to extend to other countries.

Returns "male", "female", or the original name if unsure.
"""

import logging
import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 6
CONFIDENCE_THRESHOLD = 0.70  # below this, we consider it unsure
DEFAULT_COUNTRY = "NG"       # Nigeria — change per deployment region if needed


def get_gender(first_name: str, country_code: str = DEFAULT_COUNTRY) -> str:
    """
    Accepts a first name and optional country code (ISO 3166-1 alpha-2).
    Returns "male", "female", or the name itself if unsure or API fails.

    Args:
        first_name:   The customer's first name e.g. "Blessing"
        country_code: ISO country code e.g. "NG", "GH", "GB", "US"
                      Defaults to "NG" (Nigeria)

    Examples:
        get_gender("Blessing")           → "female"
        get_gender("Chinedu")            → "male"
        get_gender("Alex", "GB")         → "male"
        get_gender("Eze")                → "Eze"  (unsure)
    """
    name = first_name.strip()
    if not name:
        return first_name

    try:
        resp = requests.get(
            "https://api.genderize.io",
            params={"name": name, "country_id": country_code.upper()},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        gender      = data.get("gender")
        probability = data.get("probability", 0)

        if not gender or probability < CONFIDENCE_THRESHOLD:
            log.info(
                "[genderize] '%s' (%s) → unsure (probability=%.2f)",
                name, country_code, probability or 0,
            )
            return name  # return name as-is when unsure

        log.info(
            "[genderize] '%s' (%s) → %s (probability=%.2f)",
            name, country_code, gender, probability,
        )
        return gender

    except Exception as exc:
        log.warning("[genderize] failed for '%s': %s", name, exc)
        return name  # return name as-is on any failure


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Usage: python gender_utils.py [country_code] [name1 name2 ...]
    # e.g.   python gender_utils.py NG Blessing Chinedu Titilayo
    #        python gender_utils.py GH Kofi Ama
    #        python gender_utils.py US Alex Jordan

    args = sys.argv[1:]

    # First arg is country code if it looks like one (2 letters)
    if args and len(args[0]) == 2 and args[0].isalpha():
        country = args[0].upper()
        names   = args[1:] or ["Blessing", "Chinedu", "Titilayo", "Michael", "Eze", "Alex"]
    else:
        country = DEFAULT_COUNTRY
        names   = args or ["Blessing", "Chinedu", "Titilayo", "Michael", "Eze", "Alex"]

    print(f"\n  Country: {country}\n")
    for name in names:
        result = get_gender(name, country)
        print(f"  {name:<15} →  {result}")
    print()