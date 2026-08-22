"""CLAUDE.md Phase 9 section 8: automated reconciliation evidence pass.
Verifies it funnels through the existing app.applications.reconcile
.reconcile_execution() (never fabricates a resolution) and leaves any
provider without confirmation_recheck_supported completely untouched."""

import json

import pytest

from app import config
from app.applications.executor import process_execution, queue_application
from app.applications.reconcile_worker import run_pass
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


def test_auto_resolves_to_applied_when_provider_has_genuine_evidence(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("timeout_after_submit", "rw-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_STATUS_UNKNOWN"

    outcome = run_pass()
    assert outcome.checked == 1
    assert outcome.auto_resolved_applied == 1

    from app.applications.repo import get_execution

    resolved = get_execution(result.execution_id)
    assert resolved["status"] == "APPLIED"
    assert resolved["confirmation_id"]


def test_auto_resolves_to_withdrawn_when_provider_confirms_no_record(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("timeout_before_submit", "rw-2"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    process_execution(result.execution_id)

    outcome = run_pass()
    assert outcome.auto_resolved_not_submitted == 1

    from app.applications.repo import get_execution

    resolved = get_execution(result.execution_id)
    assert resolved["status"] == "WITHDRAWN"


def test_never_touches_execution_for_provider_without_recheck_support(tmp_env, sample_profile):
    """A real ATS adapter (greenhouse/lever/generic) declares
    confirmation_recheck_supported=False -- the reconciliation worker must
    leave any such execution completely alone."""
    save_profile(sample_profile)
    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="lever",
        external_job_id="rw-3", mode=ApplicationMode.ASSIST,
    ))
    result = queue_application(job.id, mode="ASSIST")
    from app.applications import repo
    from app.applications.models import ExecutionStatus

    repo.update_execution(result.execution_id, job.id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN,
                           requires_user_action=1, user_action_reason="test setup")

    outcome = run_pass()
    assert outcome.checked == 0
    assert outcome.unsupported_provider == 1

    from app.applications.repo import get_execution

    still_unknown = get_execution(result.execution_id)
    assert still_unknown["status"] == "SUBMISSION_STATUS_UNKNOWN"
