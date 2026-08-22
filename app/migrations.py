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


def _m008_sponsorship_evidence_v2_columns(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 section 2: extends the Phase 6
    employer_sponsorship_evidence table (additive only -- no new table) with
    the richer evidence fields the intelligence layer needs: source
    categorization, dataset provenance, occupation/location detail, and an
    idempotency key. Deliberately excludes any beneficiary/worker PII field
    (CLAUDE.md Phase 7 section 37)."""
    add_columns_if_missing(conn, backend, "employer_sponsorship_evidence", [
        ("source_type", "TEXT DEFAULT ''"),
        ("source_record_id", "TEXT DEFAULT ''"),
        ("dataset_id", "INTEGER"),
        ("filing_date", "TEXT"),
        ("visa_class", "TEXT DEFAULT ''"),
        ("occupation_code", "TEXT DEFAULT ''"),
        ("occupation_title", "TEXT DEFAULT ''"),
        ("worksite_city", "TEXT DEFAULT ''"),
        ("worksite_state", "TEXT DEFAULT ''"),
        ("employer_city", "TEXT DEFAULT ''"),
        ("employer_state", "TEXT DEFAULT ''"),
        ("status_outcome", "TEXT DEFAULT ''"),
        ("count_value", "INTEGER"),
        ("company_normalized_name", "TEXT DEFAULT ''"),
        ("company_domain", "TEXT DEFAULT ''"),
        ("raw_source_fingerprint", "TEXT DEFAULT ''"),
        ("snippet", "TEXT DEFAULT ''"),
    ])
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sponsorship_evidence_source_record "
        "ON employer_sponsorship_evidence (dataset_id, source_record_id) WHERE source_record_id != ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_evidence_fiscal_year ON employer_sponsorship_evidence (fiscal_year)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_evidence_occupation ON employer_sponsorship_evidence (occupation_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_evidence_state ON employer_sponsorship_evidence (worksite_state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_evidence_source_type ON employer_sponsorship_evidence (source_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_evidence_company_norm "
        "ON employer_sponsorship_evidence (company_normalized_name)"
    )


def _m009_sponsorship_datasets_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 section 6: dataset versioning -- never silently
    combine unrelated years/sources without provenance."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS sponsorship_datasets (
            {id_column},
            dataset_name TEXT NOT NULL,
            dataset_version TEXT NOT NULL DEFAULT '',
            fiscal_year INTEGER,
            source_url TEXT DEFAULT '',
            downloaded_at TEXT,
            imported_at TEXT,
            record_count INTEGER DEFAULT 0,
            checksum TEXT DEFAULT '',
            schema_version TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING',
            resume_cursor INTEGER NOT NULL DEFAULT 0,
            errors TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sponsorship_datasets_identity "
        "ON sponsorship_datasets (dataset_name, dataset_version, fiscal_year)"
    )


def _m010_company_aliases_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 section 9."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS company_aliases (
            {id_column},
            company_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            alias_type TEXT NOT NULL DEFAULT 'DBA',
            source TEXT DEFAULT '',
            confidence INTEGER DEFAULT 0,
            verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_company_aliases_unique "
        "ON company_aliases (company_id, normalized_alias)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_aliases_normalized ON company_aliases (normalized_alias)")


def _m011_company_relationships_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 section 10: parent/subsidiary/affiliate/acquired
    relationships are stored, never used to auto-transfer sponsorship
    evidence between companies."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS company_relationships (
            {id_column},
            parent_company_id INTEGER NOT NULL,
            child_company_id INTEGER NOT NULL,
            relationship_type TEXT NOT NULL DEFAULT 'SUBSIDIARY',
            confidence INTEGER DEFAULT 0,
            source TEXT DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_company_relationships_unique "
        "ON company_relationships (parent_company_id, child_company_id, relationship_type)"
    )


def _m012_employer_sponsorship_profile_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 section 11/52: derived, cached, per-company
    aggregate -- recomputed on new evidence, never scanned live from millions
    of raw evidence rows on every job classification."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS employer_sponsorship_profile (
            {id_column},
            company_id INTEGER NOT NULL UNIQUE,
            years_with_h1b_activity INTEGER DEFAULT 0,
            most_recent_fiscal_year INTEGER,
            recent_filing_count INTEGER DEFAULT 0,
            historical_filing_count INTEGER DEFAULT 0,
            recent_lca_count INTEGER DEFAULT 0,
            historical_lca_count INTEGER DEFAULT 0,
            recent_occupation_families TEXT DEFAULT '[]',
            recent_occupation_titles TEXT DEFAULT '[]',
            recent_states TEXT DEFAULT '[]',
            continuity_years INTEGER DEFAULT 0,
            trend TEXT DEFAULT 'STABLE',
            source_coverage TEXT DEFAULT '[]',
            historical_strength TEXT NOT NULL DEFAULT 'NONE',
            history_score REAL DEFAULT 0.0,
            history_reasons TEXT DEFAULT '[]',
            computed_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sponsorship_profile_strength ON employer_sponsorship_profile (historical_strength)"
    )


def _m013_sponsorship_decisions_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 sections 21-22: append-only decision audit trail.
    Never updated in place -- a re-classification always inserts a new row
    with an incremented decision_version, so prior decisions stay auditable."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS sponsorship_decisions (
            {id_column},
            job_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            decision_version INTEGER NOT NULL DEFAULT 1,
            classifier_version TEXT NOT NULL DEFAULT '',
            jd_fingerprint TEXT DEFAULT '',
            current_job_evidence TEXT DEFAULT '[]',
            historical_evidence_summary TEXT DEFAULT '{{}}',
            company_policy_evidence TEXT DEFAULT '[]',
            conflicts TEXT DEFAULT '[]',
            reasons TEXT DEFAULT '[]',
            blocking_reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sponsorship_decisions_job_version "
        "ON sponsorship_decisions (job_id, decision_version)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sponsorship_decisions_job ON sponsorship_decisions (job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sponsorship_decisions_status ON sponsorship_decisions (status)")


def _m014_employer_identity_review_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 section 36: ambiguous employer matches never get
    force-merged -- they land here for explicit resolution."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS employer_identity_review (
            {id_column},
            source_company_name TEXT NOT NULL,
            source_domain TEXT DEFAULT '',
            candidate_company_ids TEXT DEFAULT '[]',
            reason TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING',
            resolved_company_id INTEGER,
            resolution_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_identity_review_status ON employer_identity_review (status)"
    )


def _m015_jobs_sponsorship_decision_columns(conn, backend: str) -> None:
    """CLAUDE.md Phase 7 sections 21/24: lets a job row point at its latest
    decision without a join for the common dashboard case, and lets JD-change
    detection compare fingerprints cheaply."""
    add_columns_if_missing(conn, backend, "jobs", [
        ("sponsorship_decision_version", "INTEGER DEFAULT 0"),
        ("jd_sponsorship_fingerprint", "TEXT DEFAULT ''"),
        ("sponsorship_conflict", "INTEGER DEFAULT 0"),
        ("sponsorship_blocking_reason", "TEXT DEFAULT ''"),
    ])


def _m016_application_executions_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 8 section 5: persistent execution record. `active`
    (1 while the execution is in any non-terminal ExecutionStatus, 0 once it
    reaches a terminal one) backs a partial unique index that is the actual
    distributed duplicate-submission guard (section 61/32) -- two workers
    racing to start an execution for the same job_id can never both succeed,
    because the second INSERT's unique-index violation is what serializes
    them, not application-level locking. No secrets/passwords/tokens are
    ever columns here (section 5's explicit "no secrets, no passwords")."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_executions (
            {id_column},
            execution_id TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'ASSIST',
            status TEXT NOT NULL DEFAULT 'QUEUED',
            active INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            form_fingerprint TEXT DEFAULT '',
            resume_artifact_path TEXT DEFAULT '',
            resume_artifact_hash TEXT DEFAULT '',
            cover_letter_artifact_path TEXT DEFAULT '',
            answers_version INTEGER DEFAULT 0,
            submission_method TEXT DEFAULT '',
            confirmation_id TEXT DEFAULT '',
            confirmation_url TEXT DEFAULT '',
            confirmation_text_fingerprint TEXT DEFAULT '',
            error_type TEXT DEFAULT '',
            error_message_safe TEXT DEFAULT '',
            requires_user_action INTEGER NOT NULL DEFAULT 0,
            user_action_reason TEXT DEFAULT '',
            automation_policy TEXT DEFAULT '',
            policy_reasons TEXT DEFAULT '[]',
            correlation_id TEXT DEFAULT '',
            lease_owner TEXT,
            lease_attempt_id TEXT,
            lease_acquired_at TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_application_executions_job_active "
        "ON application_executions (job_id) WHERE active = 1"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_executions_job ON application_executions (job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_executions_status ON application_executions (status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_application_executions_lease "
        "ON application_executions (lease_expires_at) WHERE active = 1"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_application_executions_started ON application_executions (started_at)"
    )


def _m017_application_answer_snapshots_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 8 section 18: per-execution, versioned answer
    snapshot -- if the candidate profile changes later, an already-submitted
    execution's recorded answers stay exactly as they were at submission
    time. `value` is minimized for sensitive fields (see
    app.applications.repo.snapshot_answers -- a sensitive field stores only
    a bounded fingerprint, never the raw demographic/legal answer text, per
    section 51's "do not print demographic answers/legal answers" logging
    rule extended to storage)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_answer_snapshots (
            {id_column},
            execution_id TEXT NOT NULL,
            field_id TEXT NOT NULL,
            value TEXT DEFAULT '',
            source TEXT DEFAULT '',
            source_version TEXT DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            sensitive INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_answer_snapshots_execution ON application_answer_snapshots (execution_id)"
    )


def _m018_application_audit_log_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 8 section 49: append-only audit trail, correlation-id
    linked. Never logs field values (see app.applications.repo.log_event)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_audit_log (
            {id_column},
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            correlation_id TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_execution ON application_audit_log (execution_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_job ON application_audit_log (job_id)")


def _m019_application_form_baselines_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 8 sections 16-17: per-posting form fingerprint
    baseline, distinct from Phase 6's provider_schema_drift (that table is
    about DISCOVERY payload shape; this one is about the APPLICATION FORM's
    field structure for one specific posting) -- never conflated, never
    reused across the two concerns."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_form_baselines (
            {id_column},
            provider TEXT NOT NULL,
            tenant_identifier TEXT NOT NULL DEFAULT '',
            external_job_id TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL,
            field_signature TEXT DEFAULT '[]',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_form_baselines_identity "
        "ON application_form_baselines (provider, tenant_identifier, external_job_id)"
    )


def _m020_workers_capabilities_column(conn, backend: str) -> None:
    """CLAUDE.md Phase 8 section 39: a worker's declared capability set
    (DISCOVERY / REGISTRY_VERIFY / APPLICATION_PREPARE / APPLICATION_SUBMIT).
    Additive, defaults to empty -- an existing Phase 5-7 worker row/process
    that never sets this is simply capability-less for the new queues, so a
    discovery-only worker can never accidentally claim submission work."""
    add_columns_if_missing(conn, backend, "workers", [("capabilities", "TEXT DEFAULT '[]'")])


MIGRATIONS: list[tuple[int, str, Callable]] = [
    (2, "phase6_worker_identity_columns", _m002_worker_identity_columns),
    (3, "phase6_schema_drift_table", _m003_schema_drift_table),
    (4, "phase6_sponsorship_evidence_table", _m004_sponsorship_evidence_table),
    (5, "phase6_acquisition_priority_columns", _m005_acquisition_priority_columns),
    (6, "phase6_acquisition_records_table", _m006_acquisition_records_table),
    (7, "phase6_correlation_id_column", _m007_correlation_id_column),
    (8, "phase7_sponsorship_evidence_v2_columns", _m008_sponsorship_evidence_v2_columns),
    (9, "phase7_sponsorship_datasets_table", _m009_sponsorship_datasets_table),
    (10, "phase7_company_aliases_table", _m010_company_aliases_table),
    (11, "phase7_company_relationships_table", _m011_company_relationships_table),
    (12, "phase7_employer_sponsorship_profile_table", _m012_employer_sponsorship_profile_table),
    (13, "phase7_sponsorship_decisions_table", _m013_sponsorship_decisions_table),
    (14, "phase7_employer_identity_review_table", _m014_employer_identity_review_table),
    (15, "phase7_jobs_sponsorship_decision_columns", _m015_jobs_sponsorship_decision_columns),
    (16, "phase8_application_executions_table", _m016_application_executions_table),
    (17, "phase8_application_answer_snapshots_table", _m017_application_answer_snapshots_table),
    (18, "phase8_application_audit_log_table", _m018_application_audit_log_table),
    (19, "phase8_application_form_baselines_table", _m019_application_form_baselines_table),
    (20, "phase8_workers_capabilities_column", _m020_workers_capabilities_column),
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
