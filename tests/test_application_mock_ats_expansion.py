"""CLAUDE.md Phase 9 section 41: expanded mock ATS sandbox scenarios not
already covered by Phase 8's test_applications_mock_ats.py."""

import json

import pytest

from app import config
from app.applications.executor import process_execution, queue_application
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)


def _mock_job(scenario: str, external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": scenario}),
        mode=ApplicationMode.ASSIST,
    )


def _run(sample_profile, scenario: str, external_job_id: str) -> dict:
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job(scenario, external_job_id))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    return process_execution(result.execution_id)


def test_login_required_blocks_auto_submit(tmp_env, sample_profile):
    execution = _run(sample_profile, "login_required", "m-login")
    assert execution["status"] == "NEEDS_USER_ACTION"
    assert "AUTH_REQUIRED" in (execution.get("policy_reasons") or "")


def test_rate_limited_submission_is_retryable_not_status_unknown(tmp_env, sample_profile):
    """Autonomous-ux-reliability-v1 section C: a rate-limited submit is a
    DEFINITE (never ambiguous) failure the provider affirmatively did not
    process -- retried with bounded backoff, never treated as an unknown
    outcome. Never a permanent failure on the FIRST attempt (see the
    exhaustion test below for what happens after retries run out)."""
    execution = _run(sample_profile, "rate_limited", "m-429")
    assert execution["status"] == "RETRYABLE_SUBMISSION_FAILURE"
    assert execution["error_type"] == "RATE_LIMITED"
    assert execution["active"] == 1


def test_service_unavailable_submission_is_retryable(tmp_env, sample_profile):
    execution = _run(sample_profile, "service_unavailable", "m-503")
    assert execution["status"] == "RETRYABLE_SUBMISSION_FAILURE"
    assert execution["error_type"] == "TEMPORARY_HTTP"
    assert execution["active"] == 1


def test_retryable_submission_failure_becomes_permanent_after_retries_exhausted(tmp_env, sample_profile, monkeypatch):
    """service_unavailable never recovers on its own in this scenario, so
    repeated reprocessing (mirroring what the worker fleet's backoff-then-
    reclaim loop does) must eventually give up -- bounded, never an infinite
    retry loop."""
    monkeypatch.setattr(config, "APPLICATION_SUBMIT_RETRY_MAX_ATTEMPTS", 2)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("service_unavailable", "m-503-exhaust"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    first = process_execution(result.execution_id)
    assert first["status"] == "RETRYABLE_SUBMISSION_FAILURE"

    second = process_execution(result.execution_id)
    assert second["status"] == "PERMANENT_SUBMISSION_FAILURE"
    assert second["active"] == 0


def test_rejection_scenario(tmp_env, sample_profile):
    execution = _run(sample_profile, "rejection", "m-rejected")
    assert execution["status"] == "PERMANENT_SUBMISSION_FAILURE"
    assert execution["error_type"] == "SUBMISSION_REJECTED"


def test_duplicate_application_scenario(tmp_env, sample_profile):
    execution = _run(sample_profile, "duplicate_application", "m-dup")
    assert execution["status"] == "PERMANENT_SUBMISSION_FAILURE"


def test_multi_page_form_is_discovered_with_total_steps(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("multi_page", "m-multipage"))
    from app.applications.mock_ats import MockATSProvider

    form = MockATSProvider().discover_form(job)
    assert form is not None
    assert form.total_steps == 2
    assert any(f.name == "education_school" for f in form.fields)


def test_conditional_sponsorship_field_is_mapped_and_filled(tmp_env, sample_profile):
    execution = _run(sample_profile, "conditional_sponsorship", "m-conditional")
    assert execution["status"] == "APPLIED"


def test_job_removed_scenario_blocks_before_submission(tmp_env, sample_profile):
    execution = _run(sample_profile, "job_removed", "m-removed")
    assert execution["status"] == "JOB_NO_LONGER_ACTIVE"


def test_form_not_found_scenario(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("form_not_found", "m-noform"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    # No form -> ASSIST-only draft, never a submission.
    assert execution["status"] in ("NEEDS_USER_ACTION", "SUBMISSION_READY")


def test_timeout_before_submit_leaves_no_server_side_record(tmp_env, sample_profile):
    execution = _run(sample_profile, "timeout_before_submit", "m-timeout-before")
    assert execution["status"] == "SUBMISSION_STATUS_UNKNOWN"

    from app.jobs_repo import get_job
    from app.applications.provider_registry import get_application_provider

    job = get_job(execution["job_id"])
    provider = get_application_provider(job)
    confirmation = provider.check_submission_status(job, execution)
    assert confirmation.confirmed is False  # genuinely no server-side record


def test_timeout_after_submit_has_genuine_server_side_evidence(tmp_env, sample_profile):
    execution = _run(sample_profile, "timeout_after_submit", "m-timeout-after")
    assert execution["status"] == "SUBMISSION_STATUS_UNKNOWN"

    from app.jobs_repo import get_job
    from app.applications.provider_registry import get_application_provider

    job = get_job(execution["job_id"])
    provider = get_application_provider(job)
    confirmation = provider.check_submission_status(job, execution)
    assert confirmation.confirmed is True
    assert confirmation.confirmation_id
