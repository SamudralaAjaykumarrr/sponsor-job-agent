"""Tsenta Remaining-Gaps Closure V2: HTTP-level coverage for recording a
recruiter/application update and rendering it on the application detail
page, plus the truthful "no mailbox connected" state. TestClient only --
no real network, no real employer."""

import pytest
from fastapi.testclient import TestClient

from app.applications import repo as applications_repo
from app.applications.models import ExecutionStatus
from app.jobs_repo import insert_job
from app.main import app
from app.models import ApplicationMode, Job


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _make_job(external_job_id: str) -> int:
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="full-time backend role", employment_type="Full-time", provider="lever",
        external_job_id=external_job_id, mode=ApplicationMode.ASSIST,
    )
    return insert_job(job)


def test_record_update_for_job_with_no_execution_redirects_to_job_page(tmp_env):
    job_id = _make_job("recruiter-route-1")
    client = TestClient(app)
    resp = client.post(
        f"/jobs/{job_id}/recruiter-updates",
        data={"update_type": "STATUS_CHECK_IN", "subject": "checking in"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}"


def test_record_update_for_job_with_active_execution_redirects_to_detail(tmp_env):
    job_id = _make_job("recruiter-route-2")
    execution_id = applications_repo.create_execution(job_id, provider="lever", mode="ASSIST")
    applications_repo.update_execution(execution_id, job_id, ExecutionStatus.APPROVED)

    client = TestClient(app)
    resp = client.post(
        f"/jobs/{job_id}/recruiter-updates",
        data={"update_type": "INTERVIEW_REQUEST", "subject": "Can we talk Tuesday?"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/applications/{execution_id}/detail#detail-post-application"

    detail = client.get(f"/applications/{execution_id}/detail")
    assert detail.status_code == 200
    assert "Can we talk Tuesday?" in detail.text
    assert "Interview Request" in detail.text


def test_record_update_unknown_job_404s(tmp_env):
    client = TestClient(app)
    resp = client.post("/jobs/999999/recruiter-updates", data={"update_type": "STATUS_CHECK_IN"})
    assert resp.status_code == 404


def test_record_update_invalid_type_400s(tmp_env):
    job_id = _make_job("recruiter-route-3")
    client = TestClient(app)
    resp = client.post(f"/jobs/{job_id}/recruiter-updates", data={"update_type": "NOT_A_TYPE"})
    assert resp.status_code == 400


def test_application_detail_shows_mailbox_not_connected_state(tmp_env):
    job_id = _make_job("recruiter-route-4")
    execution_id = applications_repo.create_execution(job_id, provider="lever", mode="ASSIST")
    applications_repo.update_execution(execution_id, job_id, ExecutionStatus.APPROVED)

    client = TestClient(app)
    detail = client.get(f"/applications/{execution_id}/detail")
    assert detail.status_code == 200
    assert "No mailbox is connected" in detail.text
