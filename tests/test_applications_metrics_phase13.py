"""CLAUDE.md Phase 13 section 63: collect_phase13() metrics. Every value is
a live DB query -- no PII, no in-memory counters."""

import pytest

from app.applications import browser_session, canary, metrics, repo as executions_repo
from app.applications.job_identity import JobIdentitySignals, record_verification, verify_job_identity_full
from app.applications.provider_health import record_success
from app.jobs_repo import insert_job
from app.models import ApplicationState, Job, SponsorshipStatus


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _job() -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Full-time role.", employment_type="full_time",
        sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, application_state=ApplicationState.READY_TO_APPLY,
    )


def test_collect_phase13_returns_zero_counts_on_empty_db():
    result = metrics.collect_phase13()
    assert result["job_identity_verified_total"] == 0
    assert result["job_identity_mismatch_total"] == 0
    assert result["provider_canary_runs_total"] == 0
    assert result["captcha_handoffs_total"] == 0
    assert result["provider_assist_health"] == {}


def test_job_identity_counters():
    verified = verify_job_identity_full(JobIdentitySignals(requisition_id="R-1"), JobIdentitySignals(requisition_id="R-1"))
    record_verification(1, stage="PRE_UPLOAD", stored=JobIdentitySignals(requisition_id="R-1"),
                         observed=JobIdentitySignals(requisition_id="R-1"), verification=verified)
    mismatch = verify_job_identity_full(JobIdentitySignals(requisition_id="R-1"), JobIdentitySignals(requisition_id="R-2"))
    record_verification(2, stage="PRE_UPLOAD", stored=JobIdentitySignals(requisition_id="R-1"),
                         observed=JobIdentitySignals(requisition_id="R-2"), verification=mismatch)
    result = metrics.collect_phase13()
    assert result["job_identity_verified_total"] == 1
    assert result["job_identity_mismatch_total"] == 1


def test_provider_canary_counters():
    canary.record_canary_run(canary.CanaryResult(provider="greenhouse", url="https://x/1", ok=True))
    canary.record_canary_run(canary.CanaryResult(provider="lever", url="https://y/2", ok=False, error="timeout"))
    result = metrics.collect_phase13()
    assert result["provider_canary_runs_total"] == 2
    assert result["provider_canary_failures_total"] == 1


def test_captcha_and_login_handoff_counters():
    job_id = insert_job(_job())
    execution_id = executions_repo.create_execution(job_id, provider="mock_ats", mode="ASSIST")
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="smartrecruiters",
                                              application_url="https://x")
    browser_session.update_session(session["session_id"], status="PAUSED_CAPTCHA", needs_user_action=1)

    job_id2 = insert_job(_job())
    execution_id2 = executions_repo.create_execution(job_id2, provider="mock_ats", mode="ASSIST")
    session2 = browser_session.create_session(execution_id=execution_id2, job_id=job_id2, provider="workday",
                                               application_url="https://y")
    browser_session.update_session(session2["session_id"], status="PAUSED_LOGIN_REQUIRED", needs_user_action=1)

    result = metrics.collect_phase13()
    assert result["captcha_handoffs_total"] == 1
    assert result["login_handoffs_total"] == 1


def test_confirmation_strength_counter():
    job_id = insert_job(_job())
    execution_id = executions_repo.create_execution(job_id, provider="mock_ats", mode="ASSIST")
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                              application_url="https://x")
    browser_session.update_session(session["session_id"], status="CONFIRMED", confirmation_observed=1,
                                    confirmation_evidence_strength="STRONG")
    result = metrics.collect_phase13()
    assert result["confirmation_strong_total"] == 1


def test_provider_assist_health_never_collapses_tenants():
    record_success("workday", tenant="acme", site="External", live_validation=True)
    record_success("workday", tenant="globex", site="External", live_validation=True)
    result = metrics.collect_phase13()
    assert "workday/acme/External" in result["provider_assist_health"]
    assert "workday/globex/External" in result["provider_assist_health"]
