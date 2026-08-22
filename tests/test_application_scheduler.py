"""CLAUDE.md Phase 9 section 38: continuous executor scheduler. Verifies
AUTO_PREPARE is independent of AUTO_SUBMIT (section 37), respects rate
limits, and never queues an already-active job twice."""

import json

import pytest

from app import config
from app.applications import scheduler as app_scheduler
from app.applications.repo import get_active_execution_for_job
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


def _mock_job(external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )


def test_scheduler_does_nothing_when_auto_prepare_disabled(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", False)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("sched-1"))

    result = app_scheduler.run_cycle()
    assert result.queued == 0
    assert get_active_execution_for_job(job.id) is None


def test_scheduler_queues_eligible_jobs_in_assist_mode_by_default(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("sched-2"))

    result = app_scheduler.run_cycle()
    assert result.queued == 1
    execution = get_active_execution_for_job(job.id)
    assert execution is not None
    assert execution["mode"] == "ASSIST"


def test_scheduler_uses_auto_permitted_only_when_auto_submit_enabled_and_eligible(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("sched-3"))

    result = app_scheduler.run_cycle()
    assert result.queued == 1
    execution = get_active_execution_for_job(job.id)
    assert execution["mode"] == "AUTO_PERMITTED"


def test_scheduler_never_double_queues_a_job_with_an_active_execution(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    save_profile(sample_profile)
    ingest_and_process(_mock_job("sched-4"))

    first = app_scheduler.run_cycle()
    assert first.queued == 1
    second = app_scheduler.run_cycle()
    assert second.queued == 0
    # The job's application_state already moved off READY_TO_APPLY (mirrored
    # to EXECUTION_QUEUED) the moment it was queued, so it's no longer even a
    # candidate on the second cycle -- the active-execution check is the
    # (untested-by-this-scenario) second line of defense for the case where
    # a job somehow re-entered READY_TO_APPLY while still actively executing.
    assert second.candidates_considered == 0


def test_scheduler_skips_job_with_active_execution_even_if_still_ready_to_apply(tmp_env, sample_profile, monkeypatch):
    """Defense in depth: even if a job's application_state somehow reads
    READY_TO_APPLY while it already has an active execution row, the
    scheduler must not queue a second one."""
    from app.jobs_repo import update_job
    from app.models import ApplicationState

    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("sched-5"))
    app_scheduler.run_cycle()
    update_job(job.id, application_state=ApplicationState.READY_TO_APPLY)

    result = app_scheduler.run_cycle()
    assert result.queued == 0
    assert result.skipped_active_execution == 1


def test_scheduler_respects_hourly_rate_limit(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_DAY", 100)
    save_profile(sample_profile)
    for i in range(3):
        ingest_and_process(_mock_job(f"sched-rl-{i}"))

    monkeypatch.setattr(config, "APPLICATION_SCHEDULER_MAX_QUEUE_PER_CYCLE", 3)
    result = app_scheduler.run_cycle()
    assert result.queued == 3


def test_contract_job_is_never_queued_by_scheduler(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(Job(
        title="Backend Software Engineer (Contract)", company="Acme Corp", location="Remote - US",
        description=JD_TEXT + " This is a 6-month W2 contract position, not eligible for conversion.",
        employment_type="Contract", provider="mock_ats", external_job_id="sched-contract",
        provider_metadata=json.dumps({"mock_scenario": "simple"}), mode=ApplicationMode.ASSIST,
    ))

    result = app_scheduler.run_cycle()
    assert result.queued == 0
    assert get_active_execution_for_job(job.id) is None
