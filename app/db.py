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

CREATE TABLE IF NOT EXISTS discovery_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    providers TEXT DEFAULT '',
    jobs_fetched INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    jobs_deduplicated INTEGER DEFAULT 0,
    jobs_analyzed INTEGER DEFAULT 0,
    confirmed_sponsors INTEGER DEFAULT 0,
    likely_sponsors INTEGER DEFAULT 0,
    hard_skips INTEGER DEFAULT 0,
    packages_generated INTEGER DEFAULT 0,
    errors TEXT DEFAULT '[]',
    duration_seconds REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS application_state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    actor TEXT DEFAULT 'system'
);
"""

# Additive columns introduced after the initial `jobs` table shipped. Applied
# via ALTER TABLE ... ADD COLUMN so existing rows/data are never destroyed.
JOBS_ADDITIVE_COLUMNS = [
    ("provider", "TEXT DEFAULT 'manual'"),
    ("external_job_id", "TEXT DEFAULT ''"),
    ("employment_type", "TEXT DEFAULT ''"),
    ("salary_min", "REAL"),
    ("salary_max", "REAL"),
    ("dedup_fingerprint", "TEXT DEFAULT ''"),
    ("last_seen_at", "TEXT"),
    ("freshness_minutes", "REAL"),
    ("score_breakdown", "TEXT DEFAULT '{}'"),
]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_jobs_table(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col_name, col_def in JOBS_ADDITIVE_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_def}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_provider_external_id "
        "ON jobs (provider, external_job_id) WHERE external_job_id != ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedup_fingerprint ON jobs (dedup_fingerprint)")
    conn.execute(
        "UPDATE jobs SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL"
    )


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate_jobs_table(conn)
        conn.commit()


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
