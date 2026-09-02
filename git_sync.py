"""Commits and pushes any pending changes to the repo.

Only files not covered by .gitignore ever show up here -- papers/, logs/,
seen_papers.db, results.csv, .env, and build artifacts are all ignored, so
in practice this only picks up literature_review_transitory.tex/.pdf after
paper_updater has rewritten them (or literature_review_official.tex/.pdf
after a manual promotion). If there's nothing to commit, this is a no-op.

Runs `git` as a subprocess using whatever credentials/credential helper are
already configured on this machine -- the same as any manual `git push`
here, nothing new to authenticate.
"""

import logging
import subprocess

import config

log = logging.getLogger("git_sync")


def _run(args):
    return subprocess.run(
        ["git", *args], cwd=config.PROJECT_ROOT, capture_output=True, text=True, timeout=120,
    )


def sync(commit_message: str) -> bool:
    """Commit and push any pending changes. Returns True if a commit was pushed."""
    status = _run(["status", "--porcelain"])
    if status.returncode != 0:
        log.error("git status failed: %s", status.stderr.strip())
        return False

    if not status.stdout.strip():
        log.info("git_sync: nothing to commit.")
        return False

    add = _run(["add", "-A"])
    if add.returncode != 0:
        log.error("git add failed: %s", add.stderr.strip())
        return False

    commit = _run(["commit", "-m", commit_message])
    if commit.returncode != 0:
        log.error("git commit failed: %s", commit.stderr.strip())
        return False

    push = _run(["push", "origin", "main"])
    if push.returncode != 0:
        log.error("git push failed (commit was made locally but NOT pushed): %s", push.stderr.strip())
        return False

    log.info("git_sync: committed and pushed -- %s", commit_message)
    return True
