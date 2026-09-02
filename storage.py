"""SQLite-backed dedupe index and CSV export for found records."""

import csv
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    uid TEXT PRIMARY KEY,
    source TEXT,
    language TEXT,
    query TEXT,
    title TEXT,
    authors TEXT,
    date_published TEXT,
    description TEXT,
    record_url TEXT,
    pdf_url TEXT,
    record_type TEXT,
    downloaded_path TEXT,
    first_seen_at TEXT
);
"""


@dataclass
class Record:
    uid: str  # stable unique id, e.g. "share:E017D-187-17B" or "arxiv:1202.4527v1"
    source: str
    language: str
    query: str
    title: str
    authors: str
    date_published: str
    description: str
    record_url: str
    pdf_url: Optional[str]
    record_type: str
    downloaded_path: Optional[str] = None


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(SCHEMA)
    return conn


def is_seen(conn: sqlite3.Connection, uid: str) -> bool:
    row = conn.execute("SELECT 1 FROM papers WHERE uid = ?", (uid,)).fetchone()
    return row is not None


def save_record(conn: sqlite3.Connection, record: Record) -> None:
    data = asdict(record)
    data["first_seen_at"] = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO papers
            (uid, source, language, query, title, authors, date_published,
             description, record_url, pdf_url, record_type, downloaded_path,
             first_seen_at)
        VALUES
            (:uid, :source, :language, :query, :title, :authors, :date_published,
             :description, :record_url, :pdf_url, :record_type, :downloaded_path,
             :first_seen_at)
        """,
        data,
    )
    conn.commit()


def export_csv(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT uid, source, language, query, title, authors, date_published,
               record_url, pdf_url, record_type, downloaded_path, first_seen_at
        FROM papers
        ORDER BY first_seen_at DESC
        """
    ).fetchall()
    headers = [
        "uid", "source", "language", "query", "title", "authors",
        "date_published", "record_url", "pdf_url", "record_type",
        "downloaded_path", "first_seen_at",
    ]
    with open(config.RESULTS_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
