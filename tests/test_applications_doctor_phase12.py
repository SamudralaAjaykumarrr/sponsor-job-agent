"""CLAUDE.md Phase 12 section 69: application-doctor checks added this
phase. Read-only report generation -- never auto-repairs."""

import pytest

from app.applications import browser_session, repo as executions_repo, spa_events
from app.applications.doctor import run_doctor
from app.applications.workday_tenant import record_attempt
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


def test_clean_state_has_no_phase12_issues():
    report = run_doctor()
    phase12_checks = {
        i.check for i in report.issues
        if i.check in {
            "unsafe_redirect_allowlist", "stage_transition_invalid", "job_identity_mismatch_not_surfaced",
            "workday_universal_claim_from_one_tenant",
        }
    }
    assert phase12_checks == set()


def test_trusted_redirect_allowlist_is_never_flagged_on_real_table():
    """Static assertion mirroring the doctor's own real-provider-domain
    table -- must always pass for this project's real code today."""
    report = run_doctor()
    assert not any(i.check == "unsafe_redirect_allowlist" for i in report.issues)


def test_stage_transition_invalid_event_flagged():
    spa_events.record(spa_events.EVENT_STAGE_TRANSITION_INVALID, session_id="bsess_x",
                       detail="CONFIRMATION -> LANDING_PAGE")
    report = run_doctor()
    assert any(i.check == "stage_transition_invalid" for i in report.issues)


def test_no_stage_transition_events_means_no_issue():
    report = run_doctor()
    assert not any(i.check == "stage_transition_invalid" for i in report.issues)


def test_job_identity_mismatch_not_surfaced_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="workday",
                                               application_url="https://acme.wd5.myworkdayjobs.com/x")
    browser_session.update_session(session["session_id"], status="PAUSED_JOB_IDENTITY_MISMATCH",
                                    needs_user_action=0)
    report = run_doctor()
    assert any(i.check == "job_identity_mismatch_not_surfaced" for i in report.issues)


def test_job_identity_mismatch_correctly_surfaced_not_flagged():
    job_id = insert_job(_job())
    execution_id = _execution_for(job_id)
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="workday",
                                               application_url="https://acme.wd5.myworkdayjobs.com/x")
    browser_session.update_session(session["session_id"], status="PAUSED_JOB_IDENTITY_MISMATCH",
                                    needs_user_action=1)
    report = run_doctor()
    assert not any(i.check == "job_identity_mismatch_not_surfaced" for i in report.issues)


def test_workday_universal_claim_flagged_without_stable_evidence(monkeypatch):
    """CLAUDE.md Phase 12 sections 20-21, 68: if the hand-curated matrix ever
    claimed workday=LIVE_FORM_VERIFIED without a genuinely STABLE tenant
    behind it, the doctor must catch it -- simulated here via a monkeypatched
    matrix row rather than editing the real (honest, NOT_TESTED) one."""
    from app.applications import browser_capability_matrix as matrix

    fake_row = dict(matrix.all_rows()[0])
    fake_row.update(provider="workday", verification="LIVE_FORM_VERIFIED")
    monkeypatch.setattr(matrix, "all_rows", lambda: [fake_row])
    report = run_doctor()
    assert any(i.check == "workday_universal_claim_from_one_tenant" for i in report.issues)


def test_workday_claim_not_flagged_with_genuine_stable_evidence(monkeypatch):
    from app.applications import browser_capability_matrix as matrix

    fake_row = dict(matrix.all_rows()[0])
    fake_row.update(provider="workday", verification="LIVE_FORM_VERIFIED")
    monkeypatch.setattr(matrix, "all_rows", lambda: [fake_row])
    for _ in range(3):
        record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    report = run_doctor()
    assert not any(i.check == "workday_universal_claim_from_one_tenant" for i in report.issues)


def test_workday_not_tested_never_flagged():
    """The real, current (honest) matrix has workday=NOT_TESTED (or
    honestly-earned LIVE_FORM_VERIFIED with real evidence) -- never flagged
    either way without simulation."""
    report = run_doctor()
    assert not any(i.check == "workday_universal_claim_from_one_tenant" for i in report.issues)
