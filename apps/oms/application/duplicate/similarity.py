from __future__ import annotations

import re


def levenshtein_distance(s1: str, s2: str) -> int:
    '''
    Compute the Levenshtein edit distance between two strings.
    Pure Python — no external dependencies.

    Time:  O(len(s1) * len(s2))
    Space: O(min(len(s1), len(s2)))

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Integer edit distance (0 = identical, higher = more different).
    '''
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)

    # Use the shorter string as rows to save memory
    if len(s1) > len(s2):
        s1, s2 = s2, s1

    prev = list(range(len(s1) + 1))

    for j, ch2 in enumerate(s2, 1):
        curr = [j]
        for i, ch1 in enumerate(s1, 1):
            cost = 0 if ch1 == ch2 else 1
            curr.append(min(
                prev[i] + 1,      # deletion
                curr[i - 1] + 1,  # insertion
                prev[i - 1] + cost  # substitution
            ))
        prev = curr

    return prev[-1]


def levenshtein_ratio(s1: str, s2: str) -> float:
    '''
    Similarity ratio based on Levenshtein distance.
    Returns 1.0 for identical strings, 0.0 for completely different.

    Formula: 1 - (distance / max_possible_distance)

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Float in range [0.0, 1.0].
    '''
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    distance    = levenshtein_distance(s1, s2)
    max_dist    = max(len(s1), len(s2))
    return round(1.0 - (distance / max_dist), 4)


def normalize_for_comparison(text: str) -> str:
    '''
    Normalize text for comparison purposes ONLY.
    This normalization is NEVER applied to stored data.

    Steps:
        - Lowercase
        - Strip leading/trailing whitespace
        - Collapse multiple spaces
        - Remove common honorifics and prefixes
        - Remove punctuation except hyphens (compound names)
    '''
    if not text:
        return ""

    t = text.lower().strip()

    # Remove honorifics
    honorifics = r'^(mr\.?|mrs\.?|miss\.?|ms\.?|dr\.?|prof\.?|engr\.?|'
    honorifics += r'alhaji\.?|alhaja\.?|chief\.?|barr\.?\s+)'
    t = re.sub(honorifics, '', t, flags=re.IGNORECASE).strip()

    # Remove punctuation except hyphens
    t = re.sub(r'[^\w\s\-]', '', t)

    # Collapse whitespace
    t = re.sub(r'\s+', ' ', t).strip()

    return t


def phone_normalize(raw: str) -> str:
    '''
    Normalize a phone number to digits only starting from local format.
    Used only for comparison — never applied to stored data.

    Examples:
        "+2348031234567" → "08031234567"
        "2348031234567"  → "08031234567"
        "08031234567"    → "08031234567"
        "08031234567.0"  → "08031234567"   (Excel float artifact)
    '''
    if not raw:
        return ""

    # Remove everything except digits
    digits = re.sub(r'[^\d]', '', str(raw).strip())

    # Remove country code prefix
    if digits.startswith("234") and len(digits) == 13:
        return "0" + digits[3:]

    return digits
