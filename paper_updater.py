"""LLM-assisted updater for the transitory paper draft.

literature_review_official.tex is never touched by this module -- it only
advances when a human reviews the transitory draft and runs
promote_transitory.py. This module rewrites literature_review_transitory.tex
via the Claude API to fold newly-found records into it, so the working
draft stays current without requiring a person to edit LaTeX by hand every
day. A dated backup of the previous transitory draft is written before every
rewrite, and an update is rejected (leaving the file untouched) if the
model's output doesn't look like a complete LaTeX document.
"""

import logging
import os
from datetime import datetime, timezone

import anthropic

import config

log = logging.getLogger("paper_updater")

PROMPT_TEMPLATE = """You are updating a LaTeX literature review as new candidate papers are found by an automated daily search pipeline.

Below is the CURRENT full LaTeX source of the paper. This is a "transitory" working draft, not the final version -- a human will review it before anything from it is promoted to the official version. Still, do not fabricate anything: only describe a paper using the information given about it below.

Your task: incorporate the NEW PAPERS listed below into this document.

- Add each new paper to \\begin{{thebibliography}} with a properly formatted \\bibitem, generating a citation key in the same style as the existing entries. Format each entry in APA 7th edition style, matching the conventions already used throughout this bibliography: article/preprint/report titles in sentence case and NOT italicized; only book, dissertation, thesis, and formal report titles italicized; journal names and volume numbers italicized, issue numbers in parentheses and not italicized; DOIs as a plain \\url{{https://doi.org/...}} link (never a "doi:" prefix); repository names for preprints (e.g. "arXiv", "OSF Preprints") given as plain, non-italicized text, not folded into an italicized pseudo-journal-name. For an arXiv paper, construct its DOI as https://doi.org/10.48550/arXiv.<arxiv-id> rather than writing "arXiv preprint arXiv:<id>".
- Where a new paper is clearly relevant to an existing thematic subsection (e.g. anxiety/stress, fairness, mode of delivery, teacher perspectives), add a sentence or two citing it in that subsection, written in the voice/style of the surrounding prose. If it doesn't fit any existing subsection well, add a brief standalone mention in the most relevant Findings section instead of forcing a fit.
- Update the numeric totals in the "Corpus at Time of Writing" subsection and in the PRISMA-style flow diagram to reflect the new totals (old total + number of new papers). If you don't have enough information to recompute a full per-source breakdown, keep the existing breakdown numbers and just adjust the overall total, and say so does not need a footnote -- just be numerically consistent.
- Do NOT invent findings, quotes, or details about any paper beyond what's given in its title/authors/abstract below. If a new paper's abstract is missing, describe it only by title and note its relevance briefly and conservatively.
- Do NOT remove, alter, or renumber any existing citation, section, or claim about previously-included papers.
- Preserve the document's overall structure, section ordering, and all LaTeX preamble/packages exactly as they are.

Output ONLY the complete, updated LaTeX source, starting with \\documentclass and ending with \\end{{document}}. No commentary, no markdown code fences, nothing else.

=== NEW PAPERS FOUND ({count}) ===
{new_papers_block}

=== CURRENT LATEX SOURCE ===
{current_tex}
"""


def _format_new_papers(records) -> str:
    blocks = []
    for r in records:
        blocks.append(
            f"- Title: {r.title}\n"
            f"  Authors: {r.authors or 'unknown'}\n"
            f"  Date: {r.date_published or 'unknown'}\n"
            f"  Source: {r.source} [{r.language}]\n"
            f"  URL: {r.record_url}\n"
            f"  Abstract: {r.description or '(no abstract available)'}"
        )
    return "\n\n".join(blocks)


def update_transitory_paper(new_records) -> bool:
    """Fold new_records into the transitory .tex via the Claude API.

    Returns True if the transitory file was rewritten, False if the step
    was skipped (e.g. no API key configured, no existing transitory file)
    or the model's output was rejected as not looking like valid LaTeX.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY") or config.ANTHROPIC_API_KEY
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set -- skipping transitory paper update.")
        return False

    if not config.TRANSITORY_PAPER_PATH.exists():
        log.warning("%s does not exist -- skipping transitory paper update.", config.TRANSITORY_PAPER_PATH)
        return False

    current_tex = config.TRANSITORY_PAPER_PATH.read_text(encoding="utf-8")
    prompt = PROMPT_TEMPLATE.format(
        count=len(new_records),
        new_papers_block=_format_new_papers(new_records),
        current_tex=current_tex,
    )

    client = anthropic.Anthropic(api_key=api_key, timeout=600.0)
    try:
        response = client.messages.create(
            model=config.PAPER_UPDATE_MODEL,
            max_tokens=config.PAPER_UPDATE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        log.exception("Anthropic API call failed -- leaving transitory paper unchanged.")
        return False

    updated_tex = "".join(block.text for block in response.content if block.type == "text").strip()

    if not updated_tex.startswith("\\documentclass") or "\\end{document}" not in updated_tex:
        log.error("LLM output did not look like a complete LaTeX document -- rejecting update, leaving transitory paper unchanged.")
        return False

    config.TRANSITORY_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = config.TRANSITORY_BACKUPS_DIR / f"transitory.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tex"
    backup_path.write_text(current_tex, encoding="utf-8")

    config.TRANSITORY_PAPER_PATH.write_text(updated_tex, encoding="utf-8")
    log.info(
        "Transitory paper updated with %d new paper(s). Previous version backed up to %s.",
        len(new_records), backup_path.name,
    )
    return True
