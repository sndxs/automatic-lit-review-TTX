"""Download open-access PDFs for records that have a direct file URL.

Records without a pdf_url (paywalled journals, newspaper/magazine articles,
or repository pages with no direct file) are left as metadata + link only.
"""

import logging
import re
from pathlib import Path
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)


def _safe_filename(uid: str, title: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{uid}_{title}"[:150]).strip("_")
    return f"{base or uid}.pdf"


def download(uid: str, title: str, pdf_url: str) -> Optional[str]:
    """Attempt to download pdf_url into config.PAPERS_DIR.

    Returns the saved file path on success, or None if the download was
    skipped or failed (the caller still keeps the metadata + link).
    """
    config.PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.PAPERS_DIR / _safe_filename(uid, title)
    if dest.exists():
        return str(dest)

    try:
        with requests.get(
            pdf_url, stream=True, timeout=config.REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "ttx-lit-reviewer/1.0 (research script)"},
        ) as resp:
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "html" in content_type.lower():
                log.info("Skipping download for %s: link returned HTML, not a file", uid)
                return None

            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > config.MAX_DOWNLOAD_MB * 1024 * 1024:
                log.info("Skipping download for %s: file exceeds %sMB cap", uid, config.MAX_DOWNLOAD_MB)
                return None

            max_bytes = config.MAX_DOWNLOAD_MB * 1024 * 1024
            written = 0
            tmp_dest = dest.with_suffix(".part")
            with open(tmp_dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    written += len(chunk)
                    if written > max_bytes:
                        log.info("Aborting download for %s: exceeded %sMB cap mid-stream", uid, config.MAX_DOWNLOAD_MB)
                        f.close()
                        tmp_dest.unlink(missing_ok=True)
                        return None
                    f.write(chunk)
            tmp_dest.rename(dest)
    except requests.RequestException as exc:
        log.warning("Download failed for %s (%s): %s", uid, pdf_url, exc)
        return None

    return str(dest)
