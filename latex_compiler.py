"""Compiles a .tex file to PDF via latexmk, if available.

Used to keep literature_review_transitory.pdf in sync with its .tex
whenever paper_updater rewrites it, and to recompile
literature_review_official.pdf whenever promote_transitory.py runs. Skips
(logged, non-fatal) if latexmk isn't on PATH or the compile fails --
callers still have a usable .tex either way, just a possibly-stale PDF.
"""

import logging
import shutil
import subprocess

log = logging.getLogger("latex_compiler")


def compile_pdf(tex_path) -> bool:
    """Compile tex_path to a PDF alongside it via latexmk. Returns True on success."""
    if shutil.which("latexmk") is None:
        log.warning("latexmk not found on PATH -- skipping PDF compilation for %s.", tex_path.name)
        return False

    try:
        result = subprocess.run(
            ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-cd", str(tex_path)],
            capture_output=True, text=True, timeout=300,
        )
    except Exception:
        log.exception("Failed to invoke latexmk for %s.", tex_path.name)
        return False

    if result.returncode != 0:
        log.error(
            "latexmk failed for %s (exit %d) -- leaving previous PDF in place:\n%s",
            tex_path.name, result.returncode, result.stdout[-3000:],
        )
        return False

    log.info("Compiled %s -> %s", tex_path.name, tex_path.with_suffix(".pdf").name)
    return True
