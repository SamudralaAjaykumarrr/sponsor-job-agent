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


def test_doctor_does_not_flag_applied_execution_with_a_stale_and_a_confirmed_session(profile_saved):
    """Real bug caught live (2026-08-31, job 200/Robinhood): an execution can
    legitimately accumulate MULTIPLE browser_assist_sessions rows over its
    lifetime (an EXPIRED one from an earlier reconstruction, then the actual
    CONFIRMED one) -- _check_browser_applied_without_confirmation's old
    session-row-JOIN query flagged the stale EXPIRED row even though the
    newer CONFIRMED row for the SAME execution genuinely proved confirmation.
    Also verifies a non-empty confirmation_url (with no confirmation_id --
    many employers' confirmation pages have no extractable reference number)
    on the execution row alone is honored too."""
    from app.applications import browser_session

    job = ingest_and_process(_mock_job("doc-multisession"))
    result = queue_application(job.id, mode="ASSIST")
    execution_id = result.execution_id

    stale = browser_session.create_session(
        execution_id=execution_id, job_id=job.id, provider="mock_ats", application_url="https://x",
    )
    browser_session.update_session(stale["session_id"], status="EXPIRED", active=0)
    confirmed = browser_session.create_session(
        execution_id=execution_id, job_id=job.id, provider="mock_ats", application_url="https://x",
    )
    browser_session.update_session(
        confirmed["session_id"], status="CONFIRMED", confirmation_observed=1, active=0,
    )

    with db_session() as conn:
        conn.execute(
            "UPDATE application_executions SET status = 'APPLIED', confirmation_id = '', "
            "confirmation_url = 'https://boards.greenhouse.io/acme/jobs/1/confirmation' WHERE execution_id = ?",
            (execution_id,),
        )

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "browser_applied_without_confirmation" not in checks


def test_doctor_still_catches_applied_with_zero_confirming_sessions(profile_saved):
    """Preserves the original intent: an APPLIED execution linked to
    browser-assist sessions where NONE of them show confirmation, and the
    execution itself has no confirmation_id/url either, is still a genuine
    finding."""
    from app.applications import browser_session

    job = ingest_and_process(_mock_job("doc-unconfirmed-session"))
    result = queue_application(job.id, mode="ASSIST")
    execution_id = result.execution_id

    session = browser_session.create_session(
        execution_id=execution_id, job_id=job.id, provider="mock_ats", application_url="https://x",
    )
    browser_session.update_session(session["session_id"], status="EXPIRED", active=0)

    with db_session() as conn:
        conn.execute(
            "UPDATE application_executions SET status = 'APPLIED', confirmation_id = '', confirmation_url = '' "
            "WHERE execution_id = ?",
            (execution_id,),
        )

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "browser_applied_without_confirmation" in checks


def test_doctor_does_not_flag_legitimate_manual_confirmation_for_unsupported_provider(profile_saved):
    """Real bug caught live (2026-08-31, job 200/Robinhood): Greenhouse is
    ASSIST_ONLY (submission_capability='UNSUPPORTED' at approval time) by
    design -- a human completes it manually, and
    app.applications.browser_assist.attempt_user_submit_reconciliation_from_evidence()
    (or the live-DOM version) is the sanctioned way that reaches APPLIED,
    never calling provider.submit() at all.
    _check_approval_submitted_for_unsupported_provider used to flag this
    exact legitimate case as if the automated executor pipeline had bypassed
    its own capability gate."""
    from app.applications import browser_assist, browser_session
    from app.applications import repo as executions_repo
    from app.db import db_session as _db_session

    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
        external_job_id="doc-unsupported-manual", company_identifier="acme", mode=ApplicationMode.ASSIST,
    ))
    execution_id = executions_repo.create_execution(job.id, provider="greenhouse", mode="ASSIST")
    with _db_session() as conn:
        conn.execute(
            "INSERT INTO application_approvals (approval_id, execution_id, job_id, provider, approved_at, "
            "approved_by, job_identity_fingerprint, jd_fingerprint, resume_variant_id, resume_fingerprint, "
            "answers_version, profile_fingerprint, form_fingerprint, sponsorship_status_at_approval, "
            "employment_type_at_approval, submission_capability, status, created_at) "
            "VALUES ('appr_test_unsupported', ?, ?, 'greenhouse', '2026-08-31T00:00:00+00:00', 'user', "
            "'fp', 'jdfp', '', '', 1, 'pfp', 'ffp', 'LIKELY_SPONSOR', 'FULL_TIME', 'UNSUPPORTED', 'ACTIVE', "
            "'2026-08-31T00:00:00+00:00')",
            (execution_id, job.id),
        )

    session = browser_session.create_session(
        execution_id=execution_id, job_id=job.id, provider="greenhouse",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    result = browser_assist.attempt_user_submit_reconciliation_from_evidence(
        session["session_id"],
        current_url="https://job-boards.greenhouse.io/acme/jobs/1/confirmation",
        body_text="Thank you for your interest in joining our world-class team at Acme Corp!",
    )
    assert result["ok"] is True

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "approval_submitted_for_unsupported_provider" not in checks


def test_doctor_still_catches_genuine_unsupported_provider_submission_bypass(profile_saved):
    """Preserves the original intent: SUBMITTING/SUBMITTED/SUBMISSION_CONFIRMED
    for an UNSUPPORTED-capability approval is always a genuine automated-
    pipeline-bypassed-its-own-gate bug, regardless of the APPLIED-specific
    carve-out above."""
    from app.applications import repo as executions_repo
    from app.db import db_session as _db_session

    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
        external_job_id="doc-unsupported-bypass", company_identifier="acme", mode=ApplicationMode.ASSIST,
    ))
    execution_id = executions_repo.create_execution(job.id, provider="greenhouse", mode="ASSIST")
    with _db_session() as conn:
        conn.execute(
            "INSERT INTO application_approvals (approval_id, execution_id, job_id, provider, approved_at, "
            "approved_by, job_identity_fingerprint, jd_fingerprint, resume_variant_id, resume_fingerprint, "
            "answers_version, profile_fingerprint, form_fingerprint, sponsorship_status_at_approval, "
            "employment_type_at_approval, submission_capability, status, created_at) "
            "VALUES ('appr_test_bypass', ?, ?, 'greenhouse', '2026-08-31T00:00:00+00:00', 'user', "
            "'fp', 'jdfp', '', '', 1, 'pfp', 'ffp', 'LIKELY_SPONSOR', 'FULL_TIME', 'UNSUPPORTED', 'ACTIVE', "
            "'2026-08-31T00:00:00+00:00')",
            (execution_id, job.id),
        )
        conn.execute(
            "UPDATE application_executions SET status = 'SUBMITTED' WHERE execution_id = ?", (execution_id,),
        )

    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "approval_submitted_for_unsupported_provider" in checks


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
