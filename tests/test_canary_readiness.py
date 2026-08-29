"""Tsenta Remaining-Gaps Closure V2, section 5: verified-real-submission
READINESS reporting (`app.applications.canary_readiness`). No Playwright, no
real network -- same mocked-Greenhouse-API pattern as
tests/test_greenhouse_submit_contract.py. Never asserts submission_supported
becomes True; that must stay False throughout."""

import httpx
import pytest

from app.applications import approval as applications_approval
from app.applications import provider_registry
from app.applications.canary_readiness import ReadinessLevel, best_canary_candidate, greenhouse_readiness
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process
from app import config

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)

MINIMAL_PAYLOAD = {
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


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)


def _install_mock_greenhouse(payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    return original


def _restore(original):
    provider_registry._PROVIDERS["greenhouse"] = original


def _make_job(external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
        external_job_id=external_job_id, company_identifier="acme", mode=ApplicationMode.ASSIST,
    )


def _drive_to_approved(job: Job) -> str:
    result = queue_application(job.id, mode="ASSIST")
    assert result.queued, result.reason
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value, execution
    approved = applications_approval.approve_and_apply(job.id)
    assert approved.ok, approved.reason
    return result.execution_id


def test_job_not_found():
    report = greenhouse_readiness(999999)
    assert report.level == ReadinessLevel.JOB_NOT_FOUND
    assert report.submission_supported is False


def test_no_active_execution_yet(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-r-noexec"))
        report = greenhouse_readiness(job.id)
        assert report.level == ReadinessLevel.NO_ACTIVE_EXECUTION
        assert report.submission_supported is False
    finally:
        _restore(original)


def test_infrastructure_ready_when_everything_current(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-r-ready"))
        _drive_to_approved(job)

        report = greenhouse_readiness(job.id)
        assert report.level == ReadinessLevel.INFRASTRUCTURE_READY
        assert report.submission_supported is False, "readiness must never imply submission_supported=True"
        assert report.blocking_reasons == []
        # the two browser-time steps must stay NOT_YET_CHECKED -- never
        # fabricated as PASSED just because everything else checked out
        browser_steps = {s.number: s.status.value for s in report.contract.steps if s.number in (7, 8)}
        assert browser_steps == {7: "NOT_YET_CHECKED", 8: "NOT_YET_CHECKED"}
    finally:
        _restore(original)


def test_not_ready_when_a_browser_independent_step_fails(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-r-notready"))
        # queued but never approved -- no durable approval row, so step 4
        # ("approved_answer_set") fails and readiness must reflect that.
        result = queue_application(job.id, mode="ASSIST")
        assert result.queued
        process_execution(result.execution_id)

        report = greenhouse_readiness(job.id)
        assert report.level in (ReadinessLevel.NOT_READY, ReadinessLevel.NO_ACTIVE_EXECUTION)
        assert report.submission_supported is False
    finally:
        _restore(original)


def test_best_canary_candidate_is_greenhouse_and_never_recommends_submitting():
    result = best_canary_candidate()
    assert result["provider"] == "greenhouse"
    assert "reason" in result and result["reason"]
    lowered = result["reason"].lower()
    assert "should submit" not in lowered
    assert "you should now submit" not in lowered
