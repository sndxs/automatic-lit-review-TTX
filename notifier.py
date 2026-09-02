"""Email notification sent at the end of every pipeline run."""

import logging
import os
import smtplib
from email.mime.text import MIMEText

import config

log = logging.getLogger("notifier")


def _format_records_list(records, limit: int = 30) -> str:
    if not records:
        return "(none)"
    lines = [f"- [{r.source}/{r.language}] {r.title}\n  {r.record_url}" for r in records[:limit]]
    if len(records) > limit:
        lines.append(f"... and {len(records) - limit} more -- see results.csv")
    return "\n".join(lines)


def send_run_summary_email(
    new_count: int,
    downloaded_count: int,
    seen_count: int,
    filtered_count: int,
    new_records,
    transitory_updated: bool,
) -> None:
    gmail_address = os.environ.get("GMAIL_ADDRESS") or config.GMAIL_ADDRESS
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD") or config.GMAIL_APP_PASSWORD
    recipient = os.environ.get("NOTIFY_EMAIL_TO") or config.NOTIFY_EMAIL_TO

    if not (gmail_address and gmail_app_password and recipient):
        log.warning("Email credentials not fully configured (GMAIL_ADDRESS/GMAIL_APP_PASSWORD/NOTIFY_EMAIL_TO) -- skipping notification email.")
        return

    subject = (
        f"TTX Lit Reviewer: {new_count} new paper(s) found"
        if new_count
        else "TTX Lit Reviewer: no new papers today"
    )

    body_lines = [
        f"New: {new_count}  |  Downloaded: {downloaded_count}  |  Already seen: {seen_count}  |  Filtered off-topic: {filtered_count}",
        "",
    ]
    if transitory_updated:
        body_lines.append(
            "literature_review_transitory.tex was updated with these papers -- "
            "review it and run promote_transitory.py if it should become official."
        )
        body_lines.append("")
    body_lines.append("New papers this run:")
    body_lines.append(_format_records_list(new_records))

    msg = MIMEText("\n".join(body_lines), "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_address, gmail_app_password)
            server.sendmail(gmail_address, [recipient], msg.as_string())
        log.info("Notification email sent to %s.", recipient)
    except Exception:
        log.exception("Failed to send notification email.")
