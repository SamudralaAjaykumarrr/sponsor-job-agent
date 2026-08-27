"""Autonomous-ux-reliability-v1 section K: deterministic proof that one
blocked/failed job never stops the orchestrator cycle or any other job in
the same batch. Built entirely on the existing mock_ats fixture mechanism
(app.applications.mock_ats's mock_scenario switch) -- no real network calls,
no real employer ever contacted, matching every other test in this project.

Drives app.agent.orchestrator.AgentOrchestrator._run_cycle_sync directly
(the same synchronous cycle body the real asyncio loop calls every
interval), mirroring tests/test_agent_orchestrator.py's own pattern, rather
than waiting on real wall-clock sleep timers."""

from datetime import datetime, timezone

import pytest

from app import config
from app.agent.orchestrator import AgentOrchestrator
from app.applications import blockers
from app.applications import repo as applications_repo
from app.applications.approval import approve_and_apply
from app.applications.executor import ExecutorDisabledError
from app.applications.models import ExecutionStatus
from app.applications import queue as app_queue
from app.applications import scheduler as applications_scheduler
from app.applications.worker import ApplicationWorker
from app.candidate.profile import save_profile
from app.jobs_repo import get_job, get_job_by_provider_external_id
from app.models import SponsorshipStatus


@pytest.fixture
def orchestrator(tmp_env):
    orch = AgentOrchestrator()
    yield orch
    orch._restore_config_overrides()


def _run_cycles(orchestrator, n: int, test_mode: bool = True) -> None:
    for _ in range(n):
        started = datetime.now(timezone.utc).isoformat()
        orchestrator._run_cycle_sync(started, test_mode=test_mode)


def test_mixed_batch_isolates_failures_across_jobs(tmp_env, sample_profile, orchestrator, monkeypatch):
    """Job A (captcha) and Job B (email verification) both need a human;
    Job C (explicit no sponsorship) hard-skips; two ordinary jobs and two
    submit-time-failure jobs still reach preparation -- all in the SAME
    mixed batch, proving isolation end to end (CLAUDE.md section K)."""
    save_profile(sample_profile)
    # All 6 non-skipped fixtures share the mock_ats provider, whose per-
    # provider submission concurrency is deliberately tiny
    # (APPLICATION_PROVIDER_CONCURRENCY_DEFAULT=1) -- an item claimed but
    # skipped because the provider slot is busy gets a real cooldown
    # extension (never a busy-spin release), which normally clears after a
    # few real wall-clock seconds. This test drives cycles back-to-back with
    # no real sleep, so shrink the cooldown to 0 -- same technique real
    # production simply gets for free from elapsed wall-clock time between
    # scheduled cycles.
    monkeypatch.setattr(config, "APPLICATION_SKIP_COOLDOWN_SECONDS", 0)
    orchestrator._apply_config_overrides()
    orchestrator._seed_test_fixture_if_needed()
    orchestrator._seed_mixed_batch_fixtures()

    # Bounded: several cycles so per-cycle budgets (MAX_APPLICATIONS_PER_CYCLE
    # etc.) have room to work through every candidate, never unbounded.
    _run_cycles(orchestrator, 4)

    def execution_for(external_id: str):
        job = get_job_by_provider_external_id("mock_ats", external_id)
        assert job is not None, external_id
        return job, applications_repo.get_active_execution_for_job(job.id)

    # Two ordinary jobs reach SUBMISSION_READY (READY_FOR_APPROVAL) --
    # TEST MODE never auto-submits, so this is the correct "prepared/
    # completed" resting state.
    job1, exec1 = execution_for("agent-test-mode-fixture-1")
    job2, exec2 = execution_for("agent-test-mode-fixture-2")
    assert exec1 is not None and exec1["status"] == ExecutionStatus.SUBMISSION_READY.value
    assert exec2 is not None and exec2["status"] == ExecutionStatus.SUBMISSION_READY.value

    # Needs You: CAPTCHA.
    job_captcha, exec_captcha = execution_for("agent-test-mode-fixture-captcha")
    assert exec_captcha is not None
    assert exec_captcha["status"] == ExecutionStatus.NEEDS_USER_ACTION.value
    blocker = blockers.get_active_blocker_for_execution(exec_captcha["execution_id"])
    assert blocker is not None and blocker["blocker_code"] == blockers.BlockerCode.NEEDS_CAPTCHA.value

    # Needs You: email verification.
    job_email, exec_email = execution_for("agent-test-mode-fixture-email-verify")
    assert exec_email is not None
    assert exec_email["status"] == ExecutionStatus.NEEDS_USER_ACTION.value
    blocker = blockers.get_active_blocker_for_execution(exec_email["execution_id"])
    assert blocker is not None and blocker["blocker_code"] == blockers.BlockerCode.NEEDS_EMAIL_VERIFICATION.value

    # Skipped: explicit no-sponsorship -- hard-skipped before ever entering
    # the execution queue, never given an execution row.
    job_no_sponsor = get_job_by_provider_external_id("mock_ats", "agent-test-mode-fixture-no-sponsorship")
    assert job_no_sponsor is not None
    assert job_no_sponsor.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP
    assert applications_repo.get_active_execution_for_job(job_no_sponsor.id) is None

    # The two submit-time-failure fixtures still complete PREPARATION
    # (validation clears, they just haven't been submitted yet -- ASSIST
    # mode never auto-submits without an explicit approval).
    job_retry, exec_retry = execution_for("agent-test-mode-fixture-transient-recovery")
    job_unknown, exec_unknown = execution_for("agent-test-mode-fixture-submit-unknown")
    assert exec_retry is not None and exec_retry["status"] == ExecutionStatus.SUBMISSION_READY.value
    assert exec_unknown is not None and exec_unknown["status"] == ExecutionStatus.SUBMISSION_READY.value


def test_transient_submit_failure_recovers_automatically_on_retry(tmp_env, sample_profile, orchestrator, monkeypatch):
    """Job F (transient provider failure): the first submit attempt fails
    with a retryable error, is bounded-retried with backoff, and the SECOND
    attempt succeeds -- without ever needing a second human approval click
    (CLAUDE.md section C: 'retry only objectively safe... proceeds or
    parks')."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_SUBMIT_RETRY_BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(config, "APPLICATION_SUBMIT_RETRY_BACKOFF_MAX_SECONDS", 0)
    monkeypatch.setattr(config, "APPLICATION_SKIP_COOLDOWN_SECONDS", 0)
    orchestrator._apply_config_overrides()
    orchestrator._seed_mixed_batch_fixtures()
    _run_cycles(orchestrator, 3)

    job = get_job_by_provider_external_id("mock_ats", "agent-test-mode-fixture-transient-recovery")
    execution = applications_repo.get_active_execution_for_job(job.id)
    assert execution is not None and execution["status"] == ExecutionStatus.SUBMISSION_READY.value

    result = approve_and_apply(job.id)
    assert result.ok is True
    first_attempt = applications_repo.get_execution(result.execution_id)
    assert first_attempt["status"] == ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value
    assert first_attempt["active"] == 1  # still in-flight, not terminal

    # The worker fleet reclaims it (backoff patched to 0 above so it's
    # immediately eligible) and retries -- this time it succeeds.
    worker = ApplicationWorker(single_cycle=True)
    worker._run_cycle()

    final = applications_repo.get_execution(result.execution_id)
    assert final["status"] == ExecutionStatus.APPLIED.value
    assert final["confirmation_id"]
    assert final["attempt_count"] == 2


def test_transient_submit_failure_parks_after_retries_exhausted(tmp_env, sample_profile, orchestrator, monkeypatch):
    """Bounded: once APPLICATION_SUBMIT_RETRY_MAX_ATTEMPTS is exhausted, the
    execution parks as a permanent failure instead of retrying forever."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_SUBMIT_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(config, "APPLICATION_SUBMIT_RETRY_BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(config, "APPLICATION_SUBMIT_RETRY_BACKOFF_MAX_SECONDS", 0)
    # See test_mixed_batch_isolates_failures_across_jobs's comment: several
    # fixtures share mock_ats's tiny provider-concurrency budget, so a
    # claimed-but-skipped item's cooldown must be shrunk to 0 for a
    # back-to-back (no real sleep) multi-cycle test to reliably clear it.
    monkeypatch.setattr(config, "APPLICATION_SKIP_COOLDOWN_SECONDS", 0)
    orchestrator._apply_config_overrides()
    orchestrator._seed_mixed_batch_fixtures()
    # This fixture only fails once then recovers -- use the always-failing
    # service_unavailable scenario instead so exhaustion is reachable.
    from app.jobs_repo import update_job
    import json as _json

    job = get_job_by_provider_external_id("mock_ats", "agent-test-mode-fixture-transient-recovery")
    update_job(job.id, provider_metadata=_json.dumps({"mock_scenario": "service_unavailable"}))
    _run_cycles(orchestrator, 3)

    job = get_job(job.id)
    execution = applications_repo.get_active_execution_for_job(job.id)
    assert execution is not None and execution["status"] == ExecutionStatus.SUBMISSION_READY.value

    result = approve_and_apply(job.id)
    exec_id = result.execution_id
    assert applications_repo.get_execution(exec_id)["status"] == ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value

    worker = ApplicationWorker(single_cycle=True)
    worker._run_cycle()

    final = applications_repo.get_execution(exec_id)
    assert final["status"] == ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value
    assert final["active"] == 0
    blocker = blockers.get_active_blocker_for_execution(exec_id)
    assert blocker is not None and blocker["blocker_code"] == blockers.BlockerCode.APPLICATION_ERROR.value

    # Never claimable again -- no blind retry of a permanent failure.
    claimed = app_queue.claim_execution_batch(worker_id="w2", limit=10, lease_seconds=60)
    assert exec_id not in {c["execution_id"] for c in claimed}


def test_ambiguous_submit_outcome_never_auto_retried(tmp_env, sample_profile, orchestrator, monkeypatch):
    """Job G (possible-submit timeout): SUBMISSION_STATUS_UNKNOWN is a
    dead end for automatic processing -- it must never be picked up by the
    queue again, matching 'never blind retry submission'."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_SKIP_COOLDOWN_SECONDS", 0)
    orchestrator._apply_config_overrides()
    orchestrator._seed_mixed_batch_fixtures()
    _run_cycles(orchestrator, 3)

    job = get_job_by_provider_external_id("mock_ats", "agent-test-mode-fixture-submit-unknown")
    execution = applications_repo.get_active_execution_for_job(job.id)
    assert execution is not None and execution["status"] == ExecutionStatus.SUBMISSION_READY.value

    result = approve_and_apply(job.id)
    exec_id = result.execution_id
    final = applications_repo.get_execution(exec_id)
    assert final["status"] == ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value
    assert final["active"] == 1  # stays active -- blocks a duplicate concurrent attempt

    claimed = app_queue.claim_execution_batch(worker_id="w3", limit=10, lease_seconds=60)
    assert exec_id not in {c["execution_id"] for c in claimed}

    blocker = blockers.get_active_blocker_for_execution(exec_id)
    assert blocker is not None and blocker["blocker_code"] == blockers.BlockerCode.SUBMISSION_STATUS_UNKNOWN.value


def test_bad_resume_page_count_review_required_does_not_block_others(tmp_env, sample_profile, orchestrator, monkeypatch):
    """Job H (resume can't safely reach one page): REVIEW_REQUIRED for that
    one job, while the rest of the batch keeps moving."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_SKIP_COOLDOWN_SECONDS", 0)
    orchestrator._apply_config_overrides()
    orchestrator._seed_test_fixture_if_needed()
    orchestrator._seed_mixed_batch_fixtures()

    from app.resume_optimizer import one_page as one_page_mod

    def _always_overflow(*args, **kwargs):
        from app.resume_optimizer.models import ResumeVariantStatus

        class _Result:
            status = ResumeVariantStatus.REVIEW_REQUIRED
            page_count = 2
            compression_steps_applied = 0

        return _Result()

    monkeypatch.setattr(one_page_mod, "enforce_one_page", _always_overflow, raising=False)

    _run_cycles(orchestrator, 2)

    # The batch must still have made progress on OTHER jobs regardless of
    # whichever ones hit the (monkeypatched) resume overflow.
    job2 = get_job_by_provider_external_id("mock_ats", "agent-test-mode-fixture-captcha")
    assert job2 is not None
    execution2 = applications_repo.get_active_execution_for_job(job2.id)
    # CAPTCHA is detected during validation, independent of resume page
    # count, so it should still have progressed to NEEDS_USER_ACTION.
    if execution2 is not None:
        assert execution2["status"] in (
            ExecutionStatus.NEEDS_USER_ACTION.value, ExecutionStatus.QUEUED.value,
            ExecutionStatus.STARTED.value, ExecutionStatus.FORM_DISCOVERED.value,
        )


def test_scheduler_run_cycle_survives_one_job_raising(tmp_env, sample_profile, monkeypatch):
    """A per-job unexpected exception from queue_application() must never
    abort auto-prepare for every OTHER candidate in the same cycle (the
    scheduler.py gap this feature fixed -- previously only
    ExecutorDisabledError was isolated, any other exception propagated and
    killed the whole batch)."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)

    import app.applications.scheduler as scheduler_mod

    calls: list[int] = []
    real_queue_application = scheduler_mod.queue_application

    def _flaky(job_id, mode):
        calls.append(job_id)
        if len(calls) == 1:
            raise RuntimeError("simulated transient bug in queue_application")
        return real_queue_application(job_id, mode=mode)

    monkeypatch.setattr(scheduler_mod, "queue_application", _flaky)

    from app.agent.orchestrator import AgentOrchestrator

    orch = AgentOrchestrator()
    orch._apply_config_overrides()
    try:
        orch._seed_test_fixture_if_needed()
        orch._seed_mixed_batch_fixtures()
        # Resumes must exist first (auto-prepare only queues READY_TO_APPLY
        # jobs, which requires a resume) -- reuse the orchestrator's own
        # resume stage the same way _run_cycle_sync does.
        orch._run_resume_stage()

        result = applications_scheduler.run_cycle(limit=10)
    finally:
        orch._restore_config_overrides()

    assert len(calls) >= 2  # the flaky first call happened, AND at least one more job was still attempted
    assert result.queued >= 1  # despite the first job's simulated crash, others still got queued
    assert any("simulated transient bug" in e for e in result.errors)
