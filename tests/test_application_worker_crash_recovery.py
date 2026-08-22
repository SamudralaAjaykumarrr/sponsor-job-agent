"""CLAUDE.md Phase 9 acceptance scenarios D/E: worker crash recovery, and the
critical "may have reached the provider" ambiguous-outcome safety net. Exercises
the REAL app.applications.executor.process_execution()/app.applications.queue
mechanics -- not mocks."""

import json

import pytest

from app import config
from app.applications import queue as app_queue
from app.applications.executor import queue_application, process_execution
from app.applications.models import ExecutionStatus
from app.applications.repo import get_execution
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


def test_resuming_a_row_stuck_in_submitting_never_calls_submit_twice(tmp_env, sample_profile):
    """Scenario E: simulates a worker crash that happened AFTER
    executor.process_execution() wrote SUBMITTING but before it recorded any
    final outcome (e.g. killed mid-HTTP-request). Resuming must NEVER call
    provider.submit() again -- it must convert straight to
    SUBMISSION_STATUS_UNKNOWN."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "crash-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    from app.applications import repo

    # Simulate the crash point directly: status flipped to SUBMITTING but the
    # function never got to record a final outcome.
    repo.update_execution(result.execution_id, job.id, ExecutionStatus.SUBMITTING, attempt_count=1)

    execution = process_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_STATUS_UNKNOWN"
    assert execution["requires_user_action"] == 1

    # A second call (e.g. another worker reclaiming after lease expiry) is
    # STILL a pure no-op -- never blindly retried.
    execution_again = process_execution(result.execution_id)
    assert execution_again["status"] == "SUBMISSION_STATUS_UNKNOWN"


def test_resuming_a_row_stuck_in_submitted_also_becomes_unknown(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "crash-2"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    from app.applications import repo

    repo.update_execution(result.execution_id, job.id, ExecutionStatus.SUBMITTED, submission_method="mock_ats")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_STATUS_UNKNOWN"


def test_worker_dies_before_submit_lease_recovered_and_completes(tmp_env, sample_profile):
    """Scenario D: a crash before submit() ever runs simply leaves the
    execution QUEUED-adjacent with an expired lease -- claim_execution_batch
    picks it back up and runs it to completion normally."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "crash-3"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    # Simulate: worker A claims it, then crashes before ever calling
    # process_execution() (lease immediately expired, like a crash).
    claimed = app_queue.claim_execution_batch(worker_id="worker-A-dead", limit=10, lease_seconds=-1)
    assert any(c["execution_id"] == result.execution_id for c in claimed)

    # Worker B reclaims (lease already expired) and runs it to completion.
    reclaimed = app_queue.claim_execution_batch(worker_id="worker-B", limit=10, lease_seconds=60)
    assert any(c["execution_id"] == result.execution_id for c in reclaimed)

    execution = process_execution(result.execution_id)
    assert execution["status"] == "APPLIED"
    app_queue.release_execution_lease(result.execution_id)


def test_full_pipeline_progress_is_reclaimable_after_lease_expiry(tmp_env, sample_profile):
    """A crash after the executor advanced past QUEUED (e.g. FORM_DISCOVERED)
    but before reaching SUBMITTING must also be reclaimable once its lease
    expires -- not just a fresh QUEUED row."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "crash-4"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    from app.applications import repo

    repo.update_execution(result.execution_id, job.id, ExecutionStatus.FORM_DISCOVERED)
    claimed = app_queue.claim_execution_batch(worker_id="worker-C", limit=10, lease_seconds=-1)
    assert any(c["execution_id"] == result.execution_id for c in claimed)

    reclaimed = app_queue.claim_execution_batch(worker_id="worker-D", limit=10, lease_seconds=60)
    assert any(c["execution_id"] == result.execution_id for c in reclaimed)
