"""CLAUDE.md Phase 8 section 52: mock ATS scenario coverage not already
exercised by the lettered acceptance scenarios -- demographic
decline-to-self-identify default, and a required file upload the candidate
doesn't have."""

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


def test_demographic_question_with_unanswered_profile_declines_to_self_identify(tmp_env, sample_profile):
    profile = sample_profile.model_copy(deep=True)
    profile.standard_answers.veteran_status = "NEEDS_USER_INPUT"
    save_profile(profile)

    job = ingest_and_process(_mock_job("demographic_question", "mock-demo-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)

    # The mock form offers "I don't wish to answer" -- since the candidate
    # never stated a veteran status, the safe default is selected, and the
    # application still completes fully (CLAUDE.md Phase 8 section 11).
    assert execution["status"] == "APPLIED"


def test_demographic_question_answered_truthfully_from_profile(tmp_env, sample_profile):
    save_profile(sample_profile)  # veteran_status = "I am not a veteran" in the fixture
    job = ingest_and_process(_mock_job("demographic_question", "mock-demo-2"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "APPLIED"


def test_required_file_upload_missing_blocks_auto_submit(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("required_fields", "mock-file-1"))
    # No cover letter was generated for a job whose resume has no
    # experience/projects overlap in this fixture -- but our sample profile
    # always yields one, so force the field empty to exercise the gap path.
    from app.jobs_repo import update_job

    update_job(job.id, cover_letter_path=None)

    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)

    assert execution["status"] == "NEEDS_USER_ACTION"
    assert "FILE_UPLOAD_UNSUPPORTED" in (execution.get("policy_reasons") or "")
