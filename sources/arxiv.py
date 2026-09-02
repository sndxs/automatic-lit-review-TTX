"""arXiv Atom API client.

Free, no API key. English-only and skewed toward CS/physics/math, so
yield on this topic will be low, but it's cheap to query and occasionally
turns up psychometrics or education-technology papers.
"""

import logging
import xml.etree.ElementTree as ET
from typing import List

import requests

import config
from storage import Record

log = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _build_query(language: str) -> str:
    topic = " OR ".join(f'all:"{t}"' for t in config.TOPIC_TERMS[language])
    experience = " OR ".join(f'all:"{t}"' for t in config.EXPERIENCE_TERMS[language])
    return f"({topic}) AND ({experience})"


def search(language: str, max_results: int = None) -> List[Record]:
    if language != "en":
        # arXiv has no non-English content worth searching.
        return []

    max_results = max_results or config.RESULTS_PER_QUERY
    query = _build_query(language)
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("arXiv request failed for query %r: %s", query, exc)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        log.warning("arXiv response for query %r was not valid XML: %s", query, exc)
        return []

    records = []
    for entry in root.findall("atom:entry", NS):
        arxiv_id = (entry.findtext("atom:id", default="", namespaces=NS) or "").strip()
        if not arxiv_id:
            continue
        title = " ".join((entry.findtext("atom:title", default="", namespaces=NS) or "").split())
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=NS) or "").split())
        published = (entry.findtext("atom:published", default="", namespaces=NS) or "").strip()
        authors = ", ".join(
            (a.findtext("atom:name", default="", namespaces=NS) or "").strip()
            for a in entry.findall("atom:author", NS)
        )

        pdf_url = None
        for link in entry.findall("atom:link", NS):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")
                break

        records.append(
            Record(
                uid=f"arxiv:{arxiv_id}",
                source="arxiv",
                language=language,
                query=query,
                title=title,
                authors=authors,
                date_published=published,
                description=summary,
                record_url=arxiv_id,
                pdf_url=pdf_url,
                record_type="preprint",
            )
        )
    return records
