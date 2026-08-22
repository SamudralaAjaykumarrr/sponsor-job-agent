"""CLAUDE.md Phase 10 section 64: application-doctor checks for the
browser-assist layer. Read-only report generation -- never auto-repairs."""

from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.applications import browser_session, repo as executions_repo
from app.applications.doctor import run_doctor
from app.applications.models import ExecutionStatus
from app.jobs_repo import get_job, insert_job
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


def test_clean_state_has_no_phase10_issues():
    report = run_doctor()
    phase10_checks = {i.check for i in report.issues if "browser" in i.check}
    assert phase10_checks == set()


def test_session_without_execution_detected():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    executions_repo.update_execution(execution_id, job_id, ExecutionStatus.WITHDRAWN)
    from app.db import db_session

    with db_session() as conn:
        conn.execute("DELETE FROM application_executions WHERE execution_id = ?", (execution_id,))
    report = run_doctor()
    assert any(i.check == "browser_session_without_execution" for i in report.issues)


def test_session_for_contract_job_detected():
    job_id = insert_job(_job(employment_type="contract"))
    execution_id = _execution_for(job_id)
    browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                     application_url="https://x")
    report = run_doctor()
    assert any(i.check == "browser_session_non_full_time" for i in report.issues)


def test_session_for_no_sponsorship_job_detected():
    job_id = insert_job(_job(sponsorship_status=SponsorshipStatus.NO_SPONSORSHIP))
    execution_id = _execution_for(job_id)
    browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                     application_url="https://x")
    report = run_doctor()
    assert any(i.check == "browser_session_non_eligible_sponsorship" for i in report.issues)


def test_session_for_likely_sponsor_job_is_not_flagged():
    """LIKELY_SPONSOR is legitimately allowed to prepare/review -- never
    flagged by this check."""
    job_id = insert_job(_job(sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR))
    execution_id = _execution_for(job_id)
    browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                    application_url="https://x")
    report = run_doctor()
    assert not any(i.check == "browser_session_non_eligible_sponsorship" for i in report.issues)


def test_stale_active_session_flagged_as_warning(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 5)
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    old = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    browser_session.update_session(session["session_id"], last_activity_at=old)

    report = run_doctor()
    issue = next((i for i in report.issues if i.check == "stale_browser_session_still_active"), None)
    assert issue is not None
    assert issue.severity == "warning"


def test_confirmation_without_applied_execution_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], status="CONFIRMED", confirmation_observed=1)
    # Execution deliberately left at its default (non-APPLIED) status.
    report = run_doctor()
    assert any(i.check == "browser_confirmation_without_applied_execution" for i in report.issues)


def test_no_browser_auto_submit_capability_check_passes_on_current_code():
    """Static assertion: app.applications.browser_runtime must never expose
    a click-the-submit-button capability. This should always pass for this
    project's real code -- a regression here means someone added exactly
    the capability CLAUDE.md Phase 10 section 29 forbids."""
    report = run_doctor()
    assert not any(i.check == "unexpected_browser_auto_submit_capability" for i in report.issues)


def test_forbidden_field_scan_flags_suspicious_text():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], user_action_reason="leaked password=hunter2 somehow")
    report = run_doctor()
    assert any(i.check == "browser_session_forbidden_field" for i in report.issues)
