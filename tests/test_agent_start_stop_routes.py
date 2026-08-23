"""/agent/start, /agent/stop routes and the redesigned dashboard's
START/STOP AGENT control, Needs Your Action queue, and Live Activity feed
(CLAUDE.md one-click-agent sections 1, 20, 23-24, 41)."""

import time

from fastapi.testclient import TestClient

from app.agent import run_state
from app.agent.run_state import AgentRunState
from app.candidate.profile import save_profile
from app.main import app
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process


def test_get_never_starts_agent(tmp_env):
    """CLAUDE.md section 41: no GET request may start the agent."""
    client = TestClient(app)
    # There is deliberately no GET /agent/start route at all.
    resp = client.get("/agent/start")
    assert resp.status_code == 405


def test_start_and_stop_via_dashboard_routes(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)

    resp = client.post("/agent/start", follow_redirects=False)
    assert resp.status_code == 303
    time.sleep(0.1)

    status = client.get("/agent/status").json()
    assert status["orchestrator"]["desired_state"] == AgentRunState.RUNNING.value

    resp2 = client.post("/agent/stop", follow_redirects=False)
    assert resp2.status_code == 303

    status2 = client.get("/agent/status").json()
    assert status2["orchestrator"]["desired_state"] == AgentRunState.STOPPED.value
    assert status2["orchestrator"]["actual_state"] == AgentRunState.STOPPED.value


def test_dashboard_shows_start_agent_button_when_stopped(tmp_env, sample_profile):
    save_profile(sample_profile)
    run_state.set_desired_state(AgentRunState.STOPPED)
    run_state.set_actual_state(AgentRunState.STOPPED)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "START AGENT" in resp.text
    assert "START AGENT (TEST MODE)" in resp.text
    assert "Agent Status" in resp.text


def test_dashboard_shows_stop_agent_button_when_running(tmp_env, sample_profile):
    save_profile(sample_profile)
    run_state.set_actual_state(AgentRunState.RUNNING)
    try:
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "STOP AGENT" in resp.text
    finally:
        run_state.set_actual_state(AgentRunState.STOPPED)


def _likely_sponsor_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer",
        company="Acme Corp",
        location="Remote (US)",
        description="Join our backend Python team building APIs.",
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_needs_action_queue_shows_review_required_job(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_likely_sponsor_job())
    assert job.application_state.value == "REVIEW_REQUIRED"

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Needs Your Action" in resp.text
    assert job.company in resp.text


def test_live_activity_feed_present_after_ingest(tmp_env, sample_profile):
    save_profile(sample_profile)
    ingest_and_process(_likely_sponsor_job())

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Live Activity" in resp.text
