"""SHARE (share.osf.io) search client.

Free, no API key. SHARE is an Elasticsearch-backed aggregator that indexes
OSF's preprint servers (PsyArXiv, EdArXiv, SocArXiv, ...) plus hundreds of
other repositories worldwide, which makes it the main source of
multilingual coverage for this project right now.

Docs: https://share.osf.io/api/v2/search/creativeworks/_search accepts a
raw Elasticsearch query body.
"""

import logging
from typing import List, Optional

import requests

import config
from storage import Record

log = logging.getLogger(__name__)

API_URL = "https://share.osf.io/api/v2/search/creativeworks/_search"


def _pick_record_url(identifiers: List[str]) -> str:
    for ident in identifiers:
        if "doi.org" in ident:
            return ident
    return identifiers[0] if identifiers else ""


def _pick_pdf_url(identifiers: List[str], hosts: List[str]) -> Optional[str]:
    # OSF-hosted items (preprints, projects, registrations) can usually be
    # downloaded by appending /download to their short OSF URL.
    if not any(h.upper() == "OSF" or "osf" in h.lower() for h in hosts):
        return None
    for ident in identifiers:
        if "osf.io/" in ident:
            base = ident.rstrip("/")
            if not base.endswith("/download"):
                base += "/download"
            return base
    return None


def _build_query(language: str) -> str:
    topic = " OR ".join(f'"{t}"' for t in config.TOPIC_TERMS[language])
    experience = " OR ".join(f'"{t}"' for t in config.EXPERIENCE_TERMS[language])
    return f"({topic}) AND ({experience})"


def search(language: str, max_results: int = None) -> List[Record]:
    max_results = max_results or config.RESULTS_PER_QUERY
    query = _build_query(language)
    body = {
        "query": {"query_string": {"query": query}},
        "size": max_results,
    }
    try:
        resp = requests.post(API_URL, json=body, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("SHARE request failed for query %r: %s", query, exc)
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        log.warning("SHARE response for query %r was not valid JSON: %s", query, exc)
        return []

    records = []
    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        uid = hit.get("_id")
        if not uid:
            continue
        identifiers = src.get("identifiers", []) or []
        hosts = src.get("hosts", []) or src.get("sources", []) or []
        contributors = src.get("lists", {}).get("contributors", []) or []
        authors = ", ".join(c.get("cited_as", "") for c in contributors if c.get("cited_as"))
        types = src.get("types", []) or []

        records.append(
            Record(
                uid=f"share:{uid}",
                source="share",
                language=language,
                query=query,
                title=(src.get("title") or "").strip(),
                authors=authors,
                date_published=src.get("date_published") or src.get("date") or "",
                description=(src.get("description") or "").strip(),
                record_url=_pick_record_url(identifiers),
                pdf_url=_pick_pdf_url(identifiers, hosts),
                record_type=",".join(types) if types else "unknown",
            )
        )
    return records
