"""Promote the reviewed transitory paper to official.

Run this after you've read through literature_review_transitory.tex and are
satisfied its LLM-drafted changes should become the new official version:

    python promote_transitory.py

The previous official version is archived (timestamped) in
transitory_backups/ first, so this is always reversible.
"""

import shutil
import sys
from datetime import datetime, timezone

import config
import git_sync
import latex_compiler


def main() -> None:
    if not config.TRANSITORY_PAPER_PATH.exists():
        print(f"{config.TRANSITORY_PAPER_PATH} does not exist -- nothing to promote.")
        sys.exit(1)

    config.TRANSITORY_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    if config.OFFICIAL_PAPER_PATH.exists():
        archive_path = config.TRANSITORY_BACKUPS_DIR / f"official.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tex"
        shutil.copy2(config.OFFICIAL_PAPER_PATH, archive_path)
        print(f"Archived previous official version to {archive_path}")

    shutil.copy2(config.TRANSITORY_PAPER_PATH, config.OFFICIAL_PAPER_PATH)
    print(f"Promoted {config.TRANSITORY_PAPER_PATH.name} -> {config.OFFICIAL_PAPER_PATH.name}")

    if latex_compiler.compile_pdf(config.OFFICIAL_PAPER_PATH):
        print(f"Recompiled {config.OFFICIAL_PAPER_PATH.with_suffix('.pdf').name}")
    else:
        print("PDF compilation skipped or failed -- see above; the .tex was still promoted.")

    commit_message = f"Promote transitory paper to official ({datetime.now():%Y-%m-%d})"
    if git_sync.sync(commit_message):
        print("Committed and pushed to the repo.")
    else:
        print("Nothing pushed -- see above (either nothing changed, or the push failed and needs a manual look).")


if __name__ == "__main__":
    main()
