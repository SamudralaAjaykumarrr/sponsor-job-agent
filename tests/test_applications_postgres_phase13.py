"""CLAUDE.md Phase 13 section 65: new Phase 13 persistence (job identity
evidence, provider health, checkpoints, canary runs) against REAL
PostgreSQL. Marked `postgres` -- skipped automatically if `pgserver` isn't
installed (see tests/conftest.py::postgres_url)."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_job_identity_verification_round_trip(pg_db):
    from app.applications.job_identity import JobIdentitySignals, record_verification, verify_job_identity_full

    verification = verify_job_identity_full(
        JobIdentitySignals(requisition_id="R-1"), JobIdentitySignals(requisition_id="R-2"),
    )
    row = record_verification(1, stage="PRE_UPLOAD", stored=JobIdentitySignals(requisition_id="R-1"),
                               observed=JobIdentitySignals(requisition_id="R-2"), verification=verification)
    assert row["result"] == "MISMATCH"

    from app.applications.job_identity import list_verifications
    rows = list_verifications(job_id=1)
    assert len(rows) == 1


def test_provider_health_round_trip_and_upsert(pg_db):
    from app.applications.provider_health import FailureKind, ProviderAssistHealth, get_health, record_failure, record_success

    record_success("greenhouse", live_validation=True)
    result = get_health("greenhouse")
    assert result["health"] == ProviderAssistHealth.HEALTHY.value

    record_failure("greenhouse", FailureKind.CAPTCHA)
    result = get_health("greenhouse")
    assert result["health"] == ProviderAssistHealth.CAPTCHA_BLOCKED.value
    # Upsert, not a second row.
    from app.applications.provider_health import list_health
    assert len(list_health()) == 1


def test_checkpoints_round_trip(pg_db):
    from app.applications.checkpoints import CheckpointStage, list_checkpoints, record_checkpoint

    record_checkpoint("sess-pg-1", CheckpointStage.ENTRY_REACHED, job_id=1)
    record_checkpoint("sess-pg-1", CheckpointStage.FORM_DISCOVERED, job_id=1)
    rows = list_checkpoints("sess-pg-1")
    assert [r["checkpoint"] for r in rows] == ["ENTRY_REACHED", "FORM_DISCOVERED"]


def test_canary_run_round_trip(pg_db):
    from app.applications.canary import CanaryResult, list_canary_runs, record_canary_run

    record_canary_run(CanaryResult(provider="greenhouse", url="https://x/1", ok=True, form_found=True))
    rows = list_canary_runs(provider="greenhouse")
    assert len(rows) == 1
    assert rows[0]["form_found"] == 1


def test_resume_jd_fingerprint_column_and_confirmation_strength_column(pg_db, tmp_env):
    from app.jobs_repo import insert_job, get_job, update_job
    from app.models import Job

    job_id = insert_job(Job(title="Engineer", company="Acme", description="JD"))
    update_job(job_id, resume_jd_fingerprint="abc123")
    job = get_job(job_id)
    assert job.resume_jd_fingerprint == "abc123"

    from app.applications import browser_session, repo as executions_repo

    execution_id = executions_repo.create_execution(job_id, provider="mock_ats", mode="ASSIST")
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                              application_url="https://x")
    updated = browser_session.update_session(session["session_id"], confirmation_evidence_strength="STRONG")
    assert updated["confirmation_evidence_strength"] == "STRONG"
