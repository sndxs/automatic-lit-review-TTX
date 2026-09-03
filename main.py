"""Daily entry point for the TTX (Test-Taker eXperience) lit reviewer.

For each configured language and query, runs each enabled source, skips
records already seen in previous runs, downloads open-access PDFs where a
direct file link exists, and records everything (found + downloaded) in
seen_papers.db and results.csv.

Usage:
    python main.py
"""

import logging
import sys
from datetime import datetime

import config
import downloader
import git_sync
import latex_compiler
import notifier
import paper_updater
import relevance
import snowball
import storage
from sources import arxiv, share_osf, core, openalex, gdelt

SOURCES = []
if config.ENABLE_ARXIV:
    SOURCES.append(("arxiv", arxiv))
if config.ENABLE_SHARE:
    SOURCES.append(("share", share_osf))
if config.ENABLE_CORE:
    SOURCES.append(("core", core))
if config.ENABLE_OPENALEX:
    SOURCES.append(("openalex", openalex))
if config.ENABLE_GDELT:
    SOURCES.append(("gdelt", gdelt))


def setup_logging() -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = config.LOGS_DIR / f"{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def run() -> None:
    setup_logging()
    log = logging.getLogger("main")
    errors: list = []

    if not SOURCES:
        log.warning("No sources enabled in config.py -- nothing to do.")
        return

    conn = storage.get_connection()
    new_count = 0
    downloaded_count = 0
    seen_count = 0
    filtered_count = 0
    new_records = []

    try:
        for language in config.TOPIC_TERMS:
            for source_name, module in SOURCES:
                log.info("Searching %s [%s]", source_name, language)
                try:
                    records = module.search(language)
                except Exception as e:
                    log.exception("Unhandled error searching %s [%s]", source_name, language)
                    errors.append(f"search {source_name} [{language}]: {e}")
                    continue

                log.info("  -> %d raw result(s)", len(records))
                for record in records:
                    if not relevance.is_relevant(record.title, record.description):
                        filtered_count += 1
                        continue

                    if storage.is_seen(conn, record.uid):
                        seen_count += 1
                        continue

                    if record.pdf_url:
                        saved_path = downloader.download(record.uid, record.title, record.pdf_url)
                        if saved_path:
                            record.downloaded_path = saved_path
                            downloaded_count += 1

                    storage.save_record(conn, record)
                    new_count += 1
                    new_records.append(record)
    except Exception as e:
        log.exception("Unhandled error in the main search loop -- proceeding with whatever was found so far.")
        errors.append(f"main search loop aborted early: {e}")

    if config.ENABLE_SNOWBALL:
        try:
            snowball_stats = snowball.run(conn, new_records)
            new_count += snowball_stats.new_count
            downloaded_count += snowball_stats.downloaded_count
            seen_count += snowball_stats.seen_count
            filtered_count += snowball_stats.filtered_count
        except Exception as e:
            log.exception("Unhandled error during snowballing -- keeping results found so far.")
            errors.append(f"snowballing: {e}")

    storage.export_csv(conn)
    conn.close()

    log.info(
        "Run complete: %d new record(s), %d downloaded, %d already seen, %d filtered as off-topic.",
        new_count, downloaded_count, seen_count, filtered_count,
    )

    patch_result = None
    if new_records:
        try:
            patch_result = paper_updater.update_transitory_paper(new_records)
        except Exception as e:
            log.exception("Unhandled error updating transitory paper -- leaving it unchanged.")
            errors.append(f"transitory paper update: {e}")

        if patch_result:
            try:
                latex_compiler.compile_pdf(config.TRANSITORY_PAPER_PATH)
            except Exception as e:
                log.exception("Unhandled error compiling transitory paper PDF.")
                errors.append(f"PDF compilation: {e}")

    try:
        notifier.send_run_summary_email(
            new_count, downloaded_count, seen_count, filtered_count, new_records, patch_result, errors,
        )
    except Exception:
        log.exception("Unhandled error sending notification email.")

    try:
        commit_message = f"Automated run {datetime.now():%Y-%m-%d}: {new_count} new record(s)"
        if patch_result:
            commit_message += f", transitory paper patched ({patch_result.applied_count} paper(s))"
        git_sync.sync(commit_message)
    except Exception as e:
        log.exception("Unhandled error syncing to git.")
        errors.append(f"git sync: {e}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        logging.getLogger("main").exception("FATAL: run() crashed before completing.")
        try:
            import traceback
            notifier.send_error_email("main.run() crashed", traceback.format_exc())
        except Exception:
            logging.getLogger("main").exception("Also failed to send the crash alert email.")
        raise
