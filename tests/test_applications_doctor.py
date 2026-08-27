"""CLAUDE.md Phase 8 section 58: application doctor integrity checker."""

import json

import pytest

from app import config
from app.applications.doctor import run_doctor
from app.applications.executor import process_execution, queue_application
from app.candidate.profile import save_profile
from app.db import db_session
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)


@pytest.fixture
def profile_saved(tmp_env, sample_profile):
    save_profile(sample_profile)
    return sample_profile


def _mock_job(external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )


def test_doctor_clean_after_normal_applied_flow(profile_saved):
    job = ingest_and_process(_mock_job("doc-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "APPLIED"

    report = run_doctor()
    assert report.serious_count == 0


def test_doctor_catches_applied_without_confirmation(profile_saved):
    job = ingest_and_process(_mock_job("doc-2"))
    result = queue_application(job.id, mode="ASSIST")
    process_execution(result.execution_id)

    # Corrupt the row directly to simulate a bug that marked APPLIED without
    # confirmation evidence -- the doctor must catch this, never a normal
    # code path.
    with db_session() as conn:
        conn.execute(
            "UPDATE application_executions SET status = 'APPLIED', confirmation_id = '', "
            "user_action_reason = '' WHERE execution_id = ?",
            (result.execution_id,),
        )

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "applied_without_confirmation" in checks
    assert report.serious_count >= 1


def test_doctor_catches_execution_missing_job(profile_saved):
    job = ingest_and_process(_mock_job("doc-3"))
    queue_application(job.id, mode="ASSIST")

    with db_session() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "execution_missing_job" in checks


# --- Autonomous-ux-reliability-v1 section I: health/self-healing checks ---


def test_doctor_catches_queue_starvation(profile_saved):
    job = ingest_and_process(_mock_job("doc-starve"))
    queue_application(job.id, mode="ASSIST")  # leaves it QUEUED, never processed

    with db_session() as conn:
        conn.execute(
            "UPDATE application_executions SET started_at = ? WHERE job_id = ?",
            ("2020-01-01T00:00:00+00:00", job.id),
        )

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "application_queue_starvation" in checks


def test_doctor_does_not_flag_a_freshly_queued_execution(profile_saved):
    job = ingest_and_process(_mock_job("doc-fresh"))
    queue_application(job.id, mode="ASSIST")

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "application_queue_starvation" not in checks


def test_doctor_catches_submission_circuit_open_too_long(profile_saved):
    with db_session() as conn:
        conn.execute(
            "INSERT INTO application_provider_circuit_state "
            "(provider, state, consecutive_failures, opened_at, updated_at) "
            "VALUES ('mock_ats', 'OPEN', 5, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
        )

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "application_submission_circuit_open_too_long" in checks


def test_doctor_does_not_flag_a_recently_opened_circuit(profile_saved):
    from datetime import datetime, timezone

    with db_session() as conn:
        conn.execute(
            "INSERT INTO application_provider_circuit_state "
            "(provider, state, consecutive_failures, opened_at, updated_at) "
            "VALUES ('mock_ats', 'OPEN', 5, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "application_submission_circuit_open_too_long" not in checks
