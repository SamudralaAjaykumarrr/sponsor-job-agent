from fastapi.testclient import TestClient

from app.agent import state as agent_state
from app.candidate.profile import save_profile
from app.main import app
from app.models import ApplicationState
from app.pipeline import ingest_and_process
from app.models import ApplicationMode, Job


def _confirmed_remote_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer",
        company="Acme Corp",
        location="Remote (US)",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python "
            "using FastAPI and PostgreSQL. Fully remote. Visa sponsorship available."
        ),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_agent_status_endpoint_reports_disabled_by_default(tmp_env):
    agent_state.set_enabled(False)
    client = TestClient(app)
    resp = client.get("/agent/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert "config" in body
    assert "recent_cycles" in body


def test_agent_toggle_endpoint_flips_state(tmp_env):
    agent_state.set_enabled(False)
    client = TestClient(app)
    try:
        resp = client.post("/agent/toggle", data={"enabled": "true"}, follow_redirects=False)
        assert resp.status_code == 303
        assert agent_state.get_status()["enabled"] is True

        resp2 = client.post("/agent/toggle", data={"enabled": "false"}, follow_redirects=False)
        assert resp2.status_code == 303
        assert agent_state.get_status()["enabled"] is False
    finally:
        agent_state.set_enabled(False)


def test_dashboard_shows_agent_status_bar(tmp_env):
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Autonomous agent" in resp.text


def test_dashboard_review_required_filter(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _confirmed_remote_job(company="Acme Corp", description="Join our backend Python team building APIs.")
    result = ingest_and_process(job)
    assert result.application_state == ApplicationState.REVIEW_REQUIRED

    client = TestClient(app)
    resp = client.get("/?application_state=REVIEW_REQUIRED")
    assert resp.status_code == 200
    assert "REVIEW_REQUIRED" in resp.text


def test_job_detail_shows_score_breakdown_and_history(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _confirmed_remote_job()
    result = ingest_and_process(job)
    assert result.application_state == ApplicationState.READY_TO_APPLY

    client = TestClient(app)
    resp = client.get(f"/jobs/{result.id}")
    assert resp.status_code == 200
    assert "Score breakdown" in resp.text
    assert "Pipeline history" in resp.text
    assert "Regenerate Resume" in resp.text


def test_regenerate_resume_endpoint(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _confirmed_remote_job()
    result = ingest_and_process(job)

    client = TestClient(app)
    resp = client.post(f"/jobs/{result.id}/regenerate", follow_redirects=False)
    assert resp.status_code == 303


def test_manual_transition_records_history(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _confirmed_remote_job()
    result = ingest_and_process(job)
    assert result.application_state == ApplicationState.READY_TO_APPLY

    client = TestClient(app)
    resp = client.post(f"/jobs/{result.id}/state", data={"target_state": "APPLIED"}, follow_redirects=False)
    assert resp.status_code == 303

    from app.jobs_repo import get_state_history
    history = get_state_history(result.id)
    assert any(h["to_state"] == "APPLIED" and h["actor"] == "user" for h in history)
