"""CLAUDE.md Phase 9 sections 2-5: the standalone application-executor
worker daemon -- claim, process (via the real Phase 8 executor pipeline),
attempt history, and lease release. Uses the deterministic mock ATS so no
network is ever touched."""

import json

import pytest

from app import config
from app.applications import attempts as attempts_repo
from app.applications import queue as app_queue
from app.applications.executor import queue_application
from app.applications.repo import get_execution
from app.applications.worker import ApplicationWorker
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


def _mock_job(scenario: str, external_job_id: str, **overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": scenario}),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_worker_claims_and_applies_via_mock_ats(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "worker-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    assert result.queued

    worker = ApplicationWorker(single_cycle=True)
    stats = worker._run_cycle()

    execution = get_execution(result.execution_id)
    assert execution["status"] == "APPLIED"
    assert stats["applied"] == 1
    assert execution["lease_owner"] is None  # released

    attempts = attempts_repo.list_attempts_for_execution(result.execution_id)
    assert len(attempts) == 1
    assert attempts[0]["result"] == "APPLIED"
    assert attempts[0]["confirmation_observed"] == 1


def test_worker_leaves_needs_user_action_for_captcha(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("captcha", "worker-2"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    worker = ApplicationWorker(single_cycle=True)
    stats = worker._run_cycle()

    execution = get_execution(result.execution_id)
    assert execution["status"] == "NEEDS_USER_ACTION"
    assert stats["needs_action"] == 1
    # Never re-claimable via the queue -- status left QUEUED-adjacent state
    # that isn't in the claimable set, so a second cycle finds nothing.
    claimed_again = app_queue.claim_execution_batch(worker_id="someone-else", limit=10, lease_seconds=60)
    assert claimed_again == []


def test_worker_records_attempt_and_does_not_feed_circuit_when_no_submit_attempted(tmp_env, sample_profile, monkeypatch):
    """ASSIST-mode preparation that never reaches provider.submit() (lands in
    SUBMISSION_READY) must not record a circuit-breaker success/failure --
    only genuine submission attempts feed it."""
    from app.applications import circuit as app_circuit

    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "worker-3"))
    result = queue_application(job.id, mode="ASSIST")

    worker = ApplicationWorker(single_cycle=True)
    worker._run_cycle()

    execution = get_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_READY"
    status = app_circuit.get_status("mock_ats")
    assert status.window_attempts == 0


def test_one_blocked_execution_never_stops_others_in_the_same_batch(tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md mission 'one blocked job must never stop global processing':
    an unanticipated exception processing one claimed execution must not
    prevent the other executions claimed in the same worker cycle from being
    processed and applied normally."""
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    # Both jobs share the mock_ats provider -- raise the per-provider
    # concurrency limit so the two claimed executions genuinely run
    # concurrently in this test rather than one being cooldown-skipped
    # (not an error, just deferred) by the provider's own concurrency gate.
    monkeypatch.setattr(config, "APPLICATION_PROVIDER_CONCURRENCY_DEFAULT", 5)
    save_profile(sample_profile)

    blocked_job = ingest_and_process(_mock_job("simple", "blocked-1"))
    ok_job = ingest_and_process(_mock_job("simple", "ok-1"))
    blocked_result = queue_application(blocked_job.id, mode="AUTO_PERMITTED")
    ok_result = queue_application(ok_job.id, mode="AUTO_PERMITTED")

    import app.applications.worker as worker_mod

    real_process_execution = worker_mod.process_execution

    def _boom_for_blocked(execution_id, **kwargs):
        if execution_id == blocked_result.execution_id:
            raise RuntimeError("simulated unanticipated failure")
        return real_process_execution(execution_id, **kwargs)

    monkeypatch.setattr(worker_mod, "process_execution", _boom_for_blocked)

    worker = ApplicationWorker(single_cycle=True)
    stats = worker._run_cycle()

    ok_execution = get_execution(ok_result.execution_id)
    assert ok_execution["status"] == "APPLIED"
    assert stats["applied"] == 1

    blocked_execution = get_execution(blocked_result.execution_id)
    assert blocked_execution["status"] not in ("APPLIED",)
    assert blocked_execution["lease_owner"] is None  # released, not stranded
    attempts = attempts_repo.list_attempts_for_execution(blocked_result.execution_id)
    assert attempts and attempts[0]["result"] == "WORKER_EXCEPTION"


def test_worker_drain_mode_prevents_new_claims(tmp_env, sample_profile, monkeypatch):
    from app.workers import repo as workers_repo
    from app.workers.models import WorkerStatus
    from app.applications.worker_admin import request_drain

    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "worker-4"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    worker = ApplicationWorker(single_cycle=True)
    # Register the worker row without running a live cycle first (run()
    # would immediately process the one queued job before drain could apply).
    workers_repo.upsert_worker(
        worker.identity.worker_id, hostname=worker.identity.hostname, pid=worker.identity.pid,
        shard_index=0, shard_count=1, status=WorkerStatus.STARTING.value,
    )
    request_drain(worker.identity.worker_id)

    stats = worker._run_cycle()
    assert stats == {"claimed": 0, "applied": 0, "needs_action": 0, "submitted": 0, "failed": 0, "deferred_draining": 0}
    execution = get_execution(result.execution_id)
    assert execution["status"] == "QUEUED"  # never touched
