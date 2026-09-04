"""application-action-experience-v1: TestClient coverage that every surface
(Jobs page, Dashboard, Job detail, Applications page) exposes the SAME
CTA computed by app.applications.cta.compute_apply_cta for a given job's
state (checklist items H, I, J, K, L, O -- the real-browser journey items
A-G, M, N live in tests/test_application_action_experience_playwright.py,
which needs a real Chromium binary)."""

import json

import httpx
import pytest

from app import config
from app.agent import state as agent_state
from app.applications import provider_registry
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process
from fastapi.testclient import TestClient
from app.main import app

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


def _mock_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id="cta-e2e-1", provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    agent_state.set_enabled(False)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)


def _prepare_ready_for_approval(job: Job) -> dict:
    result = queue_application(job.id, mode="ASSIST")
    assert result.queued
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value
    return execution


# --- I/J/K/L: every surface shows APPROVE & APPLY for a READY_FOR_APPROVAL job

def test_jobs_page_shows_approve_and_apply_cta(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job())
    _prepare_ready_for_approval(job)
    client = TestClient(app)

    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "APPROVE &amp; APPLY" in resp.text
    assert f'data-job-id="{job.id}"' in resp.text


def test_dashboard_shows_approve_and_apply_cta(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job())
    _prepare_ready_for_approval(job)
    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "APPROVE &amp; APPLY" in resp.text
    assert f'data-job-id="{job.id}"' in resp.text


def test_job_detail_hero_shows_approve_and_apply_cta(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job())
    _prepare_ready_for_approval(job)
    client = TestClient(app)

    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    # The hero CTA (data-job-hero) must contain the primary action, not just
    # the deep "Application execution" section far down the page.
    hero_start = resp.text.index("data-job-hero")
    exec_section_start = resp.text.index('id="application-execution"')
    hero_html = resp.text[hero_start:exec_section_start]
    assert "APPROVE &amp; APPLY" in hero_html


def test_applications_page_shows_approve_and_apply_cta(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job())
    _prepare_ready_for_approval(job)
    client = TestClient(app)

    resp = client.get("/applications", params={"bucket": "ready"})
    assert resp.status_code == 200
    assert "APPROVE &amp; APPLY" in resp.text
    assert "Review" in resp.text


# --- H: a real provider with genuine form-fill support but no verified
#        final-submission capability shows READY FOR FINAL REVIEW, never a
#        fake APPLIED, everywhere the CTA is rendered.

def test_unsupported_final_submission_provider_shows_ready_for_final_review_everywhere(tmp_env, sample_profile):
    save_profile(sample_profile)

    fixture_payload = {
        "questions": [
            {"label": "First Name", "required": True,
             "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
            {"label": "Last Name", "required": True,
             "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
            {"label": "Email", "required": True,
             "fields": [{"name": "email", "type": "input_text", "values": []}]},
            {"label": "Resume/CV", "required": True,
             "fields": [{"name": "resume", "type": "input_file", "values": []}]},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture_payload)

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_provider = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id="gh-cta-1", company_identifier="acme", mode=ApplicationMode.ASSIST,
        ))
        _prepare_ready_for_approval(job)

        client = TestClient(app)
        approve_resp = client.post(f"/jobs/{job.id}/applications/approve", follow_redirects=False)
        assert approve_resp.status_code == 303

        job_detail = client.get(f"/jobs/{job.id}")
        assert "READY FOR FINAL REVIEW" in job_detail.text
        assert "APPLIED ✓" not in job_detail.text
        assert "not verified for this provider" in job_detail.text

        jobs_page = client.get("/jobs")
        assert "READY FOR FINAL REVIEW" in jobs_page.text

        applications_page = client.get("/applications", params={"bucket": "approved"})
        assert "READY FOR FINAL REVIEW" in applications_page.text

        api_status = client.get(f"/api/jobs/{job.id}/apply-status").json()
        assert api_status["cta"]["label"] == "READY FOR FINAL REVIEW"
        assert api_status["cta"]["style"] == "secondary"
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original_provider


# --- C: APPROVE & APPLY invokes the real, durable approval endpoint (never
#        a fake link) -- the JSON variant used by the JS-enhanced button.

def test_approve_endpoint_json_variant_actually_approves_and_returns_cta(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job())
    _prepare_ready_for_approval(job)
    client = TestClient(app)

    resp = client.post(
        f"/jobs/{job.id}/applications/approve", headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # mock_ats genuinely supports submission -- a real state transition, not
    # a UI-only claim.
    assert body["execution"]["status"] == ExecutionStatus.APPLIED.value
    assert body["cta"]["style"] == "success"
    assert body["cta"]["label"] == "APPLIED ✓"

    # A second click must never re-submit -- api/apply-status agrees with
    # what the approve call already reported.
    status_resp = client.get(f"/api/jobs/{job.id}/apply-status")
    assert status_resp.json()["application_state"] == "APPLIED"


def test_approve_endpoint_json_variant_reports_failure_without_raising_html_error(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job())  # never queued -- no active execution yet
    client = TestClient(app)

    resp = client.post(
        f"/jobs/{job.id}/applications/approve", headers={"Accept": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


# --- O: TEST MODE fixture jobs never leak their CTA into the real-mode
#        default view of any of the four surfaces.

def test_test_fixture_job_cta_never_leaks_into_default_real_mode_views(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job(
        company="Zzyzx Test Fixture Co", is_test_fixture=True, external_job_id="cta-fixture-1",
    ))
    _prepare_ready_for_approval(job)
    client = TestClient(app)

    for url, kwargs in (
        ("/jobs", {}),
        ("/", {}),
        ("/applications", {"params": {"bucket": "ready"}}),
    ):
        resp = client.get(url, **kwargs)
        assert resp.status_code == 200
        assert job.company not in resp.text
        assert f'data-job-id="{job.id}"' not in resp.text

    # The opt-in TEST MODE audit view is the one place it's allowed to show up.
    audit_resp = client.get("/", params={"include_test_data": "true"})
    assert job.company in audit_resp.text


# --- Application Detail UX gap closure (2026-09-04): a completed/reconciled
# application must never visually offer Prepare/Queue actions the backend
# would reject anyway (job 454/Anthropic's real terminal reconciliation
# surfaced this) --------------------------------------------------------

def test_reconciled_terminal_application_shows_completed_banner_not_prepare_queue(tmp_env, sample_profile):
    from app.applications.reconcile import reconcile_execution
    from app.db import db_session

    save_profile(sample_profile)
    job = ingest_and_process(_mock_job(external_job_id="terminal-ux-1"))
    result = queue_application(job.id, mode="ASSIST")
    execution_id = result.execution_id
    with db_session() as conn:
        conn.execute(
            "UPDATE application_executions SET status = 'SUBMISSION_STATUS_UNKNOWN' WHERE execution_id = ?",
            (execution_id,),
        )
    reconcile_result = reconcile_execution(execution_id, "manual_applied", note="applied outside the executor")
    assert reconcile_result.ok

    client = TestClient(app)
    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200

    exec_section_start = resp.text.index('id="application-execution"')
    exec_html = resp.text[exec_section_start:]
    assert "Application completed" in exec_html
    assert "applied outside the executor" in exec_html
    # Button text specifically -- unrelated prose elsewhere on the page
    # (e.g. the Browser Assist section's own help text) legitimately still
    # mentions these words in passing.
    assert ">Prepare Application<" not in exec_html
    assert ">Queue Application<" not in exec_html
    # Job-level truthful indicators (top hero) are unaffected by this change
    # -- the CTA badge shows "VIEW RECEIPT" (compute_apply_cta's success-style
    # label override), and the state tag shows APPLIED directly.
    hero_html = resp.text[:exec_section_start]
    assert "tag-neutral\">APPLIED<" in hero_html
    assert "VIEW RECEIPT" in hero_html


def test_withdrawn_reconciliation_still_shows_prepare_queue_for_requeue(tmp_env, sample_profile):
    """confirmed_not_submitted -> WITHDRAWN is explicitly meant to be
    re-queueable (see app.applications.reconcile's own docstring) -- the
    terminal-completed banner must never hide the Prepare/Queue actions for
    THIS resolution, only for a genuinely completed one."""
    from app.applications.reconcile import reconcile_execution
    from app.db import db_session

    save_profile(sample_profile)
    job = ingest_and_process(_mock_job(external_job_id="terminal-ux-2"))
    result = queue_application(job.id, mode="ASSIST")
    execution_id = result.execution_id
    with db_session() as conn:
        conn.execute(
            "UPDATE application_executions SET status = 'SUBMISSION_STATUS_UNKNOWN' WHERE execution_id = ?",
            (execution_id,),
        )
    reconcile_result = reconcile_execution(execution_id, "confirmed_not_submitted")
    assert reconcile_result.ok

    client = TestClient(app)
    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    exec_section_start = resp.text.index('id="application-execution"')
    exec_html = resp.text[exec_section_start:]
    assert "Application completed" not in exec_html
    assert "Prepare Application" in exec_html
    assert "Queue Application" in exec_html
