"""Customer identity: name joining/matching + Contact phones."""

from __future__ import annotations

import re
from apps.sniper.product_deliver.store import (
    normalize_phone,
)



def clean_name(name: str) -> str:
    """Whitespace/case-insensitive key for matching customer names."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()



def join_name_parts(parts) -> str:
    """Join the CRM's customer-name lines into ONE name.

    FIX (this revision, "NOT_FOUND for a customer the Pending table DOES
    show"): the CRM renders one `span.block` PER NAME LINE, and the
    customer's own form answer decides where it breaks --
    `<span class="block">Oladunni folasade</span><span class="block">Ramat
    </span>` for the sheet row "Oladunni folasade Ramat", while "Ewumi
    Elizabeth" stays a single block. Reading only the first block truncated
    the name, so a row that WAS on screen could never match.
    """
    return " ".join(p.strip() for p in (parts or []) if p and p.strip())



def names_match(want: str, shown: str) -> bool:
    """True when the CRM's shown name IS the wanted customer name
    (whitespace/case-insensitive)."""
    return bool(want) and clean_name(want) == clean_name(shown)



def is_name_candidate(want: str, shown: str) -> bool:
    """Guarded fallback for the two ways the CRM name can differ from the
    sheet name, `want` (sheet) vs `shown` (CRM):

      * the CRM holds MORE of it -- 'Blessing' -> 'Blessing Okon' (this is
        what the old whole-word fallback did, so it is kept); or
      * the CRM holds only PART of it -- 'Oladunni folasade Ramat' ->
        'Oladunni folasade' (the real live case). That direction needs the
        CRM fragment to carry at least two words: ONE word of a longer name
        ('John' for 'John Okon') is too weak to move an order on.

    Only a whole-word PREFIX counts (never a substring in the middle, never
    a re-ordering), and find_row() additionally requires the candidate to be
    the ONLY one on screen before it acts on it.
    """
    a, b = clean_name(want), clean_name(shown)
    if not a or not b or a == b:
        return False
    if b.startswith(a + " "):               # CRM holds more than the sheet
        return True
    return a.startswith(b + " ") and len(b.split()) >= 2



def phones_from_hrefs(hrefs) -> list:
    """The phone number(s) a Contact cell carries, from its links.

    The CRM renders the customer's phone as `<a href="tel:...">` and their
    WhatsApp as `<a href="https://wa.me/...">`, and the two values can be
    DIFFERENT numbers, so both are returned (normalised, de-duplicated).
    """
    found: list = []
    for href in hrefs or []:
        text = str(href or "")
        if text.lower().startswith("tel:"):
            value = text[4:]
        elif "wa.me/" in text:
            value = text.split("wa.me/", 1)[1]
        else:
            continue
        value = re.split(r"[?#]", value, 1)[0]
        key = normalize_phone(value)
        if key and key not in found:
            found.append(key)
    return found
