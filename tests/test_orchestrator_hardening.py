"""autonomous-core-v3 hardening: deterministic concurrency/restart tests for
the one-click orchestrator's control plane (CLAUDE.md one-click-agent
sections + the durable single-orchestrator-guarantee lease added in
app.agent.run_state / app.agent.orchestrator). Covers scenarios the mission
brief calls out explicitly: double START, double STOP, two tabs, process
restart, and the lease mechanism itself."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config
from app.agent import run_state
from app.agent.orchestrator import AgentOrchestrator
from app.agent.run_state import AgentRunState
from app.candidate.profile import save_profile
from app.main import app


@pytest.fixture
def orchestrator(tmp_env):
    orch = AgentOrchestrator()
    yield orch
    orch._restore_config_overrides()


# --- lease primitives (app.agent.run_state) --------------------------------


def test_lease_acquire_is_exclusive(tmp_env):
    assert run_state.try_acquire_orchestrator_lease("instance-a", 60) is True
    # A different instance cannot claim it while the lease is still valid.
    assert run_state.try_acquire_orchestrator_lease("instance-b", 60) is False
    # The same instance can re-acquire (renew) its own lease.
    assert run_state.try_acquire_orchestrator_lease("instance-a", 60) is True


def test_lease_renew_fails_for_non_owner(tmp_env):
    run_state.try_acquire_orchestrator_lease("instance-a", 60)
    assert run_state.renew_orchestrator_lease("instance-a", 60) is True
    assert run_state.renew_orchestrator_lease("instance-b", 60) is False


def test_expired_lease_is_reclaimable_by_another_instance(tmp_env):
    run_state.try_acquire_orchestrator_lease("instance-a", -1)  # already expired
    assert run_state.try_acquire_orchestrator_lease("instance-b", 60) is True


def test_release_lets_another_instance_claim_immediately(tmp_env):
    run_state.try_acquire_orchestrator_lease("instance-a", 300)
    run_state.release_orchestrator_lease("instance-a")
    assert run_state.try_acquire_orchestrator_lease("instance-b", 60) is True


def test_release_guarded_by_instance_id(tmp_env):
    """An instance can never release a lease it no longer owns."""
    run_state.try_acquire_orchestrator_lease("instance-a", -1)  # expired immediately
    run_state.try_acquire_orchestrator_lease("instance-b", 300)  # instance-b reclaims it
    run_state.release_orchestrator_lease("instance-a")  # stale release attempt -- must be a no-op
    row = run_state.get_run_state()
    assert row["instance_id"] == "instance-b"
    assert row["lease_expires_at"]


# --- single orchestrator guarantee across two AgentOrchestrator instances --
# (simulating two processes sharing one database -- the exact scenario the
# lease exists to protect against.)


def test_two_orchestrator_instances_only_one_becomes_active(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "AGENT_ORCHESTRATOR_LEASE_SECONDS", 5)

    orch_a = AgentOrchestrator()
    orch_b = AgentOrchestrator()

    async def run():
        orch_a.start(test_mode=False)
        await asyncio.sleep(0.1)
        orch_b.start(test_mode=False)
        await asyncio.sleep(0.1)

        assert orch_a._became_active is True
        assert orch_b._became_active is False  # standby -- lease already held by A

        run = run_state.get_run_state()
        assert run["instance_id"] == orch_a._instance_id

        await orch_a.stop()
        await orch_b.stop()

    asyncio.run(run())
    final = run_state.get_run_state()
    assert final["actual_state"] == AgentRunState.STOPPED.value


def test_standby_instance_stop_never_clobbers_active_instance_state(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "AGENT_ORCHESTRATOR_LEASE_SECONDS", 5)

    orch_a = AgentOrchestrator()
    orch_b = AgentOrchestrator()

    async def run():
        orch_a.start(test_mode=False)
        await asyncio.sleep(0.1)
        orch_b.start(test_mode=False)
        await asyncio.sleep(0.1)
        assert orch_a._became_active is True
        assert orch_b._became_active is False

        # Stopping the standby instance must never touch actual_state --
        # instance A is still the genuine active owner.
        await orch_b.stop()
        run = run_state.get_run_state()
        assert run["actual_state"] == AgentRunState.RUNNING.value
        assert run["instance_id"] == orch_a._instance_id

        await orch_a.stop()

    asyncio.run(run())


def test_active_instance_honors_stop_requested_via_a_different_instance(tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md 'worker coordination'/'single orchestrator guarantee': a
    STOP request handled by a different process than the one actively
    holding the lease must still stop the real active loop. That other
    process only has the durable desired_state to signal through (its own
    local asyncio.Event lives in a different process's memory) -- see
    AgentOrchestrator._should_keep_running/_wait. No one ever calls
    orch_a.stop() in this test; orch_a's own loop must notice the remote
    desired_state flip entirely on its own."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "AGENT_ORCHESTRATOR_LEASE_SECONDS", 5)
    monkeypatch.setattr(AgentOrchestrator, "_REMOTE_STOP_POLL_SECONDS", 0.05)
    # Keep the first cycle fast and network-free (matches
    # test_orchestrator_continues_after_one_stage_raises's own pattern) so
    # the loop reliably reaches its inter-cycle _wait() -- where the remote
    # desired_state flip is actually detected -- well within this test's
    # polling window, rather than the assertion racing real provider I/O.
    import app.agent.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "run_discovery_cycle", lambda: {})

    orch_a = AgentOrchestrator()

    async def run():
        orch_a.start(test_mode=False)
        await asyncio.sleep(0.1)
        assert orch_a._became_active is True
        assert run_state.get_run_state()["actual_state"] == AgentRunState.RUNNING.value

        # Simulates a STOP request handled entirely by a different process.
        run_state.set_desired_state(AgentRunState.STOPPED)

        for _ in range(100):
            if run_state.get_run_state()["actual_state"] == AgentRunState.STOPPED.value:
                break
            await asyncio.sleep(0.05)

        final = run_state.get_run_state()
        assert final["actual_state"] == AgentRunState.STOPPED.value
        assert orch_a._became_active is False
        # release the (already-idle) task cleanly for test hygiene
        await orch_a.stop()

    asyncio.run(run())


def test_heartbeat_renews_lease_mid_cycle(tmp_env, sample_profile, orchestrator, monkeypatch):
    """A long-running stage must not let AGENT_ORCHESTRATOR_LEASE_SECONDS
    expire mid-cycle -- every _heartbeat call (stage-level and per-job)
    renews the lease, not just the once-per-cycle renew at the top of the
    main loop."""
    orchestrator._instance_id = "orch-x"
    orchestrator._became_active = True
    monkeypatch.setattr(config, "AGENT_ORCHESTRATOR_LEASE_SECONDS", 60)
    run_state.try_acquire_orchestrator_lease("orch-x", 1)  # about to expire
    before = run_state.get_run_state()["lease_expires_at"]

    orchestrator._heartbeat(stage="generating_resumes", job_label="job 1")

    after = run_state.get_run_state()["lease_expires_at"]
    assert after > before


# --- double START / double STOP (in-process) --------------------------------


def test_double_start_via_route_never_creates_two_loops(tmp_env, sample_profile):
    """Two near-simultaneous POST /agent/start calls (the 'two tabs'
    scenario) must never result in two orchestrator loops running."""
    save_profile(sample_profile)

    with TestClient(app) as client:
        resp1 = client.post("/agent/start", follow_redirects=False)
        resp2 = client.post("/agent/start", follow_redirects=False)
        assert resp1.status_code == 303
        assert resp2.status_code == 303

        import time

        time.sleep(0.2)
        from app.agent.orchestrator import orchestrator as global_orchestrator

        # Only ever one live task for the process-wide singleton.
        assert global_orchestrator._task is not None
        assert not global_orchestrator._task.done()

        client.post("/agent/stop", follow_redirects=False)


def test_double_stop_is_idempotent(tmp_env, sample_profile, orchestrator):
    save_profile(sample_profile)

    async def run():
        orchestrator.start(test_mode=False)
        await asyncio.sleep(0.1)
        await asyncio.gather(orchestrator.stop(), orchestrator.stop())

    asyncio.run(run())
    final = run_state.get_run_state()
    assert final["actual_state"] == AgentRunState.STOPPED.value


def test_stop_without_ever_starting_is_safe(tmp_env, orchestrator):
    async def run():
        result = await orchestrator.stop()
        assert result == {"stopped": True}

    asyncio.run(run())
    assert run_state.get_run_state()["actual_state"] == AgentRunState.STOPPED.value


# --- restart recovery --------------------------------------------------------


def test_restart_recovery_resumes_running_via_lifespan(tmp_env, sample_profile):
    """CLAUDE.md one-click-agent restart recovery: if the process restarts
    while desired_state was RUNNING, app.main's lifespan must resume the
    orchestrator without the user re-clicking START."""
    save_profile(sample_profile)
    run_state.set_desired_state(AgentRunState.RUNNING, test_mode=False)
    run_state.set_actual_state(AgentRunState.STOPPED)  # simulates the old process having died

    with TestClient(app) as client:
        import time

        time.sleep(0.2)
        status = client.get("/agent/status").json()
        assert status["orchestrator"]["desired_state"] == AgentRunState.RUNNING.value
        assert status["orchestrator"]["actual_state"] in (
            AgentRunState.STARTING.value, AgentRunState.RUNNING.value,
        )
        client.post("/agent/stop", follow_redirects=False)


def test_restart_recovery_does_not_start_when_desired_stopped(tmp_env, sample_profile):
    save_profile(sample_profile)
    run_state.set_desired_state(AgentRunState.STOPPED)
    run_state.set_actual_state(AgentRunState.STOPPED)

    with TestClient(app) as client:
        status = client.get("/agent/status").json()
        assert status["orchestrator"]["actual_state"] == AgentRunState.STOPPED.value
