"""CLAUDE.md Phase 13 section 62: application-doctor checks added this
phase. Read-only report generation -- never auto-repairs."""

import pytest

from app.applications import browser_session, checkpoints, repo as executions_repo
from app.applications.doctor import run_doctor
from app.applications.job_identity import (
    JobIdentitySignals,
    record_verification,
    verify_job_identity_full,
)
from app.applications.models import ExecutionMode, ExecutionStatus
from app.applications.provider_health import FailureKind, record_failure, record_success
from app.jobs_repo import insert_job, update_job
from app.models import ApplicationState, Job, SponsorshipStatus


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Full-time role. H-1B sponsorship is available.",
        employment_type="full_time", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        application_state=ApplicationState.READY_TO_APPLY,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _execution_for(job_id: int, mode: str = "ASSIST") -> str:
    return executions_repo.create_execution(job_id, provider="mock_ats", mode=mode)


def test_provider_healthy_from_stale_evidence_flagged(tmp_env, monkeypatch):
    from app import config

    record_success("greenhouse", live_validation=True)
    # Force staleness via the config threshold, matching capability_evidence's
    # own test style -- never mutating the row's timestamp directly. The row
    # is still form_verified=1 from the record_success() call above, so this
    # exercises "previously verified, now stale" rather than "never verified".
    monkeypatch.setattr(config, "CAPABILITY_EVIDENCE_MAX_AGE_DAYS", -1)
    report = run_doctor()
    assert any(i.check == "provider_healthy_from_stale_evidence" for i in report.issues)


def test_provider_health_not_flagged_when_fresh(tmp_env):
    record_success("greenhouse", live_validation=True)
    report = run_doctor()
    assert not any(i.check == "provider_healthy_from_stale_evidence" for i in report.issues)


def test_closed_job_queued_flagged():
    job_id = insert_job(_job())
    _execution_for(job_id)
    # The job closes AFTER the execution was already created and mirrored to
    # EXECUTION_QUEUED -- simulates a job that goes stale mid-preparation.
    update_job(job_id, application_state=ApplicationState.JOB_NO_LONGER_ACTIVE)
    report = run_doctor()
    assert any(i.check == "closed_job_queued" for i in report.issues)


def test_closed_job_not_flagged_when_no_active_execution():
    insert_job(_job(application_state=ApplicationState.JOB_NO_LONGER_ACTIVE))
    report = run_doctor()
    assert not any(i.check == "closed_job_queued" for i in report.issues)


def test_stale_resume_jd_mismatch_flagged():
    job_id = insert_job(_job())
    update_job(job_id, jd_sponsorship_fingerprint="new-fp", resume_jd_fingerprint="old-fp")
    _execution_for(job_id)
    report = run_doctor()
    assert any(i.check == "stale_resume_jd_mismatch" for i in report.issues)


def test_matching_resume_jd_fingerprint_not_flagged():
    job_id = insert_job(_job())
    update_job(job_id, jd_sponsorship_fingerprint="same-fp", resume_jd_fingerprint="same-fp")
    _execution_for(job_id)
    report = run_doctor()
    assert not any(i.check == "stale_resume_jd_mismatch" for i in report.issues)


def test_captcha_blocked_session_marked_automated_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id, mode=ExecutionMode.AUTO_PERMITTED.value)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="smartrecruiters",
                                              application_url="https://x")
    browser_session.update_session(session["session_id"], status="PAUSED_CAPTCHA", needs_user_action=1)
    report = run_doctor()
    assert any(i.check == "captcha_blocked_session_marked_automated" for i in report.issues)


def test_captcha_blocked_session_not_flagged_when_assist_mode():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id, mode=ExecutionMode.ASSIST.value)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="smartrecruiters",
                                              application_url="https://x")
    browser_session.update_session(session["session_id"], status="PAUSED_CAPTCHA", needs_user_action=1)
    report = run_doctor()
    assert not any(i.check == "captcha_blocked_session_marked_automated" for i in report.issues)


def test_checkpoint_inconsistency_flagged():
    checkpoints.record_checkpoint("sess-x", checkpoints.CheckpointStage.FIELDS_PREPARED)
    checkpoints.record_checkpoint("sess-x", checkpoints.CheckpointStage.READY_FOR_FINAL_SUBMIT)
    checkpoints.record_checkpoint("sess-x", checkpoints.CheckpointStage.ENTRY_REACHED)
    report = run_doctor()
    assert any(i.check == "checkpoint_inconsistency" for i in report.issues)


def test_checkpoint_no_inconsistency_when_forward_only():
    checkpoints.record_checkpoint("sess-y", checkpoints.CheckpointStage.ENTRY_REACHED)
    checkpoints.record_checkpoint("sess-y", checkpoints.CheckpointStage.FORM_DISCOVERED)
    report = run_doctor()
    assert not any(i.check == "checkpoint_inconsistency" for i in report.issues)


def test_unsafe_retry_state_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    executions_repo.update_execution(execution_id, job_id, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE)
    executions_repo.log_event(execution_id, job_id, "submit_attempted")
    executions_repo.log_event(execution_id, job_id, "submit_attempted")
    report = run_doctor()
    assert any(i.check == "unsafe_retry_state" for i in report.issues)


def test_single_submit_attempt_on_permanent_failure_not_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    executions_repo.update_execution(execution_id, job_id, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE)
    executions_repo.log_event(execution_id, job_id, "submit_attempted")
    report = run_doctor()
    assert not any(i.check == "unsafe_retry_state" for i in report.issues)


def test_identity_mismatch_but_session_active_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                              application_url="https://x")
    verification = verify_job_identity_full(
        JobIdentitySignals(requisition_id="R-1"), JobIdentitySignals(requisition_id="R-2"),
    )
    record_verification(job_id, stage="PRE_UPLOAD", stored=JobIdentitySignals(requisition_id="R-1"),
                         observed=JobIdentitySignals(requisition_id="R-2"), verification=verification)
    # session left ACTIVE (not paused) -- this is the anomaly.
    browser_session.update_session(session["session_id"], status="ACTIVE")
    report = run_doctor()
    assert any(i.check == "identity_mismatch_but_session_active" for i in report.issues)


def test_identity_mismatch_with_paused_session_not_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                              application_url="https://x")
    verification = verify_job_identity_full(
        JobIdentitySignals(requisition_id="R-1"), JobIdentitySignals(requisition_id="R-2"),
    )
    record_verification(job_id, stage="PRE_UPLOAD", stored=JobIdentitySignals(requisition_id="R-1"),
                         observed=JobIdentitySignals(requisition_id="R-2"), verification=verification)
    browser_session.update_session(session["session_id"], status="PAUSED_JOB_IDENTITY_MISMATCH", needs_user_action=1)
    report = run_doctor()
    assert not any(i.check == "identity_mismatch_but_session_active" for i in report.issues)


def test_applied_with_weak_confirmation_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                              application_url="https://x")
    browser_session.update_session(session["session_id"], status="CONFIRMED", confirmation_observed=1,
                                    confirmation_id="ABC-1", confirmation_evidence_strength="WEAK")
    report = run_doctor()
    assert any(i.check == "applied_with_weak_confirmation" for i in report.issues)


def test_applied_with_strong_confirmation_not_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                              application_url="https://x")
    browser_session.update_session(session["session_id"], status="CONFIRMED", confirmation_observed=1,
                                    confirmation_id="ABC-1", confirmation_evidence_strength="STRONG")
    report = run_doctor()
    assert not any(i.check == "applied_with_weak_confirmation" for i in report.issues)
