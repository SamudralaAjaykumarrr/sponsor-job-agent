"""CLAUDE.md Phase 9 section 11: deterministic daily application budget
accounting. Verifies PREPARE-only operations are never counted as
'submitted'."""

import json

import pytest

from app import config
from app.applications import budget as app_budget
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


def _mock_job(scenario: str, external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": scenario}),
        mode=ApplicationMode.ASSIST,
    )


def test_prepare_only_run_never_counts_as_submitted(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "budget-1"))
    result = queue_application(job.id, mode="ASSIST")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_READY"

    b = app_budget.collect()
    assert b.submitted_today == 0
    assert b.confirmed_today == 0


def test_confirmed_application_counts_submitted_and_confirmed(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "budget-2"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "APPLIED"

    b = app_budget.collect()
    assert b.submitted_today == 1
    assert b.confirmed_today == 1
    assert b.as_dict()["daily_budget_remaining"] == config.MAX_APPLICATIONS_PER_DAY - 1


def test_needs_user_action_counted_separately_from_failed(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("captcha", "budget-3"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "NEEDS_USER_ACTION"

    b = app_budget.collect()
    assert b.needs_user_action_today == 1
    assert b.failed_today == 0
    assert b.submitted_today == 0
