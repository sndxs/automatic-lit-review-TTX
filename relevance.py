"""Client-side relevance gate, applied uniformly to every candidate record
regardless of source.

Different search backends rank and match text differently (some enforce
boolean AND server-side, some just score by partial overlap), so this is
the one place precision is actually enforced: a record is kept only if its
title + description contains at least one phrase from
config.RELEVANCE_KEYWORDS, compared case- and accent-insensitively.
"""

import unicodedata

import config


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


_NORMALIZED_KEYWORDS = [normalize(k) for k in config.RELEVANCE_KEYWORDS]


def is_relevant(title: str, description: str) -> bool:
    haystack = normalize(f"{title} {description}")
    return any(kw in haystack for kw in _NORMALIZED_KEYWORDS)
