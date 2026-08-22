"""CLAUDE.md Phase 12 section 70: collect_phase12() metrics. Every value is
a live DB query -- no PII, no in-memory counters."""

import pytest

from app.applications import browser_session, metrics, repo as executions_repo, spa_events
from app.applications.capability_evidence import EvidenceVerificationType, record_evidence
from app.applications.workday_tenant import record_attempt
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


def test_empty_state_all_zero():
    m = metrics.collect_phase12()
    assert m["spa_apply_controls_detected"] == 0
    assert m["trusted_ats_redirects"] == 0
    assert m["blocked_redirects"] == 0
    assert m["workday_observations"] == 0
    assert m["workday_variable_observations"] == 0


def test_spa_event_counts():
    spa_events.record(spa_events.EVENT_APPLY_CONTROL_DETECTED, provider="greenhouse")
    spa_events.record(spa_events.EVENT_TRUSTED_REDIRECT, provider="lever")
    spa_events.record(spa_events.EVENT_BLOCKED_REDIRECT, provider="smartrecruiters")
    spa_events.record(spa_events.EVENT_SPA_ROUTE_DETECTED, provider="smartrecruiters")
    spa_events.record(spa_events.EVENT_DYNAMIC_FORM_TIMEOUT, provider="workday")
    spa_events.record(spa_events.EVENT_APPLY_CONTROL_UNKNOWN, provider="ashby")
    m = metrics.collect_phase12()
    assert m["spa_apply_controls_detected"] == 1
    assert m["trusted_ats_redirects"] == 1
    assert m["blocked_redirects"] == 1
    assert m["spa_routes_detected"] == 1
    assert m["dynamic_form_timeouts"] == 1
    assert m["spa_apply_controls_unknown"] == 1


def test_iframe_and_shadow_dom_session_counts():
    job_id = insert_job(_job())
    execution_id = executions_repo.create_execution(job_id, provider="smartrecruiters", mode="ASSIST")
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="smartrecruiters",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], iframe_used=1, shadow_dom_used=1,
                                    stage="APPLICATION_FORM")
    m = metrics.collect_phase12()
    assert m["iframe_forms_detected"] == 1
    assert m["shadow_forms_detected"] == 1
    assert m["dynamic_forms_detected"] == 1


def test_workday_observation_and_variable_counts():
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="LOGIN_TRIGGER")
    m = metrics.collect_phase12()
    assert m["workday_observations"] == 2
    assert m["workday_variable_observations"] == 1


def test_capability_live_revalidations_counts_repeated_evidence():
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    m = metrics.collect_phase12()
    assert m["capability_live_revalidations"] == 1


def test_smartrecruiters_form_verified_counts_real_browser_evidence():
    record_evidence("smartrecruiters", "field_discovery", EvidenceVerificationType.REAL_BROWSER)
    m = metrics.collect_phase12()
    assert m["smartrecruiters_form_verified"] == 1
