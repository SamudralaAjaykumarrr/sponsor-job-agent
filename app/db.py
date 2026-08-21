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

CREATE TABLE IF NOT EXISTS company_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    company_domain TEXT DEFAULT '',
    provider TEXT NOT NULL,
    tenant_identifier TEXT NOT NULL,
    careers_url TEXT DEFAULT '',
    country TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    support_level TEXT NOT NULL DEFAULT 'FULL',
    notes TEXT DEFAULT '',

    last_polled_at TEXT,
    next_poll_at TEXT,
    average_job_yield REAL DEFAULT 0.0,
    average_latency_ms REAL DEFAULT 0.0,
    poll_interval_minutes INTEGER DEFAULT 15,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_provider_tenant
    ON company_registry (provider, tenant_identifier);
CREATE INDEX IF NOT EXISTS idx_registry_next_poll_at ON company_registry (next_poll_at);

CREATE TABLE IF NOT EXISTS job_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    registry_id INTEGER,
    source_url TEXT DEFAULT '',
    provider_job_id TEXT DEFAULT '',
    discovery_cycle_id INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(job_id, provider, provider_job_id)
);

CREATE INDEX IF NOT EXISTS idx_provenance_job_id ON job_provenance (job_id);

CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    provider TEXT NOT NULL,
    company TEXT DEFAULT '',
    tenant TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    latency_ms REAL DEFAULT 0.0,
    jobs_received INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    jobs_duplicate INTEGER DEFAULT 0,
    jobs_filtered INTEGER DEFAULT 0,
    error_type TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_discovery_log_provider ON discovery_log (provider);
CREATE INDEX IF NOT EXISTS idx_discovery_log_cycle_id ON discovery_log (cycle_id);

-- Phase 4: company/portal acquisition-verification-lifecycle registry. Additive
-- only -- the Phase 3 `company_registry` operational polling table above is
-- untouched; a VERIFIED/ACTIVE registry_portals row gets mirrored into it
-- (app/registry/sync.py), never the other way around.
CREATE TABLE IF NOT EXISTS registry_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    primary_domain TEXT DEFAULT '',
    careers_home_url TEXT DEFAULT '',
    country TEXT DEFAULT '',
    headquarters_location TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_companies_identity
    ON registry_companies (normalized_name, primary_domain);
CREATE INDEX IF NOT EXISTS idx_registry_companies_domain ON registry_companies (primary_domain);

CREATE TABLE IF NOT EXISTS registry_portals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES registry_companies(id),
    provider TEXT NOT NULL,
    tenant_identifier TEXT DEFAULT '',
    careers_url TEXT DEFAULT '',
    jobs_url TEXT DEFAULT '',
    canonical_url TEXT DEFAULT '',
    support_level TEXT NOT NULL DEFAULT 'UNSUPPORTED',
    discovery_status TEXT NOT NULL DEFAULT 'IMPORTED',
    verification_status TEXT NOT NULL DEFAULT 'DISCOVERED',
    identity_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    enabled INTEGER NOT NULL DEFAULT 1,
    confidence INTEGER NOT NULL DEFAULT 0,
    confidence_reasons TEXT DEFAULT '[]',
    last_verified_at TEXT,
    last_polled_at TEXT,
    next_poll_at TEXT,
    last_success_at TEXT,
    last_failure_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_permanent_failures INTEGER NOT NULL DEFAULT 0,
    average_job_yield REAL DEFAULT 0.0,
    average_latency_ms REAL DEFAULT 0.0,
    current_job_count INTEGER DEFAULT 0,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 15,
    registry_entry_id INTEGER,
    superseded_by_portal_id INTEGER,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_portals_canonical
    ON registry_portals (canonical_url) WHERE canonical_url != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_portals_provider_tenant
    ON registry_portals (provider, tenant_identifier) WHERE tenant_identifier != '';
CREATE INDEX IF NOT EXISTS idx_registry_portals_company ON registry_portals (company_id);
CREATE INDEX IF NOT EXISTS idx_registry_portals_verification ON registry_portals (verification_status);
CREATE INDEX IF NOT EXISTS idx_registry_portals_next_poll ON registry_portals (next_poll_at);
CREATE INDEX IF NOT EXISTS idx_registry_portals_id_pagination ON registry_portals (id);

CREATE TABLE IF NOT EXISTS registry_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_id INTEGER REFERENCES registry_portals(id),
    company_id INTEGER REFERENCES registry_companies(id),
    source_type TEXT NOT NULL,
    source_name TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    imported_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence TEXT DEFAULT '',
    confidence INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registry_provenance_portal ON registry_provenance (portal_id);
CREATE INDEX IF NOT EXISTS idx_registry_provenance_company ON registry_provenance (company_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_provenance_upsert
    ON registry_provenance (portal_id, source_type, source_name);

CREATE TABLE IF NOT EXISTS registry_portal_health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    http_status INTEGER,
    error_type TEXT DEFAULT '',
    latency_ms REAL,
    jobs_yield INTEGER,
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_health_events_portal ON registry_portal_health_events (portal_id, occurred_at);

CREATE TABLE IF NOT EXISTS registry_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    old_portal_id INTEGER NOT NULL,
    new_portal_id INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    evidence TEXT DEFAULT '',
    confidence INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_migrations_company ON registry_migrations (company_id);

CREATE TABLE IF NOT EXISTS registry_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    format TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    rows_total INTEGER DEFAULT 0,
    rows_created INTEGER DEFAULT 0,
    rows_updated INTEGER DEFAULT 0,
    rows_skipped INTEGER DEFAULT 0,
    rows_invalid INTEGER DEFAULT 0,
    dry_run INTEGER DEFAULT 0,
    errors TEXT DEFAULT '[]'
);

-- ==========================================================================
-- Phase 5: distributed polling execution layer. Additive only. See
-- CLAUDE.md Phase 5 section and docs/phase5-distributed-polling.md.
--
-- Leasing is implemented as extra nullable columns directly on the two
-- tables that are actually claimed for work (company_registry for the poll
-- queue, registry_portals for the verification queue) rather than a
-- separate generic lease table -- one row of each table already IS one unit
-- of work, and app/workers/leasing.py performs a single atomic UPDATE ...
-- WHERE against these columns per claim, which SQLite serializes safely
-- across processes (see app/db.py's WAL + busy_timeout pragmas below).
-- ==========================================================================

-- Bounded, append-only history of every poll/verification attempt made by
-- any worker against any portal. Never contains candidate PII -- only
-- provider/portal/worker/outcome metadata.
CREATE TABLE IF NOT EXISTS poll_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE,
    portal_type TEXT NOT NULL,          -- 'company_registry' | 'registry_portal'
    portal_id INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    queue TEXT NOT NULL DEFAULT 'poll', -- 'poll' | 'verification'
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'LEASED',
    jobs_received INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    jobs_duplicate INTEGER DEFAULT 0,
    jobs_filtered INTEGER DEFAULT 0,
    latency_ms REAL,
    error_type TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    retryable INTEGER DEFAULT 0,
    next_retry_at TEXT,
    cycle_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_poll_attempts_portal ON poll_attempts (portal_type, portal_id, started_at);
CREATE INDEX IF NOT EXISTS idx_poll_attempts_status ON poll_attempts (status);
CREATE INDEX IF NOT EXISTS idx_poll_attempts_worker ON poll_attempts (worker_id);
CREATE INDEX IF NOT EXISTS idx_poll_attempts_started ON poll_attempts (started_at);

-- Worker fleet identity/heartbeat. One row per worker process (deleted only
-- by an explicit cleanup of long-dead rows, never by normal operation).
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    shard_index INTEGER NOT NULL DEFAULT 0,
    shard_count INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'STARTING',
    current_portal_type TEXT DEFAULT '',
    current_portal_id INTEGER,
    portals_processed INTEGER NOT NULL DEFAULT 0,
    jobs_processed INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workers_heartbeat ON workers (last_heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_workers_status ON workers (status);

-- Dead-letter/quarantine for work items that failed permanently and
-- repeatedly. One open (unresolved) row per (portal_type, portal_id);
-- resolving and re-triggering the item creates a fresh row on the next
-- repeated failure rather than reviving the old one.
CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_type TEXT NOT NULL,
    portal_id INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT '',
    last_attempt_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dead_letters_open
    ON dead_letters (portal_type, portal_id) WHERE resolved = 0;
CREATE INDEX IF NOT EXISTS idx_dead_letters_resolved ON dead_letters (resolved);

-- Per-provider circuit-breaker state + cross-process inflight-request
-- concurrency slot counter. One row per provider name, created lazily.
CREATE TABLE IF NOT EXISTS provider_circuit_state (
    provider TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'CLOSED',   -- CLOSED | OPEN | HALF_OPEN
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    window_attempts INTEGER NOT NULL DEFAULT 0,
    window_failures INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT,
    half_open_probe_at TEXT,
    half_open_inflight INTEGER NOT NULL DEFAULT 0,
    inflight INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Resumable registry-acquisition batch tracking (source dataset -> company
-- -> portal candidate -> verified/active/quarantined pipeline). Distinct
-- from the unused Phase-4 registry_import_batches table above (kept as-is,
-- untouched) -- this one is actually written to by app/registry/acquisition.py
-- and supports checkpointed resume.
CREATE TABLE IF NOT EXISTS registry_acquisition_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'CSV',
    path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | RUNNING | PAUSED | COMPLETED | FAILED
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    records_total INTEGER DEFAULT 0,
    records_processed INTEGER DEFAULT 0,
    companies_created INTEGER DEFAULT 0,
    portal_candidates INTEGER DEFAULT 0,
    verified INTEGER DEFAULT 0,
    active INTEGER DEFAULT 0,
    quarantined INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    resume_cursor INTEGER NOT NULL DEFAULT 0,
    errors TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_acquisition_batches_status ON registry_acquisition_batches (status);
"""

# Phase 5: leasing columns for company_registry (the poll queue) and
# registry_portals (the verification queue), applied additively below.
COMPANY_REGISTRY_ADDITIVE_COLUMNS = [
    ("lease_owner", "TEXT"),
    ("lease_attempt_id", "TEXT"),
    ("lease_acquired_at", "TEXT"),
    ("lease_expires_at", "TEXT"),
    ("consecutive_permanent_failures", "INTEGER DEFAULT 0"),
]

REGISTRY_PORTALS_ADDITIVE_COLUMNS = [
    ("verify_lease_owner", "TEXT"),
    ("verify_lease_attempt_id", "TEXT"),
    ("verify_lease_acquired_at", "TEXT"),
    ("verify_lease_expires_at", "TEXT"),
]

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
    # Phase 3 additive columns.
    ("company_identifier", "TEXT DEFAULT ''"),
    ("city", "TEXT DEFAULT ''"),
    ("state", "TEXT DEFAULT ''"),
    ("country", "TEXT DEFAULT ''"),
    ("remote_status", "TEXT DEFAULT ''"),
    ("department", "TEXT DEFAULT ''"),
    ("team", "TEXT DEFAULT ''"),
    ("office", "TEXT DEFAULT ''"),
    ("source_url", "TEXT DEFAULT ''"),
    ("canonical_url", "TEXT DEFAULT ''"),
    ("salary_currency", "TEXT DEFAULT ''"),
    ("salary_period", "TEXT DEFAULT ''"),
    ("provider_metadata", "TEXT DEFAULT '{}'"),
    ("freshness_source", "TEXT DEFAULT 'FIRST_SEEN'"),
]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Phase 5: multiple local worker processes read/write this DB
    # concurrently. WAL lets readers proceed while one writer is active, and
    # a busy_timeout makes a writer that arrives mid-transaction wait (up to
    # the timeout) instead of immediately raising "database is locked" --
    # required for safe multi-process lease acquisition (see
    # app/workers/leasing.py and docs/polling-leases.md). Honest limit: this
    # is still a single SQLite *file* -- it does not make SQLite a
    # multi-machine database (see docs/scaling-claims.md).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_def in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


def _migrate_jobs_table(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "jobs", JOBS_ADDITIVE_COLUMNS)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_provider_external_id "
        "ON jobs (provider, external_job_id) WHERE external_job_id != ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedup_fingerprint ON jobs (dedup_fingerprint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_canonical_url ON jobs (canonical_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_first_seen_at ON jobs (first_seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_published_at ON jobs (published_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_application_state ON jobs (application_state)")
    conn.execute(
        "UPDATE jobs SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL"
    )


def _migrate_phase5_lease_columns(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "company_registry", COMPANY_REGISTRY_ADDITIVE_COLUMNS)
    _add_columns(conn, "registry_portals", REGISTRY_PORTALS_ADDITIVE_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_registry_lease_expiry ON company_registry (lease_expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_registry_portals_verify_lease_expiry ON registry_portals (verify_lease_expires_at)")


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate_jobs_table(conn)
        _migrate_phase5_lease_columns(conn)
        conn.commit()


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
