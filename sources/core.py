"""CORE.ac.uk v3 API client (optional, requires a free API key).

CORE aggregates open-access repositories and journals worldwide with
strong multilingual full-text coverage. Sign up for a free key at
https://core.ac.uk/services/api and either set config.CORE_API_KEY or
the CORE_API_KEY environment variable, then set config.ENABLE_CORE = True.

Disabled by default so the rest of the pipeline runs with zero setup.
"""

import logging
import os
from typing import List, Optional

import requests

import config
from storage import Record

log = logging.getLogger(__name__)

API_URL = "https://api.core.ac.uk/v3/search/works"


def _get_api_key() -> str:
    return os.environ.get("CORE_API_KEY") or config.CORE_API_KEY


def _build_query(language: str) -> str:
    topic = " OR ".join(f'"{t}"' for t in config.TOPIC_TERMS[language])
    experience = " OR ".join(f'"{t}"' for t in config.EXPERIENCE_TERMS[language])
    return f"({topic}) AND ({experience})"


def search(language: str, max_results: int = None) -> List[Record]:
    api_key = _get_api_key()
    if not api_key:
        log.warning("CORE search skipped: no API key configured (see sources/core.py docstring)")
        return []

    max_results = max_results or config.RESULTS_PER_QUERY
    query = _build_query(language)
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {"q": query, "limit": max_results}

    try:
        resp = requests.post(
            API_URL, json=body, headers=headers, timeout=config.REQUEST_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("CORE request failed for query %r: %s", query, exc)
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        log.warning("CORE response for query %r was not valid JSON: %s", query, exc)
        return []

    records = []
    for item in data.get("results", []):
        core_id = item.get("id")
        if core_id is None:
            continue
        authors = ", ".join(
            a.get("name", "") for a in (item.get("authors") or []) if a.get("name")
        )
        record_url = item.get("doi")
        if record_url and not record_url.startswith("http"):
            record_url = f"https://doi.org/{record_url}"
        if not record_url:
            record_url = item.get("sourceFulltextUrls", [None])[0] or ""

        records.append(
            Record(
                uid=f"core:{core_id}",
                source="core",
                language=language,
                query=query,
                title=(item.get("title") or "").strip(),
                authors=authors,
                date_published=item.get("publishedDate") or str(item.get("yearPublished") or ""),
                description=(item.get("abstract") or "").strip(),
                record_url=record_url,
                pdf_url=item.get("downloadUrl"),
                record_type="paper",
            )
        )
    return records
