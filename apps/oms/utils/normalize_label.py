# ==============================================================
# ================================================================
#  HELPER: Label Normalizer (used by all extractors)
# ================================================================
# Place this at the top of each extractor file that needs it,
# or import from a shared utility module.
# ================================================================
# ==============================================================


# Shared utility — add to each extractor file or a shared utils.py

import re

def normalize_label(text: str) -> str:
    '''
    Normalize a label for comparison.
    Lowercases, collapses whitespace, strips punctuation at edges.
    Never modifies extracted VALUES — only used for label matching.
    '''
    return re.sub(r'\\s+', ' ', text.lower().strip(' :*-_'))   