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


def _m021_application_attempts_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 9 section 6: bounded, append-only per-attempt history
    for the application executor worker fleet -- the application-side
    equivalent of Phase 5's poll_attempts, but with fields specific to the
    prepare/validate/submit/confirm pipeline. Never stores secrets or
    candidate answer values (only ids/stages/results/timestamps)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_attempts (
            {id_column},
            attempt_id TEXT NOT NULL UNIQUE,
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            stage TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            retryable INTEGER NOT NULL DEFAULT 0,
            submission_request_started_at TEXT,
            submission_request_finished_at TEXT,
            confirmation_observed INTEGER NOT NULL DEFAULT 0,
            error_type TEXT DEFAULT '',
            safe_error_message TEXT DEFAULT '',
            correlation_id TEXT DEFAULT ''
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_attempts_execution ON application_attempts (execution_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_attempts_job ON application_attempts (job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_attempts_worker ON application_attempts (worker_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_attempts_started ON application_attempts (started_at)")


def _m022_application_provider_circuit_state_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 9 section 34: a SEPARATE circuit-breaker/inflight-slot
    table for application SUBMISSION attempts, distinct from Phase 5's
    provider_circuit_state (discovery polling). A provider whose discovery
    circuit is open must not automatically block application submission (and
    vice versa) -- the semantics genuinely differ (submission failures are
    rarer, more consequential, and should trip far more conservatively)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS application_provider_circuit_state (
            provider TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'CLOSED',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            window_attempts INTEGER NOT NULL DEFAULT 0,
            window_failures INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT,
            half_open_probe_at TEXT,
            half_open_inflight INTEGER NOT NULL DEFAULT 0,
            inflight INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )"""
    )


def _m023_mock_ats_server_records_table(conn, backend: str) -> None:
    """Test/demo fixture ONLY (provider == 'mock_ats' can never collide with
    a real provider name, matching every prior phase's benchmark-fixture
    convention). Represents "the ATS's own server-side record of a
    submission" -- genuinely separate storage from application_executions,
    so app.applications.mock_ats.MockATSProvider.check_submission_status()
    can demonstrate CLAUDE.md Phase 9 section 8's "check provider-supported
    confirmation method" mechanism against real (if synthetic) evidence
    rather than fabricating an answer."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS mock_ats_server_records (
            {id_column},
            job_id INTEGER NOT NULL,
            external_job_id TEXT DEFAULT '',
            confirmation_id TEXT NOT NULL,
            received_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mock_ats_server_records_job ON mock_ats_server_records (job_id)"
    )


def _m024_jobs_application_worker_columns(conn, backend: str) -> None:
    """CLAUDE.md Phase 9 sections 24-27: lets the final pre-submission
    revalidation pass compare against the JD fingerprint recorded at
    preparation time without a second table -- additive, nullable."""
    add_columns_if_missing(conn, backend, "application_executions", [
        ("prepared_jd_fingerprint", "TEXT DEFAULT ''"),
        ("prepared_employment_type", "TEXT DEFAULT ''"),
        ("prepared_sponsorship_status", "TEXT DEFAULT ''"),
    ])


def _m025_browser_assist_sessions_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 10 sections 4-5: persistent, resumable browser-assist
    session record. One row per browser-assist attempt for one execution.
    `active` (1 while non-terminal, 0 once CONFIRMED/CLOSED/EXPIRED) backs a
    partial unique index -- the same "one active thing per job" pattern
    Phase 8's application_executions already uses -- so two workers/dashboard
    clicks can never both start a second live browser session for a job that
    already has one (CLAUDE.md Phase 10 section 63). Never a column for a
    password, MFA code, cookie, or raw auth token (section 5) -- only ids,
    status, fingerprints, and bounded confirmation evidence."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS browser_assist_sessions (
            {id_column},
            session_id TEXT NOT NULL UNIQUE,
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            application_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'STARTING',
            active INTEGER NOT NULL DEFAULT 1,
            current_step INTEGER NOT NULL DEFAULT 1,
            total_steps_if_known INTEGER,
            browser_profile_reference TEXT DEFAULT '',
            form_fingerprint TEXT DEFAULT '',
            resume_artifact_hash TEXT DEFAULT '',
            answers_version INTEGER DEFAULT 0,
            mapped_field_count INTEGER NOT NULL DEFAULT 0,
            unresolved_field_count INTEGER NOT NULL DEFAULT 0,
            needs_user_action INTEGER NOT NULL DEFAULT 0,
            user_action_reason TEXT DEFAULT '',
            confirmation_observed INTEGER NOT NULL DEFAULT 0,
            confirmation_id TEXT DEFAULT '',
            confirmation_url TEXT DEFAULT '',
            confirmation_text_fingerprint TEXT DEFAULT '',
            worker_id TEXT DEFAULT '',
            lease_owner TEXT,
            lease_attempt_id TEXT,
            lease_acquired_at TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            closed_at TEXT
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_sessions_job_active "
        "ON browser_assist_sessions (job_id) WHERE active = 1"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_browser_sessions_execution ON browser_assist_sessions (execution_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_browser_sessions_status ON browser_assist_sessions (status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_browser_sessions_lease "
        "ON browser_assist_sessions (lease_expires_at) WHERE active = 1"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_browser_sessions_last_activity ON browser_assist_sessions (last_activity_at)"
    )


def _m026_browser_session_entry_stage_columns(conn, backend: str) -> None:
    """CLAUDE.md Phase 11 sections 4, 18-19, 25: apply-entry stage and
    step-progress-confidence tracking, additive/nullable so existing rows
    (from Phase 10) default safely. `stage` mirrors
    app.applications.apply_entry.EntryStage; `step_confidence` mirrors
    app.applications.apply_entry.StepConfidence -- never invented, always
    UNKNOWN until a genuine observation sets it."""
    add_columns_if_missing(conn, backend, "browser_assist_sessions", [
        ("stage", "TEXT NOT NULL DEFAULT 'APPLICATION_ENTRY'"),
        ("step_confidence", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ("entry_detection_result", "TEXT DEFAULT ''"),
        ("apply_entry_clicked", "INTEGER NOT NULL DEFAULT 0"),
        ("reconstructed_count", "INTEGER NOT NULL DEFAULT 0"),
    ])


def _m027_workday_tenant_observations_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 11 sections 10, 13, 45: Workday behavior is tracked
    PER TENANT/SITE, never as one blanket "Workday supported" claim (this
    phase's build brief section 64 is explicit about this). Each capability
    column is nullable -- NULL means "not observed", distinct from 0/1
    (observed absent/observed present)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS workday_tenant_observations (
            {id_column},
            tenant TEXT NOT NULL,
            site TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '',
            landing_navigation INTEGER,
            login_required INTEGER,
            resume_upload INTEGER,
            profile_import INTEGER,
            multi_step INTEGER,
            custom_questions INTEGER,
            review_page INTEGER,
            confirmation_detection INTEGER,
            notes TEXT DEFAULT '',
            observed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_workday_tenant_site "
        "ON workday_tenant_observations (tenant, site)"
    )


def _m028_capability_evidence_records_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 11 sections 42-43: dated, evidence-only capability
    tracking, separate from (and feeding into a staleness check on top of)
    app.applications.browser_capability_matrix's hand-curated rows. One row
    per (provider, capability) pair; re-observing updates in place (a
    capability's evidence history isn't itself the audit trail -- the
    dated `observed_at` on the single current row is)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS capability_evidence_records (
            {id_column},
            provider TEXT NOT NULL,
            capability TEXT NOT NULL,
            verification_type TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            notes TEXT DEFAULT '',
            source_domain TEXT DEFAULT '',
            parser_version TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_capability_evidence_provider_capability "
        "ON capability_evidence_records (provider, capability)"
    )


# =============================================================================
# Phase 12: SPA/dynamic ATS flow hardening (CLAUDE.md Phase 12 durable rules).
# Every table/column below is additive -- no Phase 1-11 table is altered
# destructively, matching this module's own "no rollback needed, everything
# additive" design note above.
# =============================================================================

def _m029_browser_spa_events_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 12 sections 70-71: an append-only, structured event
    log for SPA/dynamic-flow observations (apply-control detection, trusted-
    redirect decisions, route changes, dynamic-form timeouts, iframe/shadow-
    DOM form discovery, capability revalidations). This is the single source
    both `app.applications.metrics.collect_phase12()` queries (never an
    in-memory counter -- same "live query over persisted state" principle
    every other metrics function in this project already follows) and what
    structured logging correlates by session_id/execution_id/job_id. Never a
    column for a candidate field VALUE -- only ids, stage/event/result
    labels, and durations."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS browser_spa_events (
            {id_column},
            session_id TEXT DEFAULT '',
            execution_id TEXT DEFAULT '',
            job_id INTEGER,
            provider TEXT DEFAULT '',
            tenant TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            event TEXT NOT NULL,
            result TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            duration_ms INTEGER,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_browser_spa_events_event ON browser_spa_events (event)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_browser_spa_events_session ON browser_spa_events (session_id)")


def _m030_workday_tenant_attempts_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 12 sections 18-21, 54: repeated, PER-ATTEMPT Workday
    observations -- distinct from the Phase 11 `workday_tenant_observations`
    table (which stays the single-row-per-tenant/site AGGREGATE capability
    view). This table is append-only so `app.applications.workday_tenant`
    can classify a tenant's stability (STABLE/VARIABLE/UNVERIFIED/STALE)
    from genuine repeated evidence rather than overwriting the previous
    observation -- never cherry-picking the most favorable run (CLAUDE.md
    Phase 12 section 20/54)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS workday_tenant_attempts (
            {id_column},
            tenant TEXT NOT NULL,
            site TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '',
            requisition_id TEXT DEFAULT '',
            url_initial TEXT DEFAULT '',
            url_final TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            apply_control_result TEXT DEFAULT '',
            render_time_ms INTEGER,
            fields_detected INTEGER,
            resume_upload_detected INTEGER,
            step_indicator TEXT DEFAULT '',
            result TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            observed_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workday_tenant_attempts_tenant_site "
        "ON workday_tenant_attempts (tenant, site)"
    )


def _m031_capability_evidence_repeat_count_column(conn, backend: str) -> None:
    """CLAUDE.md Phase 12 section 41: repeated REAL_BROWSER evidence should
    strengthen confidence -- tracked as a simple counter alongside the
    existing single-current-row-per-(provider,capability) model (never a
    second, parallel evidence-history table; the dated `observed_at` on the
    current row remains the audit trail, matching Phase 11's own design
    note)."""
    add_columns_if_missing(conn, backend, "capability_evidence_records", [
        ("repeat_count", "INTEGER NOT NULL DEFAULT 1"),
    ])


def _m032_browser_assist_sessions_spa_columns(conn, backend: str) -> None:
    """CLAUDE.md Phase 12 sections 8-9, 14-15, 26-27: additive, nullable/
    defaulted columns tracking whether a session's form was reached through
    an iframe or open shadow root, and the provenance of the resolved
    application URL -- never inferred after the fact, always recorded at the
    point of discovery."""
    add_columns_if_missing(conn, backend, "browser_assist_sessions", [
        ("iframe_used", "INTEGER NOT NULL DEFAULT 0"),
        ("shadow_dom_used", "INTEGER NOT NULL DEFAULT 0"),
        ("url_provenance", "TEXT DEFAULT ''"),
    ])



# =============================================================================
# Phase 13: provider resilience and real-world ATS reliability (CLAUDE.md
# Phase 13 durable rules). Every table/column below is additive, matching
# this module's own "no rollback needed" design note.
# =============================================================================

def _m033_job_identity_verifications_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 13 sections 4-5: bounded, append-only identity-check
    evidence -- one row per verification attempt (pre-upload, pre-final-
    submit, etc), never overwritten, so the full history of what was
    compared is auditable. No candidate PII -- every column is already-
    public job-posting metadata (title/company/provider/tenant/url/ids)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS job_identity_verifications (
            {id_column},
            job_id INTEGER NOT NULL,
            session_id TEXT DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            provider TEXT DEFAULT '',
            provider_job_id TEXT DEFAULT '',
            requisition_id TEXT DEFAULT '',
            stored_title TEXT DEFAULT '',
            observed_title TEXT DEFAULT '',
            stored_company TEXT DEFAULT '',
            observed_company TEXT DEFAULT '',
            stored_url TEXT DEFAULT '',
            observed_url TEXT DEFAULT '',
            tenant TEXT DEFAULT '',
            site TEXT DEFAULT '',
            signals_compared TEXT DEFAULT '',
            signals_matched TEXT DEFAULT '',
            signals_mismatched TEXT DEFAULT '',
            result TEXT NOT NULL,
            reason TEXT DEFAULT '',
            parser_version TEXT DEFAULT '',
            verified_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_identity_verifications_job "
                 "ON job_identity_verifications (job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_identity_verifications_result "
                 "ON job_identity_verifications (result)")


def _m034_application_provider_health_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 13 sections 11-12: application/browser-assist provider
    health, tracked separately from discovery health (app.workers.circuit's
    `provider_circuit_state`) and from application SUBMISSION circuit health
    (app.applications.circuit's `application_provider_circuit_state`) -- a
    third, distinct concern: is this provider's real-browser ASSIST flow
    (form discovery/fill, not submission) currently trustworthy. Keyed by
    (provider, tenant, site) with tenant/site defaulting to '' for providers
    with no tenant concept."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_provider_health (
            {id_column},
            provider TEXT NOT NULL,
            tenant TEXT NOT NULL DEFAULT '',
            site TEXT NOT NULL DEFAULT '',
            last_success TEXT,
            last_failure TEXT,
            last_live_validation TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            schema_drift_count INTEGER NOT NULL DEFAULT 0,
            captcha_observed INTEGER NOT NULL DEFAULT 0,
            auth_gate_observed INTEGER NOT NULL DEFAULT 0,
            form_verified INTEGER NOT NULL DEFAULT 0,
            form_fingerprint TEXT DEFAULT '',
            parser_version TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_application_provider_health_key "
        "ON application_provider_health (provider, tenant, site)"
    )


def _m035_application_checkpoints_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 13 sections 37-38: append-only checkpoint log for a
    browser-assist session's meaningful REVERSIBLE stages -- distinct from
    `browser_assist_sessions.status/stage` (the single current-state row):
    this is the ordered history a reconstruction can be reasoned about
    against, and what a future operator/doctor check inspects for
    consistency (e.g. a FILE_READY checkpoint recorded after a
    READY_FOR_FINAL_SUBMIT one would be an ordering anomaly). Recording a
    checkpoint never itself performs any recovery action."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_checkpoints (
            {id_column},
            session_id TEXT NOT NULL,
            job_id INTEGER,
            execution_id TEXT DEFAULT '',
            checkpoint TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_checkpoints_session "
                 "ON application_checkpoints (session_id)")


def _m036_provider_canary_runs_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 13 sections 13-14, 56: safe, read-only application-flow
    canary runs -- never fills candidate PII, never uploads a resume, never
    clicks a final submit or submits an application. One row per canary
    execution against one configured public URL."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS provider_canary_runs (
            {id_column},
            provider TEXT NOT NULL DEFAULT '',
            tenant TEXT DEFAULT '',
            site TEXT DEFAULT '',
            url TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 0,
            captcha_detected INTEGER NOT NULL DEFAULT 0,
            login_detected INTEGER NOT NULL DEFAULT 0,
            apply_entry_found INTEGER NOT NULL DEFAULT 0,
            apply_entry_followed INTEGER NOT NULL DEFAULT 0,
            form_found INTEGER NOT NULL DEFAULT 0,
            upload_control_found INTEGER NOT NULL DEFAULT 0,
            final_submit_found INTEGER NOT NULL DEFAULT 0,
            step_hint TEXT DEFAULT '',
            error TEXT DEFAULT '',
            ran_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_provider_canary_runs_provider "
                 "ON provider_canary_runs (provider)")


def _m037_jobs_resume_jd_fingerprint_column(conn, backend: str) -> None:
    """CLAUDE.md Phase 13 sections 43-45: records which JD fingerprint the
    currently-generated resume artifact was built against, so a pre-upload
    check can detect a JD that materially changed AFTER the resume was
    generated (`app.applications.resume_integrity`) without re-diffing JD
    text on every check -- reuses the same `jd_sponsorship_fingerprint`
    value Phase 7 already computes per job, never a second, parallel
    fingerprinting scheme."""
    add_columns_if_missing(conn, backend, "jobs", [
        ("resume_jd_fingerprint", "TEXT DEFAULT ''"),
    ])


def _m038_confirmation_evidence_column(conn, backend: str) -> None:
    """CLAUDE.md Phase 13 sections 49-51: records the graded evidence
    STRENGTH (STRONG/MODERATE/WEAK/NONE) behind a browser-assist session's
    confirmation, alongside the existing raw confirmation_id/url/fingerprint
    columns -- never itself changes what counts as `confirmed`, only makes
    the strength of that evidence inspectable after the fact."""
    add_columns_if_missing(conn, backend, "browser_assist_sessions", [
        ("confirmation_evidence_strength", "TEXT DEFAULT ''"),
    ])


def _m039_jd_analyses_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 14 sections 3, 36, 70: cached, per-(job, JD-fingerprint)
    JD analysis result -- never recomputed on every dashboard load (section
    55). A JD-text change produces a different jd_fingerprint and a new row
    rather than overwriting the old one, so history stays inspectable."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS jd_analyses (
            {id_column},
            job_id INTEGER NOT NULL,
            jd_fingerprint TEXT NOT NULL,
            analyzer_version TEXT NOT NULL DEFAULT '',
            job_title TEXT DEFAULT '',
            seniority TEXT DEFAULT '',
            required_years REAL,
            domain_signals TEXT DEFAULT '[]',
            responsibilities TEXT DEFAULT '[]',
            education_requirements TEXT DEFAULT '[]',
            certification_requirements TEXT DEFAULT '[]',
            sponsorship_language_present INTEGER NOT NULL DEFAULT 0,
            salary_mentioned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jd_analyses_job_fingerprint "
        "ON jd_analyses (job_id, jd_fingerprint)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jd_analyses_job ON jd_analyses (job_id)")


def _m040_jd_requirements_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 14 sections 3-4, 9-11, 61: one row per extracted JD
    requirement item, linked to its parent jd_analyses row. Never mutated in
    place after creation -- a re-analysis creates a new jd_analyses row (and
    a fresh set of these) rather than editing an existing requirement."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS jd_requirements (
            {id_column},
            jd_analysis_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            normalized_value TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            priority TEXT NOT NULL,
            evidence_span TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            negated INTEGER NOT NULL DEFAULT 0,
            conditional INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jd_requirements_analysis ON jd_requirements (jd_analysis_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jd_requirements_category ON jd_requirements (category)")


def _m041_resume_variants_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 14 sections 33, 37-39, 58-59, 72: one row per
    generated, job-specific resume artifact. `current` (1 while this is the
    job's live/latest variant, 0 once superseded) backs a partial unique
    index -- the same "one active thing per job" pattern
    application_executions/browser_assist_sessions already use (CLAUDE.md
    Phase 8/10) -- so two workers racing to generate for the same job can
    never both leave a variant marked current. A SECOND unique index on
    (job_id, jd_fingerprint, profile_version, optimizer_version) is the
    actual idempotency guard (section 58): the identical input never
    produces two rows, and a concurrent duplicate INSERT is rejected by the
    database itself rather than by an application-level check-then-insert."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS resume_variants (
            {id_column},
            variant_id TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            jd_fingerprint TEXT NOT NULL,
            profile_version TEXT NOT NULL,
            optimizer_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'GENERATING',
            current INTEGER NOT NULL DEFAULT 1,
            resume_docx_path TEXT DEFAULT '',
            resume_pdf_path TEXT DEFAULT '',
            resume_txt_path TEXT DEFAULT '',
            resume_artifact_hash TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_resume_variants_identity "
        "ON resume_variants (job_id, jd_fingerprint, profile_version, optimizer_version)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_resume_variants_job_current "
        "ON resume_variants (job_id) WHERE current = 1"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resume_variants_job ON resume_variants (job_id)")


def _m042_resume_quality_reports_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 14 sections 2-3, 10-14, 33, 46: one row per resume
    variant. Summary columns (alignment_label, internal_alignment_score,
    required coverage counts, ats_parseability) are indexed/queryable
    without JSON-parsing for dashboard performance (section 55); the full
    itemized diagnostic (never a fake universal score) lives in report_json."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS resume_quality_reports (
            {id_column},
            variant_id TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            jd_fingerprint TEXT NOT NULL,
            resume_artifact_hash TEXT DEFAULT '',
            required_total INTEGER DEFAULT 0,
            required_matched INTEGER DEFAULT 0,
            required_transferable INTEGER DEFAULT 0,
            preferred_total INTEGER DEFAULT 0,
            preferred_matched INTEGER DEFAULT 0,
            ats_parseability TEXT DEFAULT '',
            alignment_label TEXT DEFAULT '',
            internal_alignment_score REAL DEFAULT 0.0,
            claim_check_passed INTEGER NOT NULL DEFAULT 0,
            optimizer_version TEXT NOT NULL DEFAULT '',
            quality_version TEXT NOT NULL DEFAULT '',
            report_json TEXT NOT NULL DEFAULT '{{}}',
            generated_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resume_quality_job ON resume_quality_reports (job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resume_quality_alignment ON resume_quality_reports (alignment_label)")


def _m043_resume_evidence_links_table(conn, backend: str) -> None:
    """CLAUDE.md Phase 14 sections 6, 9, 60-61: per-requirement match
    evidence, the backing for the "claim provenance" / "unsupported
    requirements" UI -- one row per JD requirement, showing exactly which
    verified evidence (if any) backed the match decision, or that nothing
    did (MISSING/UNSUPPORTED, never hidden)."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS resume_evidence_links (
            {id_column},
            variant_id TEXT NOT NULL,
            requirement_text TEXT NOT NULL,
            requirement_category TEXT NOT NULL,
            requirement_priority TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_ids TEXT DEFAULT '[]',
            explanation TEXT DEFAULT ''
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resume_evidence_links_variant ON resume_evidence_links (variant_id)")


def _m044_agent_run_state_table(conn, backend: str) -> None:
    """One-click autonomous agent orchestrator: durable desired/actual run
    state (STOPPED/STARTING/RUNNING/PAUSED/STOPPING/ERROR) so a dashboard
    refresh or a process restart never loses whether the user asked the
    agent to be running -- see app/agent/orchestrator.py. Single-row table
    (id always 1); never a column for a secret/token."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS agent_run_state (
            {id_column},
            desired_state TEXT NOT NULL DEFAULT 'STOPPED',
            actual_state TEXT NOT NULL DEFAULT 'STOPPED',
            test_mode INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '',
            started_at TEXT,
            stopped_at TEXT,
            start_count INTEGER NOT NULL DEFAULT 0,
            stop_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO agent_run_state (id, desired_state, actual_state, updated_at) "
        "SELECT 1, 'STOPPED', 'STOPPED', ? WHERE NOT EXISTS (SELECT 1 FROM agent_run_state WHERE id = 1)",
        (utcnow(),),
    )


def _m045_agent_cycle_log_table(conn, backend: str) -> None:
    """Append-only per-cycle orchestrator run log -- the durable source for
    the agent_* Prometheus counters (app.agent.metrics), matching this
    project's existing 'never an in-process counter, always a live query
    over persisted state' convention (see app/observability/metrics.py's own
    module docstring). One row per completed orchestrator cycle."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS agent_cycle_log (
            {id_column},
            started_at TEXT NOT NULL,
            finished_at TEXT,
            test_mode INTEGER NOT NULL DEFAULT 0,
            jobs_processed INTEGER NOT NULL DEFAULT 0,
            resumes_generated INTEGER NOT NULL DEFAULT 0,
            one_page_success INTEGER NOT NULL DEFAULT 0,
            one_page_overflow INTEGER NOT NULL DEFAULT 0,
            one_page_compression_events INTEGER NOT NULL DEFAULT 0,
            applications_prepared INTEGER NOT NULL DEFAULT 0,
            applications_submitted INTEGER NOT NULL DEFAULT 0,
            needs_user_action INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            detail TEXT DEFAULT '{{}}'
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_cycle_log_started ON agent_cycle_log (started_at)")


def _m046_resume_variants_one_page_columns(conn, backend: str) -> None:
    """One-page resume hard output contract (CLAUDE.md one-click-agent
    section 7-8): records the final rendered PDF page count and the bounded
    compression steps actually applied, alongside the existing status column
    -- ResumeVariantStatus.REVIEW_REQUIRED is reused (it was defined but
    never set in Phase 14) for 'one page could not be safely achieved',
    never a fabricated tiny/unreadable render."""
    add_columns_if_missing(conn, backend, "resume_variants", [
        ("page_count", "INTEGER"),
        ("compression_steps_applied", "INTEGER NOT NULL DEFAULT 0"),
        ("compression_log", "TEXT DEFAULT '[]'"),
    ])


def _m047_jobs_promoted_resume_columns(conn, backend: str) -> None:
    """Tracks which resume_variants row (if any) the orchestrator promoted
    to be this job's PRIMARY resume artifact (jobs.resume_docx_path etc) --
    lets the dashboard/doctor confirm 'the resume actually used for this
    application is the one-page-verified JD-tailored variant', distinct from
    resume_variants.current (which only tracks the optimizer's own latest
    variant, independent of whether it was ever promoted)."""
    add_columns_if_missing(conn, backend, "jobs", [
        ("promoted_resume_variant_id", "TEXT DEFAULT ''"),
    ])


def _m048_agent_run_state_progress_columns(conn, backend: str) -> None:
    """Fixes the real defect 'Agent Status = RUNNING but Last cycle = never,
    Next cycle = pending': the orchestrator's own loop (app/agent/orchestrator.py)
    previously had nowhere durable to record that a cycle was in progress or
    when the next one is due -- app.agent.state's next_cycle_at is the OLDER,
    separate legacy scheduler's field and was never written by the new
    orchestrator. These columns let the orchestrator own its own progress
    state directly on agent_run_state, survives a restart same as every other
    field on this table, and lets the dashboard show real in-progress status
    instead of a misleading blank."""
    add_columns_if_missing(conn, backend, "agent_run_state", [
        ("run_id", "TEXT DEFAULT ''"),
        ("cycle_number", "INTEGER NOT NULL DEFAULT 0"),
        ("last_cycle_started_at", "TEXT"),
        ("last_cycle_finished_at", "TEXT"),
        ("next_cycle_at", "TEXT"),
        ("heartbeat_at", "TEXT"),
        ("current_stage", "TEXT DEFAULT ''"),
        ("current_job_label", "TEXT DEFAULT ''"),
    ])


def _m049_jobs_test_fixture_column(conn, backend: str) -> None:
    """CLAUDE.md one-click-agent section 68/CLAUDE.md 'CURRENT REAL DASHBOARD
    DEFECTS' item 6: synthetic/test rows (TEST MODE's mock_ats fixture, any
    legacy Acme/manually-ingested demo rows) must never masquerade as real
    production opportunities on the default dashboard. Rather than infer this
    from provider name string-matching scattered across call sites, a single
    explicit column is set once at ingest time (app.pipeline) and read
    everywhere the dashboard/summary/needs-action queries filter real-mode
    data -- see app.pipeline_dashboard.REAL_MODE_JOB_FILTER."""
    add_columns_if_missing(conn, backend, "jobs", [
        ("is_test_fixture", "INTEGER NOT NULL DEFAULT 0"),
    ])
    conn.execute("UPDATE jobs SET is_test_fixture = 1 WHERE provider = 'mock_ats'")


def _m050_agent_activity_log_table(conn, backend: str) -> None:
    """CLAUDE.md production-v2 dashboard defect 7 / one-click-agent section 38:
    the orchestrator's own lifecycle/cycle events ('Agent started', 'Discovery
    cycle started', 'Found N jobs', 'Agent stopped', 'Error / recovered', ...)
    previously had nowhere to persist -- build_recent_activity() only ever
    read job-level application_state_history/application_audit_log rows, so
    the Live Activity feed looked stale while the agent was genuinely running
    a cycle with nothing yet to report at the job level. Append-only,
    company/title-free (these are agent-level, not job-level, events) --
    trimmed to the most recent 500 rows on insert so a long-running local
    single-user process never grows this table unbounded."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS agent_activity_log (
            {id_column},
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT DEFAULT ''
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_activity_log_ts ON agent_activity_log (ts)")


def _m051_agent_run_state_lease_columns(conn, backend: str) -> None:
    """autonomous-core-v3 hardening: single-orchestrator-guarantee safety net
    (see app/config.py's AGENT_ORCHESTRATOR_LEASE_SECONDS docstring). Adds an
    owning instance_id and a lease expiry to the existing single-row
    agent_run_state -- claimed with the same atomic `UPDATE ... WHERE
    (unowned OR lease-expired OR already-mine)` pattern this project's
    worker/application queues already use (app.workers.leasing /
    app.applications.queue), so correctness comes from the database's own
    single-writer serialization, never an application-level lock. A crashed
    lease holder is recovered purely by the lease expiring, never a
    heartbeat-based liveness check."""
    add_columns_if_missing(conn, backend, "agent_run_state", [
        ("instance_id", "TEXT DEFAULT ''"),
        ("lease_expires_at", "TEXT"),
    ])


def _m052_workday_tenant_dynamic_validation_column(conn, backend: str) -> None:
    """Workday/SmartRecruiters/Workable browser-assist hardening (2026-08-22):
    adds `dynamic_validation` to `workday_tenant_observations`, matching the
    new key appended to app.applications.workday_tenant.CAPABILITY_KEYS --
    does this tenant's real form genuinely block a Next/Continue click with
    inline validation when a required field is left empty. Nullable, same
    as every other capability column here: NULL means "not observed"."""
    add_columns_if_missing(conn, backend, "workday_tenant_observations", [
        ("dynamic_validation", "INTEGER"),
    ])


def _m053_app_settings_table(conn, backend: str) -> None:
    """Premium UI Settings page: a small, allowlisted key/value override
    store for runtime-mutable, non-dangerous tuning knobs.

    Safety-relevant flags such as application execution, auto-submit, and
    browser-assist enablement remain environment-controlled and are not
    stored here.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def _m054_application_approvals_table(conn, backend: str) -> None:
    """Approval-gated-autonomy-v1: the durable, per-application APPROVE &
    APPLY record (see app.applications.approval, docs/approval-gated-
    autonomy.md). APPEND-ONLY -- mirrors this project's existing
    sponsorship_decisions/capability_evidence pattern (never UPDATEd once
    written; a re-approval after invalidation always inserts a new row).
    Approval validity is always LIVE-recomputed by comparing the latest
    row's stored fingerprints against the job/execution's CURRENT
    fingerprints (app.applications.approval.is_current_valid) -- never a
    stored boolean that could silently go stale. No secrets/passwords/
    tokens are ever columns here, matching application_executions' own
    'no secrets' rule."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_approvals (
            {id_column},
            approval_id TEXT NOT NULL UNIQUE,
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            provider TEXT DEFAULT '',
            approved_at TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT 'user',
            job_identity_fingerprint TEXT DEFAULT '',
            jd_fingerprint TEXT DEFAULT '',
            resume_variant_id TEXT DEFAULT '',
            resume_fingerprint TEXT DEFAULT '',
            answers_version INTEGER DEFAULT 0,
            profile_fingerprint TEXT DEFAULT '',
            form_fingerprint TEXT DEFAULT '',
            sponsorship_status_at_approval TEXT DEFAULT '',
            employment_type_at_approval TEXT DEFAULT '',
            submission_capability TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_approvals_execution ON application_approvals (execution_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_approvals_job ON application_approvals (job_id)")


def _m055_application_receipts_table(conn, backend: str) -> None:
    """Provider Post-Approval Execution V1: a durable, append-only receipt
    row recorded the moment (and only when) genuine confirmation evidence
    marks an execution APPLIED -- from either confirmation path this project
    has (`app.applications.executor.process_execution`'s headless
    provider.submit()+verify_confirmation() path, today only reachable for
    the deterministic `mock_ats` fixture; or
    `app.applications.browser_assist.attempt_user_submit_reconciliation`'s
    browser-observed manual-submit path). Distinct from the existing
    confirmation_id/confirmation_url/confirmation_text_fingerprint columns
    already on application_executions/browser_assist_sessions (those are the
    single CURRENT confirmation state for one row) -- this table is the
    durable, provider-labeled evidence record itself, the actual "receipt"
    the build brief asks for, safe to list/export independent of whatever
    happens to either source row later. Never stores a raw cookie/token, and
    `sanitized_url`/`raw_message_fingerprint` are exactly what the source
    already sanitized/fingerprinted -- this table adds no new sensitive
    surface. One append-only row per confirmed submission; a re-confirmation
    (should one ever legitimately happen for the same execution) is a new
    row, never an UPDATE, matching this project's sponsorship_decisions/
    capability_evidence/application_approvals append-only convention."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_receipts (
            {id_column},
            receipt_id TEXT NOT NULL UNIQUE,
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            submitted_via TEXT NOT NULL DEFAULT '',
            confirmation_id TEXT DEFAULT '',
            sanitized_url TEXT DEFAULT '',
            evidence_strength TEXT NOT NULL DEFAULT 'NONE',
            raw_message_fingerprint TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            approval_id TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_receipts_execution ON application_receipts (execution_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_receipts_job ON application_receipts (job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_receipts_provider ON application_receipts (provider)")


def _m056_application_blockers_table(conn, backend: str) -> None:
    """Application-lifecycle-exception-resume-v1: the durable, first-class
    blocker record the feature is built around -- distinct from the
    existing live-derived views (app.applications.product_state/cta), which
    stay unchanged and unmodified. Only one row per execution may ever be
    unresolved at a time (the partial unique index below), mirroring this
    project's existing "one active thing per key" pattern
    (application_executions/browser_assist_sessions/resume_variants) --
    the actual atomic idempotency/concurrency guard for raise_blocker()'s
    insert-or-return-existing behavior, never a read-then-write check.
    Append-only in spirit: a blocker is resolved in place (resolved_at set)
    rather than deleted, so history survives; a fresh occurrence of the same
    condition after resolution is always a NEW row, never a reused one."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_blockers (
            {id_column},
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            blocker_code TEXT NOT NULL,
            blocker_class TEXT NOT NULL,
            human_title TEXT NOT NULL DEFAULT '',
            human_message TEXT NOT NULL DEFAULT '',
            required_action TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            resume_checkpoint TEXT DEFAULT '',
            attempt_id TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_note TEXT DEFAULT ''
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_application_blockers_execution_active "
        "ON application_blockers (execution_id) WHERE resolved_at IS NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_blockers_job ON application_blockers (job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_blockers_execution ON application_blockers (execution_id)")


def _m057_application_document_bindings_table(conn, backend: str) -> None:
    """Real Provider Execution V1: durable proof of WHICH document artifact
    was placed into WHICH provider form field, for WHICH job.

    The project already hashed a job's resume artifact in two places
    (`application_executions.resume_artifact_hash`,
    `browser_assist_sessions.resume_artifact_hash`), but neither records the
    binding itself -- the (job, resume variant, artifact hash, filename,
    provider upload target, moment) tuple that proves the file actually
    handed to a real ATS field was this job's own tailored resume and not a
    silently substituted one. That tuple is what this table stores, and it
    is APPEND-ONLY: every upload attempt is its own row, so a re-upload
    after a form change leaves both observations intact rather than
    overwriting history (mirroring application_approvals/
    application_receipts/capability_evidence's existing append-only
    convention).

    Deliberately NOT unique on (execution_id, provider_field_id): a genuinely
    multi-step form can legitimately present the same upload target twice
    across two attempts, and a durable audit log must record both.
    `verified` is the honest outcome flag -- 0 means the binding was prepared
    but the upload was not confirmed to have landed, never silently dropped.
    Booleans stay INTEGER in both backends per CLAUDE.md's Phase 6 rule."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS application_document_bindings (
            {id_column},
            binding_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            execution_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            document_kind TEXT NOT NULL,
            artifact_path TEXT NOT NULL DEFAULT '',
            artifact_filename TEXT NOT NULL DEFAULT '',
            artifact_sha256 TEXT NOT NULL DEFAULT '',
            resume_variant_id TEXT NOT NULL DEFAULT '',
            provider_field_id TEXT NOT NULL DEFAULT '',
            provider_field_label TEXT NOT NULL DEFAULT '',
            checkpoint TEXT NOT NULL DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_application_document_bindings_binding_id "
        "ON application_document_bindings (binding_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_application_document_bindings_job "
        "ON application_document_bindings (job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_application_document_bindings_execution "
        "ON application_document_bindings (execution_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_application_document_bindings_session "
        "ON application_document_bindings (session_id)"
    )


def _m058_greenhouse_submit_claims_table(conn, backend: str) -> None:
    """Greenhouse Verified Submission Contract V1: the submit-once execution
    claim for the (disabled-by-default) real Greenhouse canary submission
    engine (`app.applications.greenhouse_submit_engine`).

    Exactly one row per execution (UNIQUE execution_id). `submit_attempted`
    starts at 0 and is flipped to 1 by a single atomic
    `UPDATE ... WHERE submit_attempted = 0` immediately before -- and only
    immediately before -- the engine physically clicks a real submit
    control, mirroring this project's existing atomic-claim idiom
    (`app.applications.approval._claim_ready_execution`,
    `app.workers.leasing`, `app.applications.queue`) rather than a
    read-then-write check. Once flipped, no code path in this project ever
    flips it back or re-attempts a click for the same execution -- a second
    call always observes `submit_attempted = 1` and refuses before ever
    opening a browser, which is the actual, physical "at most one submit
    action per execution" guarantee this table exists to provide (on top of,
    never instead of, `application_executions(job_id) WHERE active = 1`'s
    existing coarser duplicate-execution guard). `outcome` records the
    typed SubmitOutcome (CONFIRMED/REJECTED/BLOCKED/
    SUBMISSION_STATUS_UNKNOWN) once known; a row that was inserted but never
    reached the claim (contract not ready) has `submit_attempted = 0` and
    `outcome` empty, so a genuinely fresh, uncontended future attempt for a
    NEW execution of the same job is never blocked by an old row that never
    actually attempted anything."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS greenhouse_submit_claims (
            {id_column},
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT '',
            claimed_by TEXT NOT NULL DEFAULT '',
            submit_attempted INTEGER NOT NULL DEFAULT 0,
            submit_attempted_at TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            outcome_detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_greenhouse_submit_claims_execution "
        "ON greenhouse_submit_claims (execution_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_greenhouse_submit_claims_job ON greenhouse_submit_claims (job_id)"
    )


def _m059_notifications_table(conn, backend: str) -> None:
    """One-click-application-experience-v1 (CLAUDE.md section J): calm,
    in-app notifications for meaningful events only (Needs You, application
    submitted, application status unknown, agent stopped unexpectedly, daily
    limit reached, serious health issue). This table is purely a durable
    read/unread record with a dedupe key -- it introduces no new business
    logic of its own; every notify() call site is a thin, best-effort
    observer bolted onto an already-existing, narrow choke point (
    app.applications.blockers.raise_blocker, app.applications.receipts.
    record_receipt, the rate-limit block in app.applications.executor, and
    app.agent.orchestrator's own crash/lease-loss handling) -- never a
    second copy of any state those modules already own.

    Dedup (spec section J: "meaningful notifications... must dedupe") is a
    simple, best-effort check-then-insert keyed on `dedupe_key`: a new
    notification for the same dedupe_key is skipped whenever an existing
    UNREAD one already carries it, so a repeatedly-retried blocker or a
    still-unresolved daily limit never floods the feed. This is
    intentionally not the atomic partial-unique-index pattern this project
    uses for safety-critical claims (leases/executions/blockers) -- a
    notification is calm UX polish, not a correctness invariant, so a rare
    race producing one extra duplicate row is harmless and never worth a
    second locking mechanism."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS notifications (
            {id_column},
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            job_id INTEGER,
            execution_id TEXT,
            dedupe_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            read_at TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_dedupe ON notifications (dedupe_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications (read_at)")


def _m060_recruiter_updates_table(conn, backend: str) -> None:
    """Tsenta Remaining-Gaps Closure V2, section 6: durable post-application
    "recruiter/contact update" concepts (confirmation observed, interview
    update received, rejection update received, or a bare status check-in),
    each tied to a job/execution and carrying a `source` of either 'manual'
    (the candidate telling the product what they heard) or 'mailbox' (a
    future connected-mailbox adapter -- app.applications.recruiter_
    communication.MailboxAdapter -- ships only a truthful NullMailboxAdapter
    today; this table's schema does not change when a real one is ever
    added, only `source`/`raw_reference` start getting populated by it).
    This is a durable HISTORY record, never itself a trigger for
    ExecutionStatus/receipts -- app.applications.handoff.record_manual_outcome
    remains the only path that can mark an execution APPLIED/terminal;
    recording a recruiter update here never does that on its own."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS recruiter_updates (
            {id_column},
            job_id INTEGER NOT NULL,
            execution_id TEXT NOT NULL DEFAULT '',
            update_type TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            subject TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            raw_reference TEXT NOT NULL DEFAULT '',
            needs_you INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recruiter_updates_job ON recruiter_updates (job_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recruiter_updates_execution ON recruiter_updates (execution_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recruiter_updates_created ON recruiter_updates (created_at)")


def _m061_jobs_employment_type_page_evidence_columns(conn, backend: str) -> None:
    """Employment Type Evidence Hardening V1: persists the RAW third-party
    evidence a posting's real public page's schema.org JobPosting JSON-LD
    `employmentType` field carried, the last time it was checked -- never a
    cached FINAL decision. app.matching.employment_type.
    resolve_employment_type_evidence() still recomputes the actual
    FULL_TIME/CONTRACT/.../UNKNOWN decision live from this raw fact plus the
    job's existing `employment_type` (provider-structured) and title/
    description (JD text) columns every time it's called -- matching this
    project's existing 'persist raw evidence, never cache a live decision'
    convention (see employer_sponsorship_evidence). `_checked_at` distinguishes
    'never checked' (empty) from 'checked, found nothing' (set but raw
    column empty) so a doctor/report can tell the difference; a page-fetch
    failure or a page with no JSON-LD is honestly recorded as 'checked, no
    signal', never silently retried forever nor treated as a negative
    signal."""
    add_columns_if_missing(conn, backend, "jobs", [
        ("employment_type_page_evidence_raw", "TEXT DEFAULT ''"),
        ("employment_type_page_evidence_checked_at", "TEXT DEFAULT ''"),
    ])


def _m062_provider_submit_claims_table(conn, backend: str) -> None:
    """Canary Candidate Pool Expansion + Multi-Provider Readiness V1: the
    provider-parameterized generalization of migration 58's
    `greenhouse_submit_claims` -- the same submit-once physical guarantee
    (one row per execution, `submit_attempted` flipped 0->1 by exactly one
    atomic `UPDATE ... WHERE submit_attempted = 0`), now keyed by
    `(provider, execution_id)` so a future Lever/Ashby/Workable submit
    engine can reuse the identical claim idiom without a new table per
    provider. `greenhouse_submit_claims` itself is UNCHANGED and remains the
    claim ledger for `app.applications.greenhouse_submit_engine` -- this
    table is for `app.applications.provider_submit_claim`, consulted only by
    `app.applications.provider_submit_contract` (read-only) and any future
    non-Greenhouse submit engine (write, once one is genuinely built and
    tested). No submit engine exists yet for any provider this table
    covers; this migration adds the ledger ahead of that work so the
    readiness contract can honestly report claim state today."""
    id_column = "id BIGSERIAL PRIMARY KEY" if backend == "postgres" else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS provider_submit_claims (
            {id_column},
            provider TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT '',
            claimed_by TEXT NOT NULL DEFAULT '',
            submit_attempted INTEGER NOT NULL DEFAULT 0,
            submit_attempted_at TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            outcome_detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_submit_claims_provider_execution "
        "ON provider_submit_claims (provider, execution_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_provider_submit_claims_job ON provider_submit_claims (job_id)"
    )


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
    (21, "phase9_application_attempts_table", _m021_application_attempts_table),
    (22, "phase9_application_provider_circuit_state_table", _m022_application_provider_circuit_state_table),
    (23, "phase9_mock_ats_server_records_table", _m023_mock_ats_server_records_table),
    (24, "phase9_jobs_application_worker_columns", _m024_jobs_application_worker_columns),
    (25, "phase10_browser_assist_sessions_table", _m025_browser_assist_sessions_table),
    (26, "phase11_browser_session_entry_stage_columns", _m026_browser_session_entry_stage_columns),
    (27, "phase11_workday_tenant_observations_table", _m027_workday_tenant_observations_table),
    (28, "phase11_capability_evidence_records_table", _m028_capability_evidence_records_table),
    (29, "phase12_browser_spa_events_table", _m029_browser_spa_events_table),
    (30, "phase12_workday_tenant_attempts_table", _m030_workday_tenant_attempts_table),
    (31, "phase12_capability_evidence_repeat_count_column", _m031_capability_evidence_repeat_count_column),
    (32, "phase12_browser_assist_sessions_spa_columns", _m032_browser_assist_sessions_spa_columns),
    (33, "phase13_job_identity_verifications_table", _m033_job_identity_verifications_table),
    (34, "phase13_application_provider_health_table", _m034_application_provider_health_table),
    (35, "phase13_application_checkpoints_table", _m035_application_checkpoints_table),
    (36, "phase13_provider_canary_runs_table", _m036_provider_canary_runs_table),
    (37, "phase13_jobs_resume_jd_fingerprint_column", _m037_jobs_resume_jd_fingerprint_column),
    (38, "phase13_confirmation_evidence_column", _m038_confirmation_evidence_column),
    (39, "phase14_jd_analyses_table", _m039_jd_analyses_table),
    (40, "phase14_jd_requirements_table", _m040_jd_requirements_table),
    (41, "phase14_resume_variants_table", _m041_resume_variants_table),
    (42, "phase14_resume_quality_reports_table", _m042_resume_quality_reports_table),
    (43, "phase14_resume_evidence_links_table", _m043_resume_evidence_links_table),
    (44, "agent_run_state_table", _m044_agent_run_state_table),
    (45, "agent_cycle_log_table", _m045_agent_cycle_log_table),
    (46, "resume_variants_one_page_columns", _m046_resume_variants_one_page_columns),
    (47, "jobs_promoted_resume_columns", _m047_jobs_promoted_resume_columns),
    (48, "agent_run_state_progress_columns", _m048_agent_run_state_progress_columns),
    (49, "jobs_test_fixture_column", _m049_jobs_test_fixture_column),
    (50, "agent_activity_log_table", _m050_agent_activity_log_table),
    (51, "agent_run_state_lease_columns", _m051_agent_run_state_lease_columns),
    (52, "workday_tenant_dynamic_validation_column", _m052_workday_tenant_dynamic_validation_column),
    (53, "app_settings_table", _m053_app_settings_table),
    (54, "application_approvals_table", _m054_application_approvals_table),
    (55, "application_receipts_table", _m055_application_receipts_table),
    (56, "application_blockers_table", _m056_application_blockers_table),
    (57, "application_document_bindings_table", _m057_application_document_bindings_table),
    (58, "greenhouse_submit_claims_table", _m058_greenhouse_submit_claims_table),
    (59, "notifications_table", _m059_notifications_table),
    (60, "recruiter_updates_table", _m060_recruiter_updates_table),
    (61, "jobs_employment_type_page_evidence_columns", _m061_jobs_employment_type_page_evidence_columns),
    (62, "provider_submit_claims_table", _m062_provider_submit_claims_table),
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
