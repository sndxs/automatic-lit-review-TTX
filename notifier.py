"""Email notification sent at the end of every pipeline run."""

import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config

log = logging.getLogger("notifier")


def _credentials():
    gmail_address = os.environ.get("GMAIL_ADDRESS") or config.GMAIL_ADDRESS
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD") or config.GMAIL_APP_PASSWORD
    recipient = os.environ.get("NOTIFY_EMAIL_TO") or config.NOTIFY_EMAIL_TO
    if not (gmail_address and gmail_app_password and recipient):
        log.warning("Email credentials not fully configured (GMAIL_ADDRESS/GMAIL_APP_PASSWORD/NOTIFY_EMAIL_TO) -- skipping notification email.")
        return None
    return gmail_address, gmail_app_password, recipient


def _send(msg, gmail_address: str, gmail_app_password: str, recipient: str) -> bool:
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_address, gmail_app_password)
            server.sendmail(gmail_address, [recipient], msg.as_string())
        log.info("Email sent to %s: %s", recipient, msg["Subject"])
        return True
    except Exception:
        log.exception("Failed to send email.")
        return False


def _format_records_list(records, limit: int = 30) -> str:
    if not records:
        return "(none)"
    lines = [f"- [{r.source}/{r.language}] {r.title}\n  {r.record_url}" for r in records[:limit]]
    if len(records) > limit:
        lines.append(f"... and {len(records) - limit} more -- see results.csv")
    return "\n".join(lines)


def _format_patch_summary(patch_result) -> str:
    lines = ["literature_review_transitory.tex was patched:"]
    if patch_result.bibliography_added:
        lines.append(f"\nAdded to bibliography ({len(patch_result.bibliography_added)}):")
        lines.extend(f"  - [{key}] {title}" for key, title in patch_result.bibliography_added)
    if patch_result.section_insertions:
        lines.append(f"\nCited in an existing subsection ({len(patch_result.section_insertions)}):")
        lines.extend(f"  - \"{title}\" -> {section}" for title, section in patch_result.section_insertions)
    if patch_result.pending_only:
        lines.append(f"\nNo good section fit, listed in \"Automatically Found Candidates Pending Full Review\" only ({len(patch_result.pending_only)}):")
        lines.extend(f"  - {title}" for title in patch_result.pending_only)
    lines.append(
        "\nReview literature_review_transitory.tex (or the attached PDF) and run "
        "promote_transitory.py if it should become official."
    )
    if patch_result.diff_text:
        lines.append("\n--- Diff (transitory .tex, before vs after) ---\n")
        lines.append(patch_result.diff_text)
    return "\n".join(lines)


def send_run_summary_email(
    new_count: int,
    downloaded_count: int,
    seen_count: int,
    filtered_count: int,
    new_records,
    patch_result=None,
    errors=None,
) -> None:
    creds = _credentials()
    if not creds:
        return
    gmail_address, gmail_app_password, recipient = creds

    subject = (
        f"TTX Lit Reviewer: {new_count} new paper(s) found"
        if new_count
        else "TTX Lit Reviewer: no new papers today"
    )
    if errors:
        subject = f"[{len(errors)} error(s)] " + subject

    body_lines = [
        f"New: {new_count}  |  Downloaded: {downloaded_count}  |  Already seen: {seen_count}  |  Filtered off-topic: {filtered_count}",
        "",
    ]
    if errors:
        body_lines.append(f"*** {len(errors)} error(s) occurred during this run (rest of the pipeline still ran where possible) ***")
        body_lines.extend(f"  - {e}" for e in errors)
        body_lines.append("See today's log file in logs/ for full tracebacks.")
        body_lines.append("")
    if patch_result:
        body_lines.append(_format_patch_summary(patch_result))
        body_lines.append("")
    body_lines.append("New papers this run:")
    body_lines.append(_format_records_list(new_records))

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient
    msg.attach(MIMEText("\n".join(body_lines), "plain", "utf-8"))

    pdf_path = config.TRANSITORY_PAPER_PATH.with_suffix(".pdf")
    if patch_result and pdf_path.exists():
        with open(pdf_path, "rb") as f:
            attachment = MIMEApplication(f.read(), _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
        msg.attach(attachment)

    _send(msg, gmail_address, gmail_app_password, recipient)


def send_error_email(context: str, error_text: str) -> None:
    """Standalone alert for a failure severe enough that the normal summary
    email never got sent (e.g. the pipeline crashed before reaching it).
    Self-contained on purpose -- only needs credentials, not any pipeline
    state -- so it still works even when most of a run failed."""
    creds = _credentials()
    if not creds:
        return
    gmail_address, gmail_app_password, recipient = creds

    msg = MIMEMultipart()
    msg["Subject"] = f"TTX Lit Reviewer: RUN FAILED ({context})"
    msg["From"] = gmail_address
    msg["To"] = recipient
    body = (
        f"The TTX Lit Reviewer run failed and did not complete normally.\n\n"
        f"Context: {context}\n\n"
        f"{error_text}\n\n"
        "See today's log file in logs/ for full details."
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))
    _send(msg, gmail_address, gmail_app_password, recipient)
