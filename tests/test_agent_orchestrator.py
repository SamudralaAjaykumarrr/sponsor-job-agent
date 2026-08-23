"""One-click autonomous agent orchestrator tests. The mandatory TEST MODE
end-to-end acceptance scenario (CLAUDE.md one-click-agent section 35) drives
app.agent.orchestrator.AgentOrchestrator through its exact synchronous cycle
body (`_run_cycle_sync`) -- the same code the real asyncio loop calls every
interval -- without waiting on real wall-clock sleep timers, so the test
stays fast and deterministic while still exercising the real, unmodified
pipeline (no manual Analyze JD / Generate Resume / Prepare Application /
Queue Application clicks anywhere in this file)."""

import asyncio
from datetime import datetime, timezone

import pytest

from app import config
from app.agent import run_state
from app.agent.orchestrator import AgentOrchestrator
from app.agent.run_state import AgentRunState
from app.candidate.profile import save_profile
from app.jobs_repo import get_job_by_provider_external_id


@pytest.fixture
def orchestrator(tmp_env):
    orch = AgentOrchestrator()
    yield orch
    # best-effort cleanup: restore any config overrides even if a test failed mid-way
    orch._restore_config_overrides()


def test_start_sets_desired_and_actual_state(tmp_env, sample_profile, orchestrator):
    save_profile(sample_profile)

    async def run():
        orchestrator.start(test_mode=False)
        await asyncio.sleep(0.05)
        assert run_state.get_run_state()["desired_state"] == AgentRunState.RUNNING.value
        await orchestrator.stop()

    asyncio.run(run())
    final = run_state.get_run_state()
    assert final["actual_state"] == AgentRunState.STOPPED.value


def test_start_already_running_is_a_no_op(tmp_env, sample_profile, orchestrator):
    save_profile(sample_profile)

    async def run():
        first = orchestrator.start(test_mode=False)
        await asyncio.sleep(0.05)
        second = orchestrator.start(test_mode=False)
        assert first["started"] is True
        assert second["started"] is False
        await orchestrator.stop()

    asyncio.run(run())


def test_stop_restores_config_overrides(tmp_env, sample_profile, orchestrator):
    save_profile(sample_profile)
    original_executor = config.APPLICATION_EXECUTOR_ENABLED
    original_prepare = config.APPLICATION_AUTO_PREPARE_ENABLED
    assert original_executor is False
    assert original_prepare is False

    async def run():
        orchestrator.start(test_mode=False)
        await asyncio.sleep(0.05)
        assert config.APPLICATION_EXECUTOR_ENABLED is True
        assert config.APPLICATION_AUTO_PREPARE_ENABLED is True
        await orchestrator.stop()

    asyncio.run(run())
    assert config.APPLICATION_EXECUTOR_ENABLED == original_executor
    assert config.APPLICATION_AUTO_PREPARE_ENABLED == original_prepare


def test_normal_mode_never_touches_auto_submit(tmp_env, sample_profile, orchestrator):
    """CLAUDE.md one-click-agent section 15/48: AUTO_SUBMIT_ENABLED must stay
    exactly whatever `.env` says for a normal (non-test-mode) run."""
    save_profile(sample_profile)
    assert config.AUTO_SUBMIT_ENABLED is False

    async def run():
        orchestrator.start(test_mode=False)
        await asyncio.sleep(0.05)
        assert config.AUTO_SUBMIT_ENABLED is False
        await orchestrator.stop()

    asyncio.run(run())


def test_seed_test_fixture_is_idempotent(tmp_env, sample_profile, orchestrator):
    save_profile(sample_profile)
    orchestrator._seed_test_fixture_if_needed()
    orchestrator._seed_test_fixture_if_needed()
    from app.db import db_session

    with db_session() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE provider = 'mock_ats' AND external_job_id = 'agent-test-mode-fixture-1'"
        ).fetchone()["c"]
    assert count == 1


def test_full_test_mode_cycle_reaches_applied(tmp_env, sample_profile, orchestrator):
    """CLAUDE.md one-click-agent section 35 (mandatory acceptance): one
    call into the orchestrator's real cycle body -- discover fixture ->
    FULL_TIME confirmed -> sponsorship confirmed -> JD analyzed -> one-page
    resume produced -> claim check PASS -> ATS parse PASS -> application
    prepared -> mock_ats submitted -> confirmation stored -> APPLIED. No
    manual intermediate button clicks."""
    save_profile(sample_profile)

    run_state.set_desired_state(AgentRunState.RUNNING, test_mode=True)
    orchestrator._apply_config_overrides()
    orchestrator._seed_test_fixture_if_needed()
    try:
        started = datetime.now(timezone.utc).isoformat()
        counters = orchestrator._run_cycle_sync(started, test_mode=True)
    finally:
        orchestrator._restore_config_overrides()

    job = get_job_by_provider_external_id("mock_ats", "agent-test-mode-fixture-1")
    assert job is not None
    assert job.application_state.value == "APPLIED"
    assert job.sponsorship_status.value == "CONFIRMED_SPONSOR"
    assert job.resume_docx_path and job.resume_pdf_path
    assert job.promoted_resume_variant_id

    from app.resume_optimizer.repo import get_current_variant

    variant = get_current_variant(job.id)
    assert variant is not None
    assert variant["status"] == "READY"
    assert variant["page_count"] == 1

    assert counters.resumes_generated >= 1
    assert counters.one_page_success >= 1
    assert counters.applications_prepared >= 1
    assert counters.applications_submitted >= 1


def test_orchestrator_continues_after_one_stage_raises(tmp_env, sample_profile, orchestrator, monkeypatch):
    """One stage failing must never abort the rest of the cycle or crash the
    loop -- matches this project's existing 'one bad provider/job never
    aborts the rest' principle, extended to the orchestrator's own stages."""
    save_profile(sample_profile)

    def boom():
        raise RuntimeError("discovery exploded")

    import app.agent.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "run_discovery_cycle", boom)

    orchestrator._apply_config_overrides()
    try:
        started = datetime.now(timezone.utc).isoformat()
        counters = orchestrator._run_cycle_sync(started, test_mode=False)
    finally:
        orchestrator._restore_config_overrides()

    assert counters.errors >= 1


def test_status_reports_recent_cycles(tmp_env, sample_profile, orchestrator):
    save_profile(sample_profile)
    orchestrator._apply_config_overrides()
    try:
        started = datetime.now(timezone.utc)
        counters = orchestrator._run_cycle_sync(started.isoformat(), test_mode=False)
        run_state.record_cycle(started.isoformat(), datetime.now(timezone.utc).isoformat(),
                                test_mode=False, counters=counters)
    finally:
        orchestrator._restore_config_overrides()

    status = orchestrator.status()
    assert status["recent_cycles"]
    assert status["last_cycle"] is not None
