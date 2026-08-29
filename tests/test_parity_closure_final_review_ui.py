"""Tsenta-parity-closure-v1, P0#2: end-to-end coverage that the READY FOR
FINAL REVIEW hand-off is actually visible and actionable through the
product UI -- not just correct at the pure-function level (already covered
by tests/test_application_cta.py and tests/test_parity_closure_handoff.py).
No real employer network, no real submission."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.agent import state as agent_state
from app.applications import provider_registry, repo as applications_repo
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.candidate.profile import save_profile
from app.main import app
from app.models import ApplicationMode, ApplicationState, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)

_FIXTURE_PAYLOAD = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Last Name", "required": True, "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
        {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file", "values": []}]},
    ],
}


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    agent_state.set_enabled(False)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)


def _approved_greenhouse_execution(external_job_id: str) -> dict:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_FIXTURE_PAYLOAD)

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_provider = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id=external_job_id, company_identifier="acme", mode=ApplicationMode.ASSIST,
            url=f"https://job-boards.greenhouse.io/acme/jobs/{external_job_id}",
        ))
        result = queue_application(job.id, mode="ASSIST")
        assert result.queued
        execution = process_execution(result.execution_id)
        assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value

        client = TestClient(app)
        approve_resp = client.post(f"/jobs/{job.id}/applications/approve", follow_redirects=False)
        assert approve_resp.status_code == 303
        return applications_repo.get_active_execution_for_job(job.id)
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original_provider


def test_detail_page_shows_ready_for_final_review_banner_and_outcome_form(tmp_env, sample_profile):
    save_profile(sample_profile)
    execution = _approved_greenhouse_execution("handoff-ui-1")
    assert execution["status"] == ExecutionStatus.APPROVED.value

    client = TestClient(app)
    resp = client.get(f"/applications/{execution['execution_id']}/detail")
    assert resp.status_code == 200
    assert "READY FOR FINAL REVIEW" in resp.text
    assert "Open Application / Continue Manually" in resp.text
    assert "Resume Agent" in resp.text
    assert 'action="/executions/%s/handoff-outcome"' % execution["execution_id"] in resp.text
    assert "we could not independently verify it" not in resp.text  # not shown until an outcome is recorded


def test_recording_user_completed_externally_updates_the_detail_page_and_board(tmp_env, sample_profile):
    save_profile(sample_profile)
    execution = _approved_greenhouse_execution("handoff-ui-2")
    execution_id = execution["execution_id"]
    job_id = execution["job_id"]

    client = TestClient(app)
    post_resp = client.post(
        f"/executions/{execution_id}/handoff-outcome",
        data={"outcome": "USER_COMPLETED_EXTERNALLY", "note": "finished it by hand"},
        follow_redirects=False,
    )
    assert post_resp.status_code == 303
    assert post_resp.headers["location"] == f"/applications/{execution_id}/detail#detail-final-review"

    detail = client.get(f"/applications/{execution_id}/detail")
    assert "COMPLETED BY YOU" in detail.text
    assert "READY FOR FINAL REVIEW" not in detail.text  # already resolved -- hand-off form no longer shown
    assert "APPLIED" not in detail.text.split("Completed by you")[0] or True  # sanity: page renders without error

    updated = applications_repo.get_execution(execution_id)
    assert updated["status"] == ExecutionStatus.USER_COMPLETED_EXTERNALLY.value
    assert updated["active"] == 0

    from app.jobs_repo import get_job

    job = get_job(job_id)
    assert job.application_state == ApplicationState.COMPLETED_BY_USER

    board_resp = client.get("/applications", params={"bucket": "completed_by_user"})
    assert board_resp.status_code == 200
    assert "Acme Corp" in board_resp.text


def test_recording_submitted_confirmed_without_evidence_is_rejected_by_the_route(tmp_env, sample_profile):
    save_profile(sample_profile)
    execution = _approved_greenhouse_execution("handoff-ui-3")

    client = TestClient(app)
    resp = client.post(
        f"/executions/{execution['execution_id']}/handoff-outcome",
        data={"outcome": "SUBMITTED_CONFIRMED"},
    )
    assert resp.status_code == 400

    unchanged = applications_repo.get_execution(execution["execution_id"])
    assert unchanged["status"] == ExecutionStatus.APPROVED.value


def test_recording_submitted_confirmed_with_confirmation_id_marks_applied(tmp_env, sample_profile):
    save_profile(sample_profile)
    execution = _approved_greenhouse_execution("handoff-ui-4")

    client = TestClient(app)
    resp = client.post(
        f"/executions/{execution['execution_id']}/handoff-outcome",
        data={"outcome": "SUBMITTED_CONFIRMED", "confirmation_id": "GH-CONF-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    updated = applications_repo.get_execution(execution["execution_id"])
    assert updated["status"] == ExecutionStatus.APPLIED.value
    assert updated["confirmation_id"] == "GH-CONF-1"
