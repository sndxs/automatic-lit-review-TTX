"""Patch-based updater for the transitory paper draft.

literature_review_official.tex is never touched by this module -- it only
advances when a human reviews the transitory draft and runs
promote_transitory.py.

Earlier versions of this module asked the model to rewrite the entire
transitory .tex on every run. That failed once the paper grew past ~39k
tokens: the model's reproduction of the existing document plus additions
exceeded even its own 64000-token output ceiling, so every update got
rejected as truncated (see git history). This version instead asks the
model for a small JSON patch -- new bibliography entries, and for each new
paper either a best-fit existing subsection plus a one-sentence mention, or
"no good fit" -- and applies it with plain string insertion. Output size no
longer depends on the paper's size at all, so this has no equivalent
failure mode. Numeric totals (corpus counts, the PRISMA figure) are
deliberately NOT touched by this module: those describe a specific,
human-screened snapshot, and bumping them for papers nobody has read yet
would misrepresent what's actually been screened. New papers instead land
in a running "pending full review" list (see literature_review_transitory.tex,
between the AUTO-PENDING-LIST-START/END markers) until a human promotes them.
"""

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic

import config

log = logging.getLogger("paper_updater")

PATCH_PROMPT_TEMPLATE = """You are proposing a small set of additions to a LaTeX literature review, given newly found candidate papers and the review's existing subsection structure. Do NOT reproduce or rewrite the document. Respond with ONLY a JSON array, one object per new paper below, in this exact shape:

{{
  "uid": "<copied exactly from the paper's UID line below>",
  "bibitem_key": "<new, unique citation key, lowercase, no spaces, in the style authorYEARshorttitle, e.g. smith2026testanxiety>",
  "bibitem_label": "<natbib short-cite label without brackets, e.g. Smith(2026) or Smith et al.(2026)>",
  "bibitem_body": "<the reference-list text for this entry, in APA 7th edition style: sentence case, NOT italicized for an article/preprint title (italicize only if this is clearly a book, dissertation, thesis, or formal report); journal name and volume italicized if given, issue in parentheses not italicized; a DOI as a plain https://doi.org/... link if you can construct one (e.g. for an arXiv id use https://doi.org/10.48550/arXiv.<id>), else the record URL given below. Use ONLY the fields given for this paper below -- do not invent a journal name, volume, issue, or page numbers that aren't given. This must be VALID LATEX, not Markdown: italics are \\\\textit{{...}}, never *asterisks* or **double asterisks**. Escape any literal & % $ # _ characters that appear in a title or author name (e.g. & becomes \\\\&).>",
  "anchor_section": "<the exact title string of the single best-fitting subsection from ANCHOR SECTIONS below, or null if none fit well>",
  "blurb": "<if anchor_section is not null: one sentence citing this paper with \\\\citet{{bibitem_key}}, written to be appended as a short new paragraph at the end of that subsection, in the voice of the surrounding review prose -- conservative, based only on the abstract given, no invented findings. Must be valid LaTeX, not Markdown (\\\\textit{{...}} not *asterisks*), with any literal & % $ # _ characters escaped. If anchor_section is null, this must be null.>"
}}

ANCHOR SECTIONS (choose anchor_section from this list only, or null):
{anchor_list}

NEW PAPERS ({count}):
{new_papers_block}

Output ONLY the JSON array. No commentary, no markdown code fences, nothing else.
"""


@dataclass
class PatchResult:
    bibliography_added: list = field(default_factory=list)   # [(key, title)]
    section_insertions: list = field(default_factory=list)   # [(title, anchor_section)]
    pending_only: list = field(default_factory=list)          # [title]
    diff_text: str = ""
    applied_count: int = 0


def _format_new_papers(records) -> str:
    blocks = []
    for r in records:
        blocks.append(
            f"UID: {r.uid}\n"
            f"Title: {r.title}\n"
            f"Authors: {r.authors or 'unknown'}\n"
            f"Date: {r.date_published or 'unknown'}\n"
            f"Source: {r.source} [{r.language}]\n"
            f"URL: {r.record_url}\n"
            f"Abstract: {r.description or '(no abstract available)'}"
        )
    return "\n\n".join(blocks)


def _extract_json_array(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text.strip())
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array found in model output")
    return json.loads(text[start:end + 1])


def _existing_bibitem_keys(tex: str) -> set:
    return set(re.findall(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}", tex))


_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape_latex(text: str) -> str:
    """Escape LaTeX special characters in text that didn't come from the model
    (e.g. a raw record title inserted directly by this module)."""
    return "".join(_LATEX_ESCAPES.get(ch, ch) for ch in text)


def _markdown_to_latex_italics(text: str) -> str:
    """Defensive cleanup: the model is instructed to output LaTeX, not
    Markdown, but occasionally emits *word* for italics anyway."""
    return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\\textit{\1}", text)


def _unique_key(proposed: str, taken: set) -> str:
    key = proposed
    suffix = ord("b")
    while key in taken:
        key = f"{proposed}{chr(suffix)}"
        suffix += 1
    return key


def _insert_bibliography_entries(tex: str, items: list) -> str:
    entries = "\n\n".join(
        f"\\bibitem[{item['bibitem_label']}]{{{item['bibitem_key']}}}\n{_markdown_to_latex_italics(item['bibitem_body'])}"
        for item in items
    )
    marker = "\\end{thebibliography}"
    idx = tex.rfind(marker)
    if idx == -1:
        log.error("Could not find \\end{thebibliography} -- skipping bibliography insertion.")
        return tex
    return tex[:idx] + entries + "\n\n" + tex[idx:]


def _insert_section_blurb(tex: str, anchor_title: str, blurb: str) -> tuple:
    anchor_marker = "\\subsection{" + anchor_title + "}"
    start = tex.find(anchor_marker)
    if start == -1:
        return tex, False

    search_from = start + len(anchor_marker)
    next_subsection = tex.find("\\subsection{", search_from)
    next_section = tex.find("\\section{", search_from)
    candidates = [p for p in (next_subsection, next_section) if p != -1]
    boundary = min(candidates) if candidates else len(tex)

    insertion = "\n\n" + _markdown_to_latex_italics(blurb.strip()) + "\n\n"
    return tex[:boundary] + insertion + tex[boundary:], True


_PENDING_PLACEHOLDER = r"\item \emph{None yet -- this list is populated automatically as new candidate papers are found.}"


def _insert_pending_list_items(tex: str, items: list, records_by_uid: dict) -> str:
    start_marker = "% AUTO-PENDING-LIST-START"
    end_marker = "% AUTO-PENDING-LIST-END"
    start = tex.find(start_marker)
    end = tex.find(end_marker)
    if start == -1 or end == -1:
        log.warning("Pending-list markers not found -- skipping pending-list update.")
        return tex

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = []
    for item in items:
        record = records_by_uid.get(item["uid"])
        title = _escape_latex(record.title) if record else "(untitled)"
        source = _escape_latex(f"{record.source} [{record.language}]") if record else "unknown source"
        note = ""
        if item.get("anchor_section"):
            note = f" Cited provisionally in the ``{item['anchor_section']}'' subsection."
        lines.append(
            f"\\item \\citet{{{item['bibitem_key']}}} -- \\emph{{{title}}} "
            f"({source}). Found {today}.{note}"
        )

    insertion_point = start + len(start_marker)
    existing_between = tex[insertion_point:end]
    if existing_between.strip() == _PENDING_PLACEHOLDER:
        existing_between = ""  # first real entries -- drop the "none yet" placeholder

    return tex[:insertion_point] + "\n" + "\n".join(lines) + "\n" + existing_between.strip() + ("\n" if existing_between.strip() else "") + tex[end:]


def update_transitory_paper(new_records):
    """Fold new_records into the transitory .tex via a small LLM-proposed patch.

    Returns a PatchResult if anything was applied, or None if the step was
    skipped (no API key, no transitory file, API/JSON error) or the model
    proposed nothing usable.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY") or config.ANTHROPIC_API_KEY
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set -- skipping transitory paper update.")
        return None

    if not config.TRANSITORY_PAPER_PATH.exists():
        log.warning("%s does not exist -- skipping transitory paper update.", config.TRANSITORY_PAPER_PATH)
        return None

    original_tex = config.TRANSITORY_PAPER_PATH.read_text(encoding="utf-8")
    prompt = PATCH_PROMPT_TEMPLATE.format(
        count=len(new_records),
        new_papers_block=_format_new_papers(new_records),
        anchor_list="\n".join(f"- {s}" for s in config.PAPER_UPDATE_ANCHOR_SECTIONS),
    )

    client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
    try:
        response = client.messages.create(
            model=config.PAPER_UPDATE_MODEL,
            max_tokens=config.PAPER_UPDATE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        log.exception("Anthropic API call failed -- leaving transitory paper unchanged.")
        return None

    raw_text = "".join(block.text for block in response.content if block.type == "text")
    try:
        items = _extract_json_array(raw_text)
    except (ValueError, json.JSONDecodeError):
        log.error("Could not parse a JSON array from the model's patch response -- leaving transitory paper unchanged.")
        return None

    if not items:
        log.info("Model proposed no bibliography/patch items for this run's new records.")
        return None

    records_by_uid = {r.uid: r for r in new_records}
    taken_keys = _existing_bibitem_keys(original_tex)
    valid_titles = set(config.PAPER_UPDATE_ANCHOR_SECTIONS)

    for item in items:
        item["bibitem_key"] = _unique_key(item.get("bibitem_key", "unknown"), taken_keys)
        taken_keys.add(item["bibitem_key"])
        if item.get("anchor_section") not in valid_titles:
            item["anchor_section"] = None
            item["blurb"] = None

    tex = original_tex
    tex = _insert_bibliography_entries(tex, items)

    result = PatchResult()
    for item in items:
        record = records_by_uid.get(item["uid"])
        title = record.title if record else item["bibitem_key"]
        result.bibliography_added.append((item["bibitem_key"], title))

        if item.get("anchor_section") and item.get("blurb"):
            tex, ok = _insert_section_blurb(tex, item["anchor_section"], item["blurb"])
            if ok:
                result.section_insertions.append((title, item["anchor_section"]))
            else:
                log.warning("Anchor section %r not found in document -- listing %r in pending list only.", item["anchor_section"], title)
                result.pending_only.append(title)
                item["anchor_section"] = None
        else:
            result.pending_only.append(title)

    tex = _insert_pending_list_items(tex, items, records_by_uid)
    tex = re.sub(r"\n{3,}", "\n\n", tex)

    result.diff_text = "\n".join(
        difflib.unified_diff(
            original_tex.splitlines(), tex.splitlines(),
            fromfile="transitory (before)", tofile="transitory (after)", lineterm="",
        )
    )
    result.applied_count = len(items)

    config.TRANSITORY_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = config.TRANSITORY_BACKUPS_DIR / f"transitory.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tex"
    backup_path.write_text(original_tex, encoding="utf-8")

    config.TRANSITORY_PAPER_PATH.write_text(tex, encoding="utf-8")
    log.info(
        "Transitory paper patched: %d bibliography entr(y/ies) added, %d cited in a section, %d pending-only. Backup: %s",
        len(result.bibliography_added), len(result.section_insertions), len(result.pending_only), backup_path.name,
    )
    return result
