"""GDELT DOC 2.0 API client -- the newspaper/magazine source.

Free, no API key. GDELT indexes metadata (not full text) for news
coverage from tens of thousands of outlets worldwide, in dozens of
languages, updated continuously -- a reasonable free substitute for a
paid news API for this project's purposes.

GDELT gives no abstract/snippet (only title + URL + date + domain), and
no PDF link -- so records from this source are always metadata + link
only, consistent with the project's policy of not attempting to
download or scrape paywalled/copyrighted news content. The relevance
gate therefore judges these records on title text alone, which is
weaker than for academic sources; treat this source as lower-precision
by design (see literature_review's News/Magazine Coverage section).

GDELT asks callers to limit requests to one per 5 seconds; this module
sleeps before each request to respect that without needing a key.
"""

import logging
import time
from typing import List

import requests

import config
from storage import Record

log = logging.getLogger(__name__)

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT's sourcelang: operator takes the full English language name,
# lowercase -- not an ISO code.
_SOURCELANG = {"en": "english", "es": "spanish", "fr": "french"}

_MIN_REQUEST_INTERVAL_SECONDS = 5
_last_request_time = 0.0
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 10

# GDELT's DOC API rejects queries above a fairly short length ("query was
# too short or too long"); config.TOPIC_TERMS/EXPERIENCE_TERMS (24 OR'd
# phrases once combined) is well over that limit. Use a compact, curated
# subset of the most discriminative terms instead -- lower recall than the
# academic sources, but that trade-off is already documented above.
_TOPIC_TERMS = {
    "en": ["standardized test", "standardized testing", "high-stakes testing"],
    "es": ["prueba estandarizada", "pruebas estandarizadas"],
    "fr": ["test standardise", "examens standardises"],
}
_EXPERIENCE_TERMS = {
    "en": ["test anxiety", "test-taker", "student experience"],
    "es": ["ansiedad", "experiencia estudiantil"],
    "fr": ["anxiete", "experience etudiante"],
}


def _build_query(language: str) -> str:
    topic = " OR ".join(f'"{t}"' for t in _TOPIC_TERMS[language])
    experience = " OR ".join(f'"{t}"' for t in _EXPERIENCE_TERMS[language])
    query = f"({topic}) ({experience})"
    lang = _SOURCELANG.get(language)
    if lang:
        query += f" sourcelang:{lang}"
    return query


def _throttle() -> None:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
    _last_request_time = time.time()


def _parse_date(seendate: str) -> str:
    # e.g. "20260624T201500Z" -> "2026-06-24"
    if len(seendate) >= 8 and seendate[:8].isdigit():
        return f"{seendate[0:4]}-{seendate[4:6]}-{seendate[6:8]}"
    return seendate


def search(language: str, max_results: int = None) -> List[Record]:
    max_results = max_results or config.RESULTS_PER_QUERY
    query = _build_query(language)
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": max_results,
        "format": "json",
        "sort": "hybridrel",
    }

    data = None
    for attempt in range(_MAX_RETRIES + 1):
        _throttle()
        try:
            resp = requests.get(API_URL, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("GDELT request failed for query %r: %s", query, exc)
            return []

        if resp.status_code == 429:
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_SECONDS * (attempt + 1)
                log.info("GDELT rate-limited, waiting %ds (attempt %d/%d)", wait, attempt + 1, _MAX_RETRIES)
                time.sleep(wait)
                continue
            log.warning("GDELT rate-limited, giving up for this query.")
            return []

        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("GDELT request failed for query %r: %s", query, exc)
            return []

        try:
            data = resp.json()
        except ValueError:
            # GDELT returns plain-text rate-limit notices (not JSON) when
            # callers exceed its courtesy limit -- treat as empty, not a crash.
            log.warning("GDELT response for query %r was not valid JSON (likely rate-limited): %s", query, resp.text[:200])
            return []
        break

    if data is None:
        return []

    records = []
    for article in data.get("articles", []):
        url = article.get("url")
        if not url:
            continue
        records.append(
            Record(
                uid=f"gdelt:{url}",
                source="gdelt",
                language=language,
                query=query,
                title=(article.get("title") or "").strip(),
                authors="",
                date_published=_parse_date(article.get("seendate") or ""),
                description="",
                record_url=url,
                pdf_url=None,
                record_type=f"news ({article.get('domain', '')})",
            )
        )
    return records
