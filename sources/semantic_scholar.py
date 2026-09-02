"""Semantic Scholar Graph API client.

Free API, works without a key at a low rate limit (shared IP pools can hit
429s fairly easily). Get a free key at
https://www.semanticscholar.org/product/api#api-key-form for reliable
daily use -- set config.S2_API_KEY or the S2_API_KEY environment variable.

Used for snowballing: a paper's references (backward), citations
(forward), and recommendations (Semantic Scholar's "related papers",
similar to Google Scholar's "related articles" panel).
"""

import logging
import os
import time
from typing import List, Optional

import requests

import config

log = logging.getLogger(__name__)

GRAPH_BASE = "https://api.semanticscholar.org/graph/v1"
REC_BASE = "https://api.semanticscholar.org/recommendations/v1"

PAPER_FIELDS = "paperId,title,abstract,externalIds,openAccessPdf,year,authors"

_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 5


def _get_api_key() -> str:
    return os.environ.get("S2_API_KEY") or config.S2_API_KEY


def _headers() -> dict:
    api_key = _get_api_key()
    return {"x-api-key": api_key} if api_key else {}


def _get(url: str, params: dict) -> Optional[dict]:
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, headers=_headers(), timeout=config.REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            log.warning("Semantic Scholar request failed: %s", exc)
            return None

        if resp.status_code == 429:
            if attempt < _MAX_RETRIES:
                wait = int(resp.headers.get("Retry-After", _RETRY_DELAY_SECONDS))
                log.info("Semantic Scholar rate-limited, waiting %ds (attempt %d/%d)", wait, attempt + 1, _MAX_RETRIES)
                time.sleep(wait)
                continue
            log.warning("Semantic Scholar rate-limited, giving up for this request")
            return None

        if not resp.ok:
            log.warning("Semantic Scholar request to %s failed: %s %s", url, resp.status_code, resp.text[:200])
            return None

        try:
            return resp.json()
        except ValueError:
            log.warning("Semantic Scholar response from %s was not valid JSON", url)
            return None
    return None


def get_paper(paper_ref: str) -> Optional[dict]:
    """paper_ref: 'DOI:10.xxxx/yyy' or 'ARXIV:1202.4527' or a raw S2 paperId."""
    return _get(f"{GRAPH_BASE}/paper/{paper_ref}", {"fields": PAPER_FIELDS})


def get_paper_by_title(title: str) -> Optional[dict]:
    """Fallback when a DOI/arXiv lookup 404s (common for institutional-
    repository DOIs, e.g. ProQuest/BePress dissertation DOIs, that S2 hasn't
    indexed under that identifier). Returns the top match only if its title
    is a close match, to avoid silently snowballing from the wrong paper.
    """
    data = _get(
        f"{GRAPH_BASE}/paper/search",
        {"query": title, "fields": PAPER_FIELDS, "limit": 1},
    )
    if not data:
        return None
    results = data.get("data") or []
    if not results:
        return None
    candidate = results[0]
    if relevance_title_match(title, candidate.get("title") or ""):
        return candidate
    return None


def relevance_title_match(a: str, b: str) -> bool:
    norm = lambda s: "".join(c.lower() for c in s if c.isalnum())
    a, b = norm(a), norm(b)
    return bool(a) and bool(b) and (a in b or b in a)


def get_references(paper_id: str, limit: int = None) -> List[dict]:
    limit = limit or config.SNOWBALL_MAX_PER_RELATION
    data = _get(
        f"{GRAPH_BASE}/paper/{paper_id}/references",
        {"fields": PAPER_FIELDS, "limit": limit},
    )
    if not data:
        return []
    return [d["citedPaper"] for d in (data.get("data") or []) if d.get("citedPaper")]


def get_citations(paper_id: str, limit: int = None) -> List[dict]:
    limit = limit or config.SNOWBALL_MAX_PER_RELATION
    data = _get(
        f"{GRAPH_BASE}/paper/{paper_id}/citations",
        {"fields": PAPER_FIELDS, "limit": limit},
    )
    if not data:
        return []
    return [d["citingPaper"] for d in (data.get("data") or []) if d.get("citingPaper")]


def get_recommendations(paper_id: str, limit: int = None) -> List[dict]:
    limit = limit or config.SNOWBALL_MAX_PER_RELATION
    data = _get(
        f"{REC_BASE}/papers/forpaper/{paper_id}",
        {"fields": PAPER_FIELDS, "limit": limit},
    )
    if not data:
        return []
    return data.get("recommendedPapers") or []
