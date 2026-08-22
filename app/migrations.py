"""Schema-versioning/migration framework (CLAUDE.md Phase 6 section 5).

Phase 1-5's schema (app.db.SCHEMA + the additive-column helpers in app.db)
is already fully idempotent (CREATE TABLE IF NOT EXISTS / manual
column-existence checks) and stays exactly as it was -- untouched, still
applied directly by app.db.init_sqlite_db()/app.db_postgres.init_db() before
this module ever runs. This module is what Phase 6 (and every future phase)
uses for *new* schema changes going forward, with real, deterministic
ordering and a recorded current version -- something Phase 1-5 didn't need
yet because there was only ever one additive layer to apply.

Design:
  - Each migration is (version: int, name: str, sql_by_backend: dict).
    Versions are applied strictly in ascending order; a version already
    recorded in `schema_migrations` is never re-applied (idempotent
    startup check).
  - Every migration's SQL is itself idempotent (CREATE TABLE IF NOT EXISTS,
    CREATE INDEX IF NOT EXISTS, or the add_columns_if_missing() helper below
    for ALTER TABLE ADD COLUMN, which SQLite has no IF NOT EXISTS spelling
    for but Postgres does) -- so re-running init_db() on an already-migrated
    database is always a safe no-op, matching "no destructive migration by
    default" and "safe failure".
  - No rollback mechanism is implemented or claimed. Every migration here is
    additive (new table / new nullable-or-defaulted column) so a rollback
    was never needed for Phase 6 -- this is stated plainly rather than
    faking a `down()` step that was never built or tested.
  - Migrations run inside the same connection/transaction app.db.init_db()
    already opens (db_session() commits once at the end), so a failure
    partway through a migration rolls back that migration's own statements
    rather than leaving a half-applied schema change committed.
"""

from datetime import datetime, timezone
from typing import Callable

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    backend TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL
)
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_columns_if_missing(conn, backend: str, table: str, columns: list[tuple[str, str]]) -> None:
    """Portable ALTER TABLE ADD COLUMN, safe to call every startup. SQLite has
    no `ADD COLUMN IF NOT EXISTS` (checked: raises a syntax error on 3.45),
    so it queries PRAGMA table_info first; Postgres supports the clause
    directly (9.6+)."""
    if backend == "postgres":
        for col_name, col_def in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
        return
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_def in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


def _m002_worker_identity_columns(conn, backend: str) -> None:
    """CLAUDE.md Phase 6 sections 8/19: multi-machine worker identity needs
    software/schema/capability version + backend fields so a mixed-version
    fleet can be detected rather than silently corrupting state."""
    add_columns_if_missing(conn, backend, "workers", [
        ("worker_version", "TEXT DEFAULT ''"),
        ("schema_version", "INTEGER DEFAULT 0"),
        ("capability_version", "TEXT DEFAULT ''"),
        ("backend", "TEXT DEFAULT ''"),
    ])


def _m003_schema_drift_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 6 section 16: persistent, bounded schema-drift
    tracking distinct from the per-attempt boolean check
    app.workers.schema_check already does. Never stores raw payloads --
    only a structural signature string (see app.workers.schema_check
    .structural_signature) and small text fields."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS provider_schema_drift (
            {id_column},
            provider TEXT NOT NULL,
            tenant_identifier TEXT NOT NULL DEFAULT '',
            signature TEXT NOT NULL,
            expected_parser_version TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            detail TEXT DEFAULT ''
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_drift_signature "
        "ON provider_schema_drift (provider, tenant_identifier, signature)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schema_drift_provider ON provider_schema_drift (provider)")


def _m004_sponsorship_evidence_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 6 section 27: storage foundation for Phase 7
    sponsorship intelligence. Durable rule preserved: a row here is evidence
    about a COMPANY's history, never proof that a specific current job is
    CONFIRMED_SPONSOR -- see app/sponsorship/evidence.py and
    app.pipeline's sponsorship gate, which never reads this table."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS employer_sponsorship_evidence (
            {id_column},
            company_id INTEGER,
            company_name_raw TEXT NOT NULL,
            source TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            fiscal_year INTEGER,
            petition_type TEXT DEFAULT '',
            job_title TEXT DEFAULT '',
            location TEXT DEFAULT '',
            observed_at TEXT NOT NULL,
            confidence INTEGER DEFAULT 0,
            source_quality TEXT DEFAULT '',
            imported_at TEXT NOT NULL,
            notes TEXT DEFAULT ''
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_evidence_company ON employer_sponsorship_evidence (company_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_evidence_name ON employer_sponsorship_evidence (company_name_raw)"
    )


def _m005_acquisition_priority_columns(conn, backend: str) -> None:
    """CLAUDE.md Phase 6 section 26: deterministic acquisition priority --
    explicitly NOT interview probability, NEVER used to promote a job to
    CONFIRMED_SPONSOR (see app/registry/acquisition_priority.py)."""
    add_columns_if_missing(conn, backend, "registry_companies", [
        ("priority_score", "REAL DEFAULT 0.0"),
        ("priority_reasons", "TEXT DEFAULT '[]'"),
        ("has_sponsorship_history_signal", "INTEGER DEFAULT 0"),
    ])


def _m006_acquisition_records_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 6 section 28: per-row checkpoint/lease tracking for a
    large acquisition batch, so two workers/processes resuming the same
    batch never create duplicate companies -- see
    app/registry/acquisition.py's claim_acquisition_record_batch()."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS registry_acquisition_records (
            {id_column},
            batch_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            company_name_raw TEXT NOT NULL,
            company_domain_raw TEXT DEFAULT '',
            raw_row_json TEXT NOT NULL DEFAULT '{{}}',
            status TEXT NOT NULL DEFAULT 'PENDING',
            lease_owner TEXT,
            lease_expires_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            verification_result TEXT DEFAULT '',
            company_id INTEGER,
            portal_id INTEGER,
            error TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_acquisition_records_batch_row "
        "ON registry_acquisition_records (batch_id, row_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_acquisition_records_status ON registry_acquisition_records (batch_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_acquisition_records_lease ON registry_acquisition_records (lease_expires_at)"
    )


def _m007_correlation_id_column(conn, backend: str) -> None:
    """CLAUDE.md Phase 6 section 36: propagate one correlation id across
    polling attempt -> provider request -> job normalization -> pipeline ->
    application package generation, so a production issue can be traced end
    to end. Nullable/defaulted so existing rows are untouched."""
    add_columns_if_missing(conn, backend, "poll_attempts", [("correlation_id", "TEXT DEFAULT ''")])
    add_columns_if_missing(conn, backend, "jobs", [("correlation_id", "TEXT DEFAULT ''")])


MIGRATIONS: list[tuple[int, str, Callable]] = [
    (2, "phase6_worker_identity_columns", _m002_worker_identity_columns),
    (3, "phase6_schema_drift_table", _m003_schema_drift_table),
    (4, "phase6_sponsorship_evidence_table", _m004_sponsorship_evidence_table),
    (5, "phase6_acquisition_priority_columns", _m005_acquisition_priority_columns),
    (6, "phase6_acquisition_records_table", _m006_acquisition_records_table),
    (7, "phase6_correlation_id_column", _m007_correlation_id_column),
]

# Version 1 is the implicit Phase 1-5 baseline schema, applied by
# app.db.init_sqlite_db()/app.db_postgres.init_db() directly (not through
# this list) since it predates this framework and is already proven/tested.
BASELINE_VERSION = 1
CURRENT_SCHEMA_VERSION = max([BASELINE_VERSION] + [v for v, _, _ in MIGRATIONS])


def _ensure_migrations_table(conn) -> None:
    conn.execute(SCHEMA_MIGRATIONS_TABLE)


def applied_versions(conn) -> set[int]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r["version"] for r in rows}


def run_pending(conn, backend: str) -> list[int]:
    """Applies every migration not yet recorded, strictly in version order.
    Idempotent: safe to call on every process startup regardless of backend."""
    _ensure_migrations_table(conn)
    if BASELINE_VERSION not in applied_versions(conn):
        conn.execute(
            "INSERT INTO schema_migrations (version, name, backend, applied_at) VALUES (?, ?, ?, ?)",
            (BASELINE_VERSION, "phase1_5_baseline_schema", backend, utcnow()),
        )
    newly_applied = []
    applied = applied_versions(conn)
    for version, name, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version in applied:
            continue
        fn(conn, backend)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, backend, applied_at) VALUES (?, ?, ?, ?)",
            (version, name, backend, utcnow()),
        )
        newly_applied.append(version)
    return newly_applied


def current_db_version(conn) -> int:
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def is_compatible(conn) -> bool:
    """False if this process's code expects migrations the live database
    hasn't applied yet (an older DB talking to newer code) -- used by
    /readiness and worker startup compatibility checks (CLAUDE.md Phase 6
    section 19)."""
    return current_db_version(conn) >= CURRENT_SCHEMA_VERSION
