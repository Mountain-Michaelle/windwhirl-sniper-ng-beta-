"""
product_matcher.py

Turns a messy, free-text Excel product description into zero or one
sellable CRM product name, the way an experienced staff member reads it:
strip promotional freebies, combine multiple purchased-item fragments back
into a single product line (the live CRM form has exactly one product
slot per order), and fuzzy-match whatever's left against the real CRM
product list.

Nothing here talks to the browser. It's pure text logic so it can be
unit-tested without Playwright.

------------------------------------------------------------------------
CHANGELOG (bug fix, Aug 2026) -- additive, backward compatible:

1. FIX: _promo_similarity()'s fuzzy fallback was comparing entire
   strings character-by-character (SequenceMatcher over the whole
   candidate vs the whole promo phrase). Two misspelled promo entries --
   "1 sadore collagen face mask" / "2 sadore collagen face mask" ("sadore"
   instead of the real product's "sadoer") -- share a long literal
   character prefix with genuine purchased products like "2 Sadoer
   Collagen Creams". That pushed whole-string similarity to ~0.80-0.82,
   over the is_promotional_item() threshold, even though the actual
   distinguishing word ("creams" vs "face mask") is completely
   different. Real orders were silently having their product line
   discarded as a "freebie" and then failing with "no sellable product
   found in Excel product text".

   Fixed by replacing the fuzzy fallback with _promo_token_overlap(): a
   token-level (word-level) comparison, the same philosophy
   score_product_candidate() already uses elsewhere in this file for
   real product scoring. It requires the actual differentiating word(s)
   to agree, not just the string-as-a-whole to look similar by shared
   prefix. The exact-substring shortcut in _promo_similarity() (used for
   precise phrases like "part delivery payment") is untouched.

   Per-word fuzzy tolerance is tuned to 0.85, chosen to sit strictly
   between the two real cases that motivated this fix: "delivary"/
   "delivery" (0.875 -- a genuine typo that should still count as a
   promo match) and "sadoer"/"sadore" (0.833 -- the actual bug, must NOT
   count as a match).

   Verified: all existing PROMO_PHRASES entries still detected as
   promotional: all KNOWN_ALIASES real-product keys -- zero false
   positives; typo variants of promo phrases still caught.
------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False


# ==============================================================================
# CONFIDENCE THRESHOLDS  (business logic spec, section 18)
# ==============================================================================
HIGH_CONFIDENCE = 0.85
MEDIUM_CONFIDENCE = 0.60
# NEW -- business rule: scores in [LOW_UPLOAD_CONFIDENCE, MEDIUM_CONFIDENCE)
# are no longer hard-rejected. They're still uploaded to the CRM (a real
# matched_product is set), but flagged via MatchResult.needs_manual_review
# so a human can double-check the guess on the live dashboard afterward.
LOW_UPLOAD_CONFIDENCE = 0.30
# below LOW_UPLOAD_CONFIDENCE => reject, log failure, never guess (unchanged)


# ==============================================================================
# PROMOTIONAL / NON-SELLABLE ITEMS  (business logic spec, section 6)
# ==============================================================================
# Phrases here are never a real CRM product. Anything that normalizes to one
# of these (or is a very close fuzzy match to one) is dropped before matching
# ever starts.
PROMO_PHRASES = [
    "free gift",
    "part delivery payment",
    "part delivery payment(#2000)",
    "free soap",
    "free sample",
    "hand cream",
    "scar cream",
    "scar repair cream",
    "face towel",
    "free scar repair cream",
    "1 free scar repair cream",
    "2 free scar repair cream",
    "free spa facial mask",
    "free doorstep delivery",
    "free doorstep deliver",
    "2 free spa facial mask",
    "2 free spa facial masks",
    "1 free spa facial mask",
    "sadore collagen face mask",
    "1 sadore collagen face mask",
    "2 sadore collagen face mask",
    "2 free spa facial masks",
    "2 free scar repair creams",
    "2 free scar repair cream",
    "free collagen hand cream",
    "1 free collagen hand cream",
    "1 free collagen hand creams",
    "2 free collagen hand cream",
    "1 free collagen hand creams",  
    "2free face mask — ₦29,500",
    "hand cream and scar repair cream for — ₦28,500",
    "— ₦28,500",
    "with 2free face mask",
    "2free face mask",
    "part delivery payment(#2000)",
    "free doorstep delivery — ₦35,000",  
    "bonus",
    "promo",
    "with",
    "sample",
    "gift",
    "freebie",
    "free spa"
]

# ------------------------------------------------------------------------------
# Inline parenthetical price stripping
# ------------------------------------------------------------------------------
# Real Excel cells sometimes carry a per-item price INSIDE the string, not
# just glued to the end -- e.g. "1 Sadoer Collagen Serum (#19,500) + part
# delivery payment(#2000) -- N21,500". Line-item splitting runs on "+" AND
# ",", so the comma inside "(#19,500)" was being read as a real item
# separator, tearing one product's price annotation into two garbage
# fragments ("...Serum (#19" and "500)") that then failed to match anything
# and sank the whole order. Stripping any "(...)" that's just a price
# BEFORE splitting removes that comma entirely, the same way
# strip_trailing_price already handles the end-of-string case.
_INLINE_PAREN_PRICE_RE = re.compile(r"\(\s*[\u20a6#]\s*[\d][\d,]*(?:\.\d+)?\s*\)")


def strip_inline_paren_prices(text: str) -> str:
    return _INLINE_PAREN_PRICE_RE.sub(" ", text)


# ------------------------------------------------------------------------------
# Trailing price stripping
# ------------------------------------------------------------------------------
# The exported Product cell has the order's total price glued onto the end,
# e.g. "... Free Collagen Hand Cream — ₦28,500". The comma inside that price
# is indistinguishable from a real item-separator comma once split_line_items
# runs -- "28,500" becomes two fake line items, "28" and "500", the second of
# which then fails to match anything and (per the "reject the whole order on
# any unmatched fragment" rule) sinks an otherwise-fine order. Stripping the
# price suffix before any splitting happens removes that comma entirely.
#
# Matches a dash/em-dash/en-dash or "=" at the END of the string, followed by
# an optional naira sign or "#", then a run of digits with optional comma
# thousands-separators. Anchored to $ so it never touches a mid-string dash
# like the one in "Combo set -(1 serum & 1 Cream)".
_PRICE_SUFFIX_RE = re.compile(r"[-\u2013\u2014=]\s*[\u20a6#]\s*[\d][\d,]*(?:\.\d+)?\s*$")


def strip_trailing_price(text: str) -> str:
    return _PRICE_SUFFIX_RE.sub("", text).strip()


def clean_price_annotations(text: str) -> str:
    """Runs both price strippers in the right order: inline parenthetical
    prices first (they can appear anywhere in the string and their commas
    would otherwise corrupt splitting), then the trailing total-price
    suffix. Single entry point so parse_order_products doesn't need to
    know about both regexes."""
    return strip_trailing_price(strip_inline_paren_prices(text))


# ------------------------------------------------------------------------------
# Known aliases  (business decision, not a text-matching problem)
# ------------------------------------------------------------------------------
# Keys MUST be the exact output of normalize_product_text(<Excel phrase>) --
# lowercase, punctuation stripped, "&"->"and", duplicate words collapsed.
# To add a new one: call normalize_product_text("your Excel phrase") to get
# the key, then set the value to the CRM product's exact label text.
#
# An alias hit skips fuzzy scoring entirely and returns confidence 1.0 --
# so only add a line here once you've actually confirmed the mapping is
# correct. A wrong guess here ships a real order under the wrong product;
# a wrong fuzzy-match guess just gets rejected as low-confidence and lands
# in manual_check for a human to catch. That asymmetry is why these start
# empty/marked GUESS rather than me filling in every one I can pattern-match.
#
# Entries below marked GUESS are my best read of your Excel<->CRM pairing
# discussion -- confirm each one against the live #selpro dropdown before
# trusting it. Entries marked CONFIRMED are near-exact text matches.
KNOWN_ALIASES: dict[str, str] = {
    # -- Advanced Collagen Body Lotion bundle (qty 1/2/3) --------------------
    "1 advanced collagen body lotion": "Sadoer Collagen Combo + Body Lotion",
    "2 advanced collagen body lotions": "Sadoer Collagen Combo + Body Lotion",
    "3 advanced collagen body lotions": "Sadoer Collagen Combo + Body Lotion",
    "collagen body lotion": "Sadoer Collagen Combo + Body Lotion",
    "sadoer collagen face cream and serum + 1advanced collagen body lotion": "Sadoer Collagen Combo + Body Lotion",  
    "1 advanced collagen body lotion + sadoer collagen face cream + sadoer collagen serum": "Sadoer Collagen Combo + Body Lotion",
    "1 advanced collagen body lotion + 1 combo set serum & cream)": "Sadoer Collagen Combo + Body Lotion", 
    "1 combo set serum & cream + 1 avanced collagen body lotion": "Sadoer Collagen Combo + Body Lotion", 
    "1 advanced collagen body lotion + salicylic serum set (face cream and face serum )" : "Sadoer Salicylic Acne Repair Set + Body Lotion NEW",
    "salicylic cumbo set (face cream and face serum )" : "Sadoer Salicylic Acne Repair Set",
    "salicylic cumbo set(face cream and cleanser )" : "Sadoer Salicylic Acne Repair Set",
    # was: "Sadoer collagen face cream and serum + 1 Avanced collagen body lotion "
    "sadoer collagen face cream and serum 1 avanced body lotion": "Sadoer Collagen Combo + Body Lotion",
    "1 combo set serum and cream 1 avanced collagen body lotion":" Sadoer Collagen Combo + Body Lotion",
    "1 advanced salisylic acid": "Sadoer Salicylic Acne Repair Set",
    "2 sadoer collagen creams": "Sadoer Collagen Serum",
    # was: "Sadoer collagen face cream and serum" (capital S)
    "sadoer collagen face cream and serum": "Sadoer Collagen Combo Set",
    
    "face cream and serum": "Sadoer Collagen Combo Set",  # GUESS (misspelling: "Sadoer" vs "Sadeor")
    "combo-set face cream and serum":"Sadoer Collagen Combo Set",
    "sadoer collagen face serum": "Sadoer Collagen Serum",
    "combo-set":"Sadoer Collagen Serum",
    "Collagen body lotion": "Sadoer Collagen Combo + Body Lotion",
    # was: "1 Sadoer collagen serum" (capital S)
    "1 sadoer collagen serum": "Sadoer Collagen Serum",

    # was: "1 combo set -(1 serum & 1 Cream) + 1 Advanced Collagen Body Lotion"
    "1 combo set serum and cream advanced collagen body lotion": "Sadoer Collagen Combo + Body Lotion",

    "1 sadoer collagen face cream": "Sadoer Collagen Serum",
    "1 sadoer collagen cream": "Sadoer Collagen Serum",
    "sadoer collagen cream": "Sadoer Collagen Serum",
    "sadoer collagen face cream": "Sadoer Collagen Serum",  # GUESS (misspelling: "Sadoer" vs "Sadeor")

    "1 cumbo salicylic acid": "Sadoer Salicylic Acne Repair Set + Body Lotion NEW",
    "salicylic acid": "Sadoer Salicylic Acne Repair Set + Body Lotion NEW",  # GUESS (misspelling: "salisylic")

    # was: "1 Combo set -(1 serum & 1 Cream)" -- normalized to the SAME key
    # as the entry below it, so it was dead weight, not just broken; removed
    # rather than duplicated.

    # -- "Combo set" (1 serum & 1 cream), incl. misspellings ------------------
    "1 combo set serum and cream": "Sadoer Collagen Combo Set",
    "1 cumbo set serum and cream": "Sadoer Collagen Combo Set",   # misspelling: "Cumbo"
    "2 combo set": "Sadoer Collagen Combo Set",
    "1sadeor collagen combo set": "Sadoer Collagen Combo Set",   # misspelling: "Sadeor"

    # -- Near-exact text matches ----------------------------------------------
    "sadoer collagen serum": "Sadoer Collagen Serum",  # CONFIRMED (exact text match)

    # -- Oct 2026 ("no sellable product found" recurrences) -------------------
    # These two Excel families failed on every run because promotional-item
    # stripping emptied the line-item list completely (every fragment is in
    # PROMO_PHRASES), so parse_order_products() returned nothing and the row
    # died with "no sellable product found in Excel product text".
    # Keys are the EXACT normalize_product_text() output of the cleaned row
    # text (verified by running the normalizer against the real Excel
    # strings), values are labels confirmed present in the live catalog.
    "collagen body lotion hand cream and scar repair for":
        "Sadoer Collagen Combo + Body Lotion",
    "combo set face cream and serum with 2free mask":
        "Sadoer Collagen Combo Set",
}

# Business rule: Excel descriptions often start with a leading quantity
# number ("1 Advanced...", "2 Cumbo..."). Extract it; default to 1 if
# there's no leading number. Runs on RAW text (before normalization) so
# it sees the number exactly as written.
_LEADING_QTY_RE = re.compile(r"^\s*(\d+)\b")

def extract_quantity(raw_line_item: str) -> int:
    if not raw_line_item:
        return 1
    m = _LEADING_QTY_RE.match(raw_line_item.strip())
    if m:
        try:
            qty = int(m.group(1))
            return qty if qty > 0 else 1
        except ValueError:
            return 1
    return 1


def _find_catalog_label(target: str, crm_catalog: list[str]) -> Optional[str]:
    """Case/whitespace-insensitive lookup of an alias's target name against
    the LIVE-scraped catalog, so an alias hit still uses whatever exact
    string is currently in the #selpro dropdown (source of truth) rather
    than whatever was hardcoded in KNOWN_ALIASES -- if the CRM ever renames
    the product, this returns None and match_product falls back to normal
    fuzzy matching instead of submitting a stale/wrong label."""
    target_norm = " ".join(target.split()).lower()
    for name in crm_catalog:
        if " ".join(name.split()).lower() == target_norm:
            return name
    return None


# ------------------------------------------------------------------------------
# Price -> product inference (Oct 2026, "the code should be able to infer the
# correct product to select FROM the price -- e.g. 28,500 is the Sadoer
# Collagen Combo + Body Lotion, 29,500 is the Sadoer Collagen Combo Set")
# ------------------------------------------------------------------------------
# The recurrence is high: the same few package prices always mean the same few
# bundles. This is the LAST resort only -- it is consulted only when the normal
# product pipeline produced nothing sellable at all (after aliases and fuzzy),
# so it can never override a real matcher hit. A dict lookup, so it is O(1)
# and adds no per-row fuzzy cost.
#
# Values are CRM product names; they are resolved against the LIVE catalog via
# _find_catalog_label() before use, so a rename on the CRM makes this return
# None (and the row keeps its old failure) instead of submitting a stale name.
PRICE_TO_PRODUCT: dict[int, str] = {
    28500: "Sadoer Collagen Combo + Body Lotion",
    29500: "Sadoer Collagen Combo Set",
}


def product_for_price(excel_price, crm_catalog: list[str]) -> Optional[MatchResult]:
    """Price -> product fallback. Returns a flagged MatchResult when the Excel
    price is a known package price whose bundle exists in the live catalog,
    else None (caller keeps its normal failure path)."""
    try:
        price = int(round(float(str(excel_price).replace(",", "").replace("₦", "")
                                .replace("#", "").strip())))
    except (TypeError, ValueError):
        return None
    target = PRICE_TO_PRODUCT.get(price)
    if target is None:
        return None
    catalog_label = _find_catalog_label(target, crm_catalog)
    if catalog_label is None:
        return None
    return MatchResult(
        excel_text=f"(inferred from price ₦{price})",
        normalized=normalize_product_text(target),
        matched_product=catalog_label,
        confidence=1.0,
        confidence_band="high",
        reason=(f"no sellable product in the row's text, but the row's price "
                f"{price} maps to {catalog_label!r} -- inferred the product "
                f"from the price (flagged for manual review)"),
        needs_manual_review=True,
    )
# KNOWN_ALIASES itself is an exact-string dict -- fast, and correct as long
# as fragments always get typed/combined in the same order. But the
# business spec explicitly requires tolerating REORDERED words (section 4),
# and two Excel rows describing the same bundle can easily list the pieces
# in a different order ("Combo set + Advanced Collagen Body Lotion" vs
# "Advanced Collagen Body Lotion + Combo set"). Rather than require every
# ordering to be hand-added as its own dict entry, build a second lookup
# keyed by a SORTED word signature of each alias key -- order-independent,
# but still exact on which words (and how many of each) are present, so it
# doesn't get looser than a real alias should be. Only consulted when the
# direct exact-string lookup misses; every existing KNOWN_ALIASES entry is
# untouched.
def _word_signature(normalized_text: str) -> tuple:
    return tuple(sorted(normalized_text.split()))


_ALIAS_SIGNATURES: dict[tuple, str] = {
    _word_signature(key): value for key, value in KNOWN_ALIASES.items()
}


def _lookup_alias(normalized: str) -> Optional[str]:
    """Exact match first (cheap, and unambiguous); falls back to the
    order-insensitive signature match only if that misses."""
    direct = KNOWN_ALIASES.get(normalized)
    if direct is not None:
        return direct
    return _ALIAS_SIGNATURES.get(_word_signature(normalized))


@dataclass
class MatchResult:
    excel_text: str          # the raw candidate line item as it appeared in Excel
    normalized: str          # normalized form used for matching
    matched_product: Optional[str]   # CRM product name, or None if rejected
    confidence: float
    confidence_band: str     # "high" | "medium" | "flagged" | "low"
    reason: str = ""         # populated when rejected
    reason: str = ""          # (existing line, unchanged)
    quantity: int = 1         # (existing line, unchanged)
    # NEW -- True only for results accepted in the reduced-confidence band
    # [LOW_UPLOAD_CONFIDENCE, MEDIUM_CONFIDENCE). Appended last with a
    # default, so every existing positional/keyword construction of
    # MatchResult elsewhere keeps working unchanged.
    needs_manual_review: bool = False


# ------------------------------------------------------------------------------
# Normalization  (business logic spec, section 5)
# ------------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^\w\s]")
_MULTISPACE_RE = re.compile(r"\s+")


def normalize_product_text(text: str) -> str:
    """lowercase, strip punctuation/brackets, '&' -> 'and', collapse spaces,
    drop duplicate words (keeping first occurrence / word order)."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace("&", " and ")
    t = _PUNCT_RE.sub(" ", t)
    t = _MULTISPACE_RE.sub(" ", t).strip()

    seen = set()
    deduped_words = []
    for word in t.split(" "):
        if word not in seen:
            seen.add(word)
            deduped_words.append(word)
    return " ".join(deduped_words)


# ------------------------------------------------------------------------------
# Splitting multiple purchased items apart  (business logic spec, section 7)
# ------------------------------------------------------------------------------
# Only split on "+" and "," -- NOT on "&"/"and", since real CRM product names
# legitimately contain "&" (e.g. "Whitening Face & Body Combo"). Splitting on
# those would shred a single product into two garbage fragments.
_SPLIT_RE = re.compile(r"[+,]")


def split_line_items(raw_excel_product_text: str) -> list[str]:
    if not raw_excel_product_text:
        return []
    parts = _SPLIT_RE.split(raw_excel_product_text)
    return [p.strip() for p in parts if p.strip()]


# ------------------------------------------------------------------------------
# Promotional item filtering  (business logic spec, section 6)
# ------------------------------------------------------------------------------
def _promo_similarity(normalized_candidate: str) -> float:
    """Highest similarity between the candidate and any known promo phrase.

    Exact-substring shortcut (unchanged): if a promo phrase is literally
    contained in the candidate, that's precise and reliable -- e.g.
    "part delivery payment" appearing verbatim inside a longer string is
    unambiguously the promo item, so this returns 1.0 immediately, no
    fuzzy scoring needed.

    CHANGED (Aug 2026 bug fix) -- the FALLBACK (used only when no phrase
    is an exact substring) no longer runs SequenceMatcher over the two
    WHOLE strings character-by-character. That was producing false
    positives on real products: e.g. "2 Sadoer Collagen Creams" (an
    actual purchased product) scored 0.824 against the promo phrase
    "2 sadore collagen face mask" purely because both strings happen to
    share a long literal character prefix ("2 sado_e collagen ") -- even
    though the one word that actually distinguishes them ("creams" vs
    "face mask") is completely different. That false positive was
    silently discarding real orders as promotional and sinking them with
    "no sellable product found".

    The fallback now uses _promo_token_overlap() instead: a token-level
    comparison (with light per-word fuzzy tolerance for genuine
    misspellings, e.g. "deliver"/"delivery") that requires the actual
    distinguishing words to agree, not just the string-as-a-whole to
    look similar by shared prefix/suffix. Same 0.80 default threshold in
    is_promotional_item() -- only the metric feeding it changed.
    """
    best = 0.0
    for phrase in PROMO_PHRASES:
        if phrase in normalized_candidate:
            return 1.0
        best = max(best, _promo_token_overlap(normalized_candidate, phrase))
    return best


def _promo_token_overlap(normalized_candidate: str, phrase: str) -> float:
    """Token-level (word-level) similarity between a candidate string and
    one promo phrase -- the fuzzy fallback used by _promo_similarity()
    only when no phrase is an exact substring of the candidate.

    Computes, in both directions, what fraction of one side's words are
    "explained by" the other side (exact word match, or a close per-word
    spelling variant at ratio >= 0.85 -- chosen to sit strictly between
    the two real cases that motivated this: "delivary"/"delivery"
    (0.875 -- a genuine typo that should still count as a match) and
    "sadoer"/"sadore" (0.833 -- the actual bug, must NOT count as a
    match, since "sadoer" is your real product's name and "sadore" is a
    misspelled promo-phrase entry for something else entirely). That gap
    is what lets this stay tight enough to still catch genuine promo
    typos without being loose enough to equate genuinely different words
    the way raw whole-string character similarity was). Returns the
    MINIMUM of the two directions, not the average -- a short candidate
    matching only part of a longer phrase (or vice versa) should not
    score as a strong promotional match either direction; both sides
    need to substantially agree.
    """
    cand_tokens = normalized_candidate.split()
    phrase_tokens = phrase.split()
    if not cand_tokens or not phrase_tokens:
        return 0.0

    def _fraction_explained(tokens_a, tokens_b) -> float:
        matched = 0
        for tok_a in tokens_a:
            if tok_a in tokens_b:
                matched += 1
                continue
            if any(SequenceMatcher(None, tok_a, tok_b).ratio() >= 0.85 for tok_b in tokens_b):
                matched += 1
        return matched / len(tokens_a)

    forward = _fraction_explained(cand_tokens, phrase_tokens)
    backward = _fraction_explained(phrase_tokens, cand_tokens)
    return min(forward, backward)


def is_promotional_item(raw_line_item: str, threshold: float = 0.80) -> bool:
    normalized = normalize_product_text(raw_line_item)
    return _promo_similarity(normalized) >= threshold


def strip_promotional_items(line_items: Iterable[str]) -> list[str]:
    return [item for item in line_items if not is_promotional_item(item)]


# ------------------------------------------------------------------------------
# Matching engine  (business logic spec, section 4)
# ------------------------------------------------------------------------------
def _token_overlap_score(candidate_norm: str, catalog_norm: str) -> float:
    """Containment-oriented score: how much of the (usually shorter) Excel
    description's meaning is present in the CRM product name. Falls back to
    per-token fuzzy matching for spelling variations / abbreviations
    (e.g. 'gluta' ~ 'glutathione')."""
    cand_tokens = candidate_norm.split()
    cat_tokens = set(catalog_norm.split())
    if not cand_tokens:
        return 0.0

    matched = 0.0
    for tok in cand_tokens:
        if tok in cat_tokens:
            matched += 1.0
            continue
        # abbreviation / spelling-variation tolerance
        best_tok_score = max(
            (SequenceMatcher(None, tok, cat_tok).ratio() for cat_tok in cat_tokens),
            default=0.0,
        )
        prefix_bonus = any(
            len(tok) >= 4 and (cat_tok.startswith(tok) or tok.startswith(cat_tok))
            for cat_tok in cat_tokens
        )
        if prefix_bonus:
            best_tok_score = max(best_tok_score, 0.85)
        if best_tok_score >= 0.6:
            matched += best_tok_score
    return matched / len(cand_tokens)


def _full_string_score(candidate_norm: str, catalog_norm: str) -> float:
    if _HAVE_RAPIDFUZZ:
        # token_set_ratio tolerates reordering, missing/extra words -- exactly
        # the tolerance the business spec asks for.
        return _rf_fuzz.token_set_ratio(candidate_norm, catalog_norm) / 100.0
    return SequenceMatcher(None, candidate_norm, catalog_norm).ratio()


def score_product_candidate(candidate_norm: str, catalog_norm: str) -> float:
    """Blend containment score (good for 'Collagen Combo' -> 'Sadoer Collagen
    Combo Set') with a full-string fuzzy score (catches reordering /
    misspellings), weighted toward containment since Excel descriptions are
    almost always shorter than the real CRM name."""
    containment = _token_overlap_score(candidate_norm, catalog_norm)
    full = _full_string_score(candidate_norm, catalog_norm)
    return 0.7 * containment + 0.3 * full


# NEW -- shared exit point for anything that didn't reach high/confirmed-
# medium confidence. Scores >= LOW_UPLOAD_CONFIDENCE are still uploaded
# (matched_product IS set, so add_products.py proceeds exactly as it
# already does -- it only ever checks `matched_product is None`), but
# flagged with needs_manual_review=True so run_automation.py can log
# these separately for a human to double-check on the live CRM dashboard.
# Anything below LOW_UPLOAD_CONFIDENCE is still a hard rejection, exactly
# as before this change.
def _low_confidence_result(raw_line_item, normalized, best_name, best_score, reason) -> MatchResult:
    if best_score >= LOW_UPLOAD_CONFIDENCE:
        return MatchResult(
            raw_line_item, normalized, best_name, best_score, "flagged",
            reason=f"{reason} -- accepted for upload at reduced confidence "
                   f"(>= {LOW_UPLOAD_CONFIDENCE}), flagged for manual review on the CRM",
            needs_manual_review=True,
        )
    return MatchResult(raw_line_item, normalized, None, best_score, "low", reason=reason)


def match_product(raw_line_item: str, crm_catalog: list[str]) -> MatchResult:
    """Match one already-split, already-filtered line item against the CRM
    product catalog. `crm_catalog` is the list of real, sellable CRM product
    names (exactly as they appear in the #selpro dropdown)."""
    normalized = normalize_product_text(raw_line_item)

    if not normalized:
        return MatchResult(raw_line_item, normalized, None, 0.0, "low",
                            reason="empty after normalization")

    # Check KNOWN_ALIASES (exact, then order-insensitive) before any fuzzy
    # scoring happens.
    alias_target = _lookup_alias(normalized)
    if alias_target is not None:
        catalog_label = _find_catalog_label(alias_target, crm_catalog)
        if catalog_label is not None:
            return MatchResult(raw_line_item, normalized, catalog_label, 1.0, "high",
                                reason=f"matched via known alias -> {catalog_label!r}")
        # alias target isn't in the live catalog right now (renamed/removed) --
        # fall through to normal fuzzy matching rather than silently failing.     

    scored = sorted(
        ((crm_name, score_product_candidate(normalized, normalize_product_text(crm_name)))
         for crm_name in crm_catalog),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not scored:
        return MatchResult(raw_line_item, normalized, None, 0.0, "low",
                            reason="empty CRM catalog")

    best_name, best_score = scored[0]

    if best_score >= HIGH_CONFIDENCE:
        return MatchResult(raw_line_item, normalized, best_name, best_score, "high")

    if best_score >= MEDIUM_CONFIDENCE:
        # Secondary matching pass (business spec section 18): recompute with
        # a different algorithm; only keep the match if it agrees.
        secondary_best_name, secondary_score = max(
            ((name, _full_string_score(normalized, normalize_product_text(name)))
             for name in crm_catalog),
            key=lambda pair: pair[1],
        )
        if secondary_best_name == best_name and secondary_score >= MEDIUM_CONFIDENCE:
            return MatchResult(raw_line_item, normalized, best_name, best_score, "medium")
        # CHANGED -- was a hard rejection; now routes through
        # _low_confidence_result so a score >= LOW_UPLOAD_CONFIDENCE still
        # gets uploaded-but-flagged instead of rejected outright.
        return _low_confidence_result(
            raw_line_item, normalized, best_name, best_score,
            reason=(f"medium-confidence match ({best_name!r}, {best_score:.2f}) "
                    "did not agree with secondary matching pass -- rejected"),
        )

    # CHANGED -- same as above: was a hard rejection, now routes through
    # _low_confidence_result.
    return _low_confidence_result(
        raw_line_item, normalized, best_name, best_score,
        reason=f"low confidence (best candidate {best_name!r} scored {best_score:.2f})",
    )


def parse_order_products(raw_excel_product_text, crm_catalog: list[str]) -> list[MatchResult]:
    """Full pipeline, in the order a human would actually read the text:

    0. Strip inline parenthetical prices ("(#19,500)") and the trailing
       "— ₦28,500"-style total price BEFORE anything else touches the
       text -- otherwise the commas inside either get treated as item
       separators and shred a real product's price annotation into
       garbage fragments (this was happening to genuine products, not
       just freebies -- see clean_price_annotations / strip_inline_paren_prices
       docstrings).
    1. Try matching the WHOLE (price-stripped) string as a single product
       first. Some real CRM products contain "+"/"," in their own name
       (e.g. "Sadoer Collagen Combo + Body Lotion"), so an Excel
       description like "Collagen Combo + Lotion" must NOT be blindly
       split -- it's one bundled product, not two.
    2. Only if the whole string doesn't confidently match anything, split
       on "+"/"," and filter out promotional items -- this handles
       "Collagen Combo + Hand Cream + Free Soap" (three fragments, two of
       which are freebies to discard, one real product left).
    3. If that leaves 2+ real (non-promotional) fragments, they are NOT
       returned as separate MatchResults. The live CRM add_multi_order
       form has exactly ONE #selpro/#pricevar pair per order -- it cannot
       accept more than one product line, no matter how many genuinely
       different items the fragments describe. Returning one MatchResult
       per fragment used to make add_products.py try to fill N product
       fields that don't exist in the DOM, and fail with "expected N
       product field(s)... found 1". Instead, the real fragments are
       joined back into ONE combined string and matched as a single
       product. If the combined text still doesn't confidently match
       anything, this returns ONE clear low-confidence (or now,
       flagged-for-review) result naming which fragments were involved,
       instead of silently trying to submit fragments the form can't
       accept.
    """
    # pandas hands back NaN (a float) for an empty Excel cell, not "" --
    # isinstance check first so that never reaches .strip() and crashes.
    if not isinstance(raw_excel_product_text, str) or not raw_excel_product_text.strip():
        return []

    cleaned_text = clean_price_annotations(raw_excel_product_text)
    if not cleaned_text:
        return []

    whole_match = match_product(cleaned_text, crm_catalog)
    whole_match.quantity = extract_quantity(cleaned_text)
    if whole_match.matched_product is not None and whole_match.confidence_band == "high":
        return [whole_match]

    line_items = split_line_items(cleaned_text)
    sellable_items = strip_promotional_items(line_items)

    if not sellable_items:
        # FIX (Oct 2026, "no sellable product found" recurrences): when EVERY
        # fragment of the row is promotional-looking (e.g. "Collagen body
        # lotion hand cream and scar repair cream for — ₦28,500", whose whole
        # tail is a promo phrase), stripping used to leave nothing and this
        # returned [] -- the row died as "no sellable product found" even
        # though the WHOLE cleaned row text was a perfectly matchable string.
        # Try the whole cleaned text as a single product first, exactly like
        # the whole-string attempt above, and only give up if that also
        # matches nothing. This keeps the old rejection for genuinely
        # unusable rows while rescuing real orders the promo filter
        # over-stripped.
        whole_only = match_product(cleaned_text, crm_catalog)
        whole_only.quantity = extract_quantity(cleaned_text)
        if whole_only.matched_product is not None:
            return [whole_only]
        return []

    if len(sellable_items) == 1:
        single = match_product(sellable_items[0], crm_catalog)
        single.quantity = extract_quantity(sellable_items[0])
        return [single]

    # 2+ real fragments -- the live form can only take ONE product line,
    # so combine them and match once instead of once-per-fragment.
    combined_text = " ".join(sellable_items)
    combined_match = match_product(combined_text, crm_catalog)
    combined_match.excel_text = " + ".join(sellable_items)  # keep the readable original for logs
    combined_match.quantity = extract_quantity(sellable_items[0])
    if combined_match.matched_product is None:
        combined_match.reason = (
            f"order bundles {len(sellable_items)} distinct-looking items "
            f"({sellable_items}) into a single product line, and neither the "
            f"individual fragments nor the combined text matched a CRM product "
            f"confidently -- needs manual review (last reason: {combined_match.reason})"
        )
    return [combined_match]