"""CLAUDE.md Phase 10 section 61: browser-assist metrics."""

import pytest

from app.applications import browser_session, metrics, repo as executions_repo
from app.jobs_repo import insert_job
from app.models import ApplicationState, Job, SponsorshipStatus


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _session(status: str) -> dict:
    job_id = insert_job(Job(
        title="Backend Software Engineer", company="Acme Corp", description="Full-time role.",
        employment_type="full_time", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        application_state=ApplicationState.READY_TO_APPLY,
    ))
    execution_id = executions_repo.create_execution(job_id, provider="greenhouse", mode="ASSIST")
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    return browser_session.update_session(session["session_id"], status=status)


def test_metrics_all_zero_on_empty_db():
    m = metrics.collect_browser_assist()
    assert m["browser_assist_sessions_active"] == 0
    assert m["browser_assist_confirmed"] == 0
    assert m["browser_assist_live_in_process"] == 0


def test_metrics_reflect_session_statuses():
    _session("PAUSED_LOGIN_REQUIRED")
    _session("PAUSED_CAPTCHA")
    _session("READY_FOR_FINAL_SUBMIT")
    _session("SUBMISSION_STATUS_UNKNOWN")
    _session("CONFIRMED")
    _session("PAUSED_FORM_CHANGED")

    m = metrics.collect_browser_assist()
    assert m["browser_assist_login_required"] == 1
    assert m["browser_assist_captcha_required"] == 1
    assert m["browser_assist_ready_for_submit"] == 1
    assert m["browser_assist_confirmation_unknown"] == 1
    assert m["browser_assist_confirmed"] == 1
    assert m["browser_assist_form_drift"] == 1
    # CONFIRMED is terminal (active=0) so it must not count toward "active".
    assert m["browser_assist_sessions_active"] == 5
    assert m["browser_assist_sessions_paused"] == 3


def test_live_in_process_reflects_runtime_registry():
    from app.applications import browser_runtime

    class _Fake:
        pass

    browser_runtime._REGISTRY["fake-session"] = _Fake()
    try:
        m = metrics.collect_browser_assist()
        assert m["browser_assist_live_in_process"] == 1
    finally:
        browser_runtime._REGISTRY.pop("fake-session", None)
