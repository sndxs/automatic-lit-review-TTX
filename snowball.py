"""Citation snowballing: for newly-found papers, pull their references
(backward snowball), citations (forward snowball), and Semantic Scholar's
recommendations (their equivalent of Google Scholar's "related articles" /
"cited by" panel), via sources/semantic_scholar.py.

Candidates go through the same relevance.is_relevant() gate as every other
source before being kept, since a relevant paper's reference list is often
mostly unrelated (methods textbooks, tangential citations, etc).
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import config
import downloader
import relevance
import storage
from sources import semantic_scholar as s2

log = logging.getLogger(__name__)

RELATIONS = ("reference", "citation", "recommendation")


@dataclass
class SnowballStats:
    new_count: int = 0
    downloaded_count: int = 0
    seen_count: int = 0
    filtered_count: int = 0


def _paper_ref_for_seed(record: storage.Record) -> Optional[str]:
    if record.record_url and "doi.org/" in record.record_url:
        doi = record.record_url.split("doi.org/", 1)[1]
        return f"DOI:{doi}"
    if record.source == "arxiv" and record.uid.startswith("arxiv:"):
        # record.uid embeds arXiv's full abs-page URL, e.g.
        # "arxiv:http://arxiv.org/abs/2410.21033v1" -- take the last path
        # segment and drop the version suffix to get the bare ID.
        raw = record.uid.split("arxiv:", 1)[1].rstrip("/")
        arxiv_id = re.sub(r"v\d+$", "", raw.split("/")[-1])
        return f"ARXIV:{arxiv_id}"
    return None


def _config_seed_records() -> List[storage.Record]:
    """Placeholder Records for config.SEED_PAPERS -- resolved fresh every
    run so forward citations of foundational papers (e.g. the TTX model)
    are tracked even when the keyword search finds nothing new that day.
    """
    records = []
    for entry in config.SEED_PAPERS:
        doi = entry["doi"]
        records.append(
            storage.Record(
                uid=f"seed:doi:{doi}",
                source="config-seed",
                language="en",
                query="config-seed",
                title=entry.get("label", doi),
                authors="",
                date_published="",
                description="",
                record_url=f"https://doi.org/{doi}",
                pdf_url=None,
                record_type="seed",
            )
        )
    return records


def _paper_to_record(paper: dict, language: str, seed_uid: str, relation: str) -> Optional[storage.Record]:
    s2_id = paper.get("paperId")
    doi = (paper.get("externalIds") or {}).get("DOI")
    if s2_id:
        uid = f"s2:{s2_id}"
    elif doi:
        uid = f"s2doi:{doi}"
    else:
        return None

    record_url = f"https://doi.org/{doi}" if doi else f"https://www.semanticscholar.org/paper/{s2_id}"
    authors = ", ".join(a.get("name", "") for a in (paper.get("authors") or []) if a.get("name"))
    pdf_url = (paper.get("openAccessPdf") or {}).get("url")

    return storage.Record(
        uid=uid,
        source=f"snowball-{relation}",
        language=language,
        query=f"snowball<-{seed_uid}",
        title=(paper.get("title") or "").strip(),
        authors=authors,
        date_published=str(paper.get("year") or ""),
        description=(paper.get("abstract") or "").strip(),
        record_url=record_url,
        pdf_url=pdf_url,
        record_type="paper",
    )


def run(conn, seed_records: List[storage.Record]) -> SnowballStats:
    stats = SnowballStats()

    resolvable = [r for r in seed_records if _paper_ref_for_seed(r)]
    # arXiv is low-relevance for this topic (see sources/arxiv.py) -- don't
    # let it crowd out share/CORE seeds, which are more likely to be
    # genuinely on-topic, out of the limited snowball budget.
    resolvable.sort(key=lambda r: r.source == "arxiv")
    keyword_seeds = resolvable[: config.SNOWBALL_MAX_SEEDS]

    # Persistent seeds (config.SEED_PAPERS) are added on top of, not counted
    # against, SNOWBALL_MAX_SEEDS -- there are usually only one or two.
    seeds = _config_seed_records() + keyword_seeds

    if not seeds:
        log.info("Snowball: no seed records with a resolvable DOI/arXiv ID this run.")
        return stats

    log.info("Snowball: expanding %d seed paper(s) via Semantic Scholar.", len(seeds))

    for seed in seeds:
        paper_ref = _paper_ref_for_seed(seed)
        seed_paper = s2.get_paper(paper_ref)
        if not seed_paper or not seed_paper.get("paperId"):
            # Institutional-repository DOIs (ProQuest/BePress dissertations,
            # OSF/Zenodo preprints) are often not indexed under that exact
            # identifier -- fall back to a title search before giving up.
            if seed.title:
                seed_paper = s2.get_paper_by_title(seed.title)
            if not seed_paper or not seed_paper.get("paperId"):
                log.info("Snowball: could not resolve %s (%s) on Semantic Scholar.", seed.uid, paper_ref)
                continue
            log.info("Snowball: resolved %s via title search fallback.", seed.uid)

        paper_id = seed_paper["paperId"]

        if seed.source == "config-seed":
            # Persistent seeds aren't found by keyword search, so make sure
            # the seed paper itself is represented in the corpus too (once).
            enriched = _paper_to_record(seed_paper, seed.language, "config-seed", "seed")
            if enriched and not storage.is_seen(conn, enriched.uid):
                if enriched.pdf_url:
                    saved_path = downloader.download(enriched.uid, enriched.title, enriched.pdf_url)
                    if saved_path:
                        enriched.downloaded_path = saved_path
                        stats.downloaded_count += 1
                storage.save_record(conn, enriched)
                stats.new_count += 1

        candidates = [
            (p, "reference") for p in s2.get_references(paper_id)
        ] + [
            (p, "citation") for p in s2.get_citations(paper_id)
        ] + [
            (p, "recommendation") for p in s2.get_recommendations(paper_id)
        ]

        for paper, relation in candidates:
            title = paper.get("title") or ""
            abstract = paper.get("abstract") or ""
            if not relevance.is_relevant(title, abstract):
                stats.filtered_count += 1
                continue

            record = _paper_to_record(paper, seed.language, seed.uid, relation)
            if record is None:
                continue

            if storage.is_seen(conn, record.uid):
                stats.seen_count += 1
                continue

            if record.pdf_url:
                saved_path = downloader.download(record.uid, record.title, record.pdf_url)
                if saved_path:
                    record.downloaded_path = saved_path
                    stats.downloaded_count += 1

            storage.save_record(conn, record)
            stats.new_count += 1

    log.info(
        "Snowball complete: %d new, %d downloaded, %d already seen, %d filtered as off-topic.",
        stats.new_count, stats.downloaded_count, stats.seen_count, stats.filtered_count,
    )
    return stats
