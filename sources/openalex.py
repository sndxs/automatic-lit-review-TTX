"""OpenAlex search client -- the "white literature" (peer-reviewed journal)
source, as distinct from the preprint/repository sources (SHARE, arXiv).

Free, no API key (a `mailto` contact email gets you into OpenAlex's
"polite pool" for better rate limits -- see config.OPENALEX_MAILTO).
OpenAlex aggregates Crossref, PubMed, repositories, and more, so results
are restricted to type=article with a real journal host to keep this
source's category meaning ("formally published, peer-reviewed") distinct
from the preprints already covered elsewhere.
"""

import logging
from typing import List, Optional

import requests

import config
from storage import Record

log = logging.getLogger(__name__)

API_URL = "https://api.openalex.org/works"

# OpenAlex language filter uses ISO 639-1 codes, matching our language keys.
_LANGUAGE_FILTER = {"en": "en", "es": "es", "fr": "fr"}


def _build_query(language: str) -> str:
    topic = " OR ".join(f'"{t}"' for t in config.TOPIC_TERMS[language])
    experience = " OR ".join(f'"{t}"' for t in config.EXPERIENCE_TERMS[language])
    return f"({topic}) AND ({experience})"


def _reconstruct_abstract(inverted_index: Optional[dict]) -> str:
    if not inverted_index:
        return ""
    positions = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def search(language: str, max_results: int = None) -> List[Record]:
    max_results = max_results or config.RESULTS_PER_QUERY
    query = _build_query(language)
    lang_code = _LANGUAGE_FILTER.get(language)
    params = {
        "search": query,
        "per-page": max_results,
        "mailto": config.OPENALEX_MAILTO,
        "filter": f"type:article,language:{lang_code}" if lang_code else "type:article",
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("OpenAlex request failed for query %r: %s", query, exc)
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        log.warning("OpenAlex response for query %r was not valid JSON: %s", query, exc)
        return []

    records = []
    for work in data.get("results", []):
        work_id = work.get("id")
        if not work_id:
            continue
        doi = work.get("doi") or ""
        record_url = doi or work_id
        authors = ", ".join(
            (a.get("author") or {}).get("display_name", "")
            for a in (work.get("authorships") or [])
            if (a.get("author") or {}).get("display_name")
        )
        best_oa = work.get("best_oa_location") or {}
        pdf_url = best_oa.get("pdf_url") if (work.get("open_access") or {}).get("is_oa") else None
        venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""

        records.append(
            Record(
                uid=f"openalex:{work_id}",
                source="openalex",
                language=language,
                query=query,
                title=(work.get("title") or work.get("display_name") or "").strip(),
                authors=authors,
                date_published=work.get("publication_date") or str(work.get("publication_year") or ""),
                description=_reconstruct_abstract(work.get("abstract_inverted_index")),
                record_url=record_url,
                pdf_url=pdf_url,
                record_type=f"journal-article ({venue})" if venue else "journal-article",
            )
        )
    return records
