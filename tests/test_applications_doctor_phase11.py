"""CLAUDE.md Phase 11 section 52: application-doctor checks added this
phase. Read-only report generation -- never auto-repairs."""

from datetime import datetime, timedelta, timezone

import pytest

from app.applications import browser_session, repo as executions_repo
from app.applications.capability_evidence import EvidenceVerificationType, record_evidence
from app.applications.doctor import run_doctor
from app.applications.models import ExecutionStatus
from app.jobs_repo import insert_job
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


def _execution_for(job_id: int) -> str:
    return executions_repo.create_execution(job_id, provider="mock_ats", mode="ASSIST")


def test_clean_state_has_no_phase11_issues():
    report = run_doctor()
    phase11_checks = {
        i.check for i in report.issues
        if i.check in {
            "paused_session_holding_lease", "browser_session_owner_conflict", "stale_capability_evidence",
            "invalid_step_progress", "invented_total_steps", "real_provider_auto_submit_without_authorization",
            "false_confirmation_evidence", "duplicate_detected_execution_marked_applied",
        }
    }
    assert phase11_checks == set()


def test_paused_session_holding_lease_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], status="PAUSED_LOGIN_REQUIRED")
    browser_session.claim_session(session["session_id"], worker_id="proc-1", lease_seconds=600)
    report = run_doctor()
    assert any(i.check == "paused_session_holding_lease" for i in report.issues)


def test_paused_session_without_lease_not_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], status="PAUSED_LOGIN_REQUIRED")
    report = run_doctor()
    assert not any(i.check == "paused_session_holding_lease" for i in report.issues)


def test_owner_conflict_flagged_when_columns_disagree():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.claim_session(session["session_id"], worker_id="proc-1", lease_seconds=600)
    from app.db import db_session

    with db_session() as conn:
        conn.execute("UPDATE browser_assist_sessions SET worker_id = 'proc-2' WHERE session_id = ?",
                     (session["session_id"],))
    report = run_doctor()
    assert any(i.check == "browser_session_owner_conflict" for i in report.issues)


def test_stale_capability_evidence_flagged():
    old_observed = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    record_evidence("greenhouse", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC, observed_at=old_observed)
    report = run_doctor()
    assert any(i.check == "stale_capability_evidence" for i in report.issues)


def test_fresh_capability_evidence_not_flagged():
    record_evidence("greenhouse", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC)
    report = run_doctor()
    assert not any(i.check == "stale_capability_evidence" for i in report.issues)


def test_current_step_exceeding_total_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], current_step=5, total_steps_if_known=3,
                                    step_confidence="EXACT")
    report = run_doctor()
    assert any(i.check == "invalid_step_progress" for i in report.issues)


def test_total_steps_without_exact_confidence_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], total_steps_if_known=4, step_confidence="UNKNOWN")
    report = run_doctor()
    assert any(i.check == "invented_total_steps" for i in report.issues)


def test_exact_step_progress_not_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], current_step=2, total_steps_if_known=4,
                                    step_confidence="EXACT")
    report = run_doctor()
    assert not any(i.check in ("invalid_step_progress", "invented_total_steps") for i in report.issues)


def test_real_provider_auto_submit_without_authorization_not_flagged_on_current_code():
    """Static assertion mirroring _check_no_browser_auto_submit_capability
    -- must always pass for this project's real code today."""
    report = run_doctor()
    assert not any(i.check == "real_provider_auto_submit_without_authorization" for i in report.issues)


def test_confirmed_session_without_any_evidence_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], status="CONFIRMED", confirmation_observed=1)
    report = run_doctor()
    assert any(i.check == "false_confirmation_evidence" for i in report.issues)


def test_confirmed_session_with_evidence_not_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], status="CONFIRMED", confirmation_observed=1,
                                    confirmation_id="ABC-123")
    report = run_doctor()
    assert not any(i.check == "false_confirmation_evidence" for i in report.issues)


def test_duplicate_detected_with_applied_execution_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], status="DUPLICATE_APPLICATION_DETECTED",
                                    needs_user_action=1)
    executions_repo.update_execution(execution_id, job_id, ExecutionStatus.APPLIED, confirmation_id="X-1")
    report = run_doctor()
    assert any(i.check == "duplicate_detected_execution_marked_applied" for i in report.issues)
