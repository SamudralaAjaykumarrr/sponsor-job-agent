"""CLAUDE.md Phase 8 sections 46, 62: application rate limiting."""

import json

import pytest

from app import config
from app.applications.executor import process_execution, queue_application
from app.applications.rate_limit import check_rate_limits
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
    "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
    "H-1B sponsorship is available for this role."
)


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)


@pytest.fixture
def profile_saved(tmp_env, sample_profile):
    save_profile(sample_profile)
    return sample_profile


def _mock_job(external_job_id: str, company: str = "Acme Corp", title: str = "Backend Software Engineer") -> Job:
    return Job(
        title=title, company=company, location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )


def _apply(job_ref, mode="AUTO_PERMITTED"):
    result = queue_application(job_ref.id, mode=mode)
    return process_execution(result.execution_id) if result.queued else None


def test_hourly_rate_limit_blocks_further_submissions(profile_saved, monkeypatch):
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 2)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_DAY", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 100)

    jobs = [ingest_and_process(_mock_job(f"h{i}", company=f"Company{i}")) for i in range(3)]
    outcomes = [_apply(j)["status"] for j in jobs]

    assert outcomes[0] == "APPLIED"
    assert outcomes[1] == "APPLIED"
    assert outcomes[2] == "NEEDS_USER_ACTION"


def test_per_company_daily_limit(profile_saved, monkeypatch):
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_DAY", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 1)

    jobs = [ingest_and_process(_mock_job(f"c{i}", company="SameCo", title=f"Backend Software Engineer {i}"))
            for i in range(2)]
    outcomes = [_apply(j)["status"] for j in jobs]

    assert outcomes[0] == "APPLIED"
    assert outcomes[1] == "NEEDS_USER_ACTION"


def test_check_rate_limits_allows_when_under_thresholds(profile_saved):
    result = check_rate_limits("Nobody Yet Inc")
    assert result.allowed
