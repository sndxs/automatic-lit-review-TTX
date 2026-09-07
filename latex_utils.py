"""Small shared helpers for safely inserting raw/LLM-produced text into LaTeX
source, used by paper_updater.py and deep_dive_news.py's report generation."""

import re

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


def escape_latex(text: str) -> str:
    """Escape LaTeX special characters in text that didn't come from an LLM
    already asked to produce LaTeX (e.g. a raw record title inserted
    directly by Python)."""
    return "".join(_LATEX_ESCAPES.get(ch, ch) for ch in text or "")


def markdown_to_latex_italics(text: str) -> str:
    """Defensive cleanup: a model instructed to output LaTeX occasionally
    emits *word* for italics anyway -- convert it rather than leave stray
    asterisks in the compiled document."""
    return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\\textit{\1}", text or "")
