import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT DEFAULT '',
    description TEXT NOT NULL,
    url TEXT DEFAULT '',
    source TEXT DEFAULT 'manual',

    published_at TEXT,
    first_seen_at TEXT NOT NULL,

    work_arrangement TEXT NOT NULL DEFAULT 'UNKNOWN',
    sponsorship_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    sponsorship_evidence TEXT DEFAULT '',

    freshness_tier TEXT NOT NULL DEFAULT 'LOWER',

    technical_match_score REAL DEFAULT 0.0,
    matched_skills TEXT DEFAULT '',
    gap_skills TEXT DEFAULT '',

    priority_tier TEXT NOT NULL DEFAULT 'NOT_ELIGIBLE',
    priority_score REAL DEFAULT 0.0,

    application_state TEXT NOT NULL DEFAULT 'NEW',
    mode TEXT NOT NULL DEFAULT 'ASSIST',

    resume_docx_path TEXT,
    resume_pdf_path TEXT,
    resume_txt_path TEXT,
    job_analysis_path TEXT,
    application_answers_path TEXT,
    cover_letter_path TEXT,

    notes TEXT DEFAULT '',

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
