"""Real Provider Execution V1 against REAL PostgreSQL: migration 57
(`application_document_bindings`) clean bootstrap, idempotent re-run, the
INTEGER-boolean coercion rule, and the doctor's new cross-table checks under
the Postgres backend.

Marked `postgres` -- skipped automatically if `pgserver` isn't installed
(see tests/conftest.py::postgres_url).
"""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_clean_bootstrap_reaches_schema_version_57(pg_db):
    import app.migrations as migrations

    with pg_db.db_session() as conn:
        assert migrations.current_db_version(conn) >= 57
        assert migrations.is_compatible(conn)


def test_migration_57_idempotent_rerun(pg_db):
    import app.migrations as migrations

    with pg_db.db_session() as conn:
        assert migrations.run_pending(conn, "postgres") == []


def test_document_bindings_table_and_indexes_exist(pg_db):
    with pg_db.db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_name = ?",
            ("application_document_bindings",),
        ).fetchone()
        assert row["n"] == 1
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = ?",
            ("application_document_bindings",),
        ).fetchall()
        names = {r["indexname"] for r in indexes}
        assert "idx_application_document_bindings_binding_id" in names
        assert "idx_application_document_bindings_job" in names


def test_binding_insert_and_read_round_trip(pg_db, tmp_path):
    from app.applications import document_binding

    artifact = tmp_path / "77" / "resume.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"%PDF-1.4 pg round trip")

    row = document_binding.record_binding(
        job_id=77, document_kind=document_binding.DocumentKind.RESUME, artifact_path=str(artifact),
        provider="greenhouse", execution_id="exec_pg_b1", session_id="sess_pg_b1",
        resume_variant_id="var_pg", provider_field_id="resume", provider_field_label="Resume/CV",
        checkpoint="browser_assist:form_fingerprint=pgpg", verified=True,
    )
    assert row["binding_id"].startswith("docb_")
    assert row["artifact_filename"] == "resume.pdf"
    assert row["artifact_sha256"]

    assert len(document_binding.list_bindings_for_job(77)) == 1
    assert len(document_binding.list_bindings_for_execution("exec_pg_b1")) == 1
    assert len(document_binding.list_bindings_for_session("sess_pg_b1")) == 1
    latest = document_binding.latest_binding(77, document_binding.DocumentKind.RESUME)
    assert latest["binding_id"] == row["binding_id"]


def test_verified_boolean_is_coerced_to_integer_for_postgres(pg_db, tmp_path):
    """CLAUDE.md Phase 9's boolean-coercion rule: psycopg maps a Python bool
    to Postgres `boolean`, which conflicts with this INTEGER column. SQLite
    would silently accept the bool, so this is the test that actually proves
    the coercion."""
    from app.applications import document_binding

    artifact = tmp_path / "78" / "resume.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"%PDF-1.4 pg bool")

    true_row = document_binding.record_binding(
        job_id=78, document_kind=document_binding.DocumentKind.RESUME, artifact_path=str(artifact),
        verified=True)
    false_row = document_binding.record_binding(
        job_id=78, document_kind=document_binding.DocumentKind.COVER_LETTER, artifact_path=str(artifact),
        verified=False)
    assert true_row["verified"] == 1
    assert false_row["verified"] == 0
    assert isinstance(true_row["verified"], int) and not isinstance(true_row["verified"], bool)


def test_bindings_are_append_only_under_postgres(pg_db, tmp_path):
    from app.applications import document_binding

    artifact = tmp_path / "79" / "resume.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"%PDF-1.4 pg append only")

    ids = {
        document_binding.record_binding(
            job_id=79, document_kind=document_binding.DocumentKind.RESUME,
            artifact_path=str(artifact), provider_field_id="resume")["binding_id"]
        for _ in range(3)
    }
    assert len(ids) == 3
    assert len(document_binding.list_bindings_for_job(79)) == 3


def test_doctor_document_binding_checks_run_under_postgres(pg_db, tmp_path):
    """The new checks use a JOIN and a partial scan -- both must execute
    against the Postgres backend, not just SQLite."""
    from app.applications import document_binding
    from app.applications.doctor import run_doctor

    good = tmp_path / "81" / "resume.pdf"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"%PDF-1.4 good")
    document_binding.record_binding(
        job_id=81, document_kind=document_binding.DocumentKind.RESUME, artifact_path=str(good))
    clean = [i for i in run_doctor().issues if i.check.startswith("document_binding")]
    assert clean == []

    # A binding pointing at another job's artifact must be flagged.
    document_binding.record_binding(
        job_id=82, document_kind=document_binding.DocumentKind.RESUME, artifact_path=str(good))
    flagged = [i for i in run_doctor().issues if i.check == "document_binding_wrong_job"]
    assert len(flagged) == 1
    assert flagged[0].severity == "serious"


def test_execution_contract_checks_run_under_postgres(pg_db):
    from app.applications.doctor import run_doctor

    issues = [i for i in run_doctor().issues
               if i.check.startswith("execution_contract") or i.check.startswith("confirmation_phrase")]
    assert issues == []


def test_capability_audit_is_backend_independent(pg_db):
    """The contract is derived purely from in-process registries -- it must
    read identically regardless of database backend."""
    from app.applications.execution_contract import build_contract

    assert build_contract("greenhouse").submission_supported is False
    assert build_contract("lever").submission_supported is False
    assert build_contract("mock_ats").submission_supported is True


def test_every_application_doctor_check_executes_under_postgres(pg_db):
    """Real Provider Execution V1 found that `run_doctor()` had NEVER
    actually worked against PostgreSQL: `_check_duplicate_active_execution`
    used `HAVING n > 1` (a SELECT alias, which Postgres rejects) and
    `_check_missing_answer_snapshot` selected an ungrouped, unaggregated
    column -- both of which SQLite silently tolerates. The whole doctor
    aborted on its first grouped check.

    This test runs EVERY check individually, each in its own session (a
    failed statement aborts a Postgres transaction, which would otherwise
    cascade and hide the real culprit), so any future SQLite-only SQL is
    caught immediately rather than at the next backend switch."""
    import inspect

    from app.applications import doctor

    checks = [(name, fn) for name, fn in vars(doctor).items()
               if name.startswith("_check_") and callable(fn)]
    assert len(checks) > 50, "expected the full doctor check set"

    failures = []
    for name, fn in checks:
        parameters = inspect.signature(fn).parameters
        try:
            if "conn" in parameters:
                with pg_db.db_session() as conn:
                    fn(conn, doctor.DoctorReport())
            else:
                fn(doctor.DoctorReport())
        except Exception as exc:  # noqa: BLE001 -- the whole point is to collect them
            failures.append(f"{name}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
    assert failures == [], failures


def test_run_doctor_completes_end_to_end_under_postgres(pg_db):
    from app.applications.doctor import run_doctor

    report = run_doctor()
    assert report.serious_count == 0
    assert report.warning_count == 0
