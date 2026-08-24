"""CLAUDE.md Phase 8 sections 42-43: /applications dashboard page and job
detail action routes."""

import json

from fastapi.testclient import TestClient

from app import config
from app.agent import state as agent_state
from app.candidate.profile import save_profile
from app.main import app
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


def _mock_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id="dash-1", provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_applications_page_loads_and_shows_executor_state(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", False)
    client = TestClient(app)
    resp = client.get("/applications")
    assert resp.status_code == 200
    assert "Application executor" in resp.text
    assert "OFF" in resp.text


def test_applications_doctor_page_loads(tmp_env, sample_profile):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/applications/doctor")
    assert resp.status_code == 200


def test_executor_disabled_returns_400_on_queue_action(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", False)
    job = ingest_and_process(_mock_job())
    client = TestClient(app)
    resp = client.post(f"/jobs/{job.id}/applications/queue", data={"mode": "ASSIST"})
    assert resp.status_code == 400


def test_prepare_application_via_dashboard_reaches_applied(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    job = ingest_and_process(_mock_job())
    client = TestClient(app)

    resp = client.post(
        f"/jobs/{job.id}/applications/prepare", data={"mode": "AUTO_PERMITTED"}, follow_redirects=False,
    )
    assert resp.status_code == 303

    job_detail = client.get(f"/jobs/{job.id}")
    assert job_detail.status_code == 200
    assert "APPLIED" in job_detail.text

    api_resp = client.get(f"/api/jobs/{job.id}/eligibility")
    assert api_resp.status_code == 200

    metrics_resp = client.get("/api/applications/metrics")
    assert metrics_resp.status_code == 200
    assert metrics_resp.json()["applications_confirmed"] >= 1


def test_test_fixture_execution_never_shows_on_applications_page(tmp_env, sample_profile, monkeypatch):
    """Real bug this integration QA pass caught live: a TEST MODE mock_ats
    execution (app.agent.orchestrator's _seed_test_fixture_if_needed, or any
    job with is_test_fixture=True) showed up as a real row on the primary
    /applications page and inflated its tab counters -- app.pipeline_dashboard
    already filters is_test_fixture = 0 everywhere, but
    app.applications.repo.list_executions_with_jobs()/bucket_counts() never
    did. CLAUDE.md's 'no fake Acme/test rows in normal real-mode product
    views' rule applies to the Applications page exactly like the Dashboard."""
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    job = ingest_and_process(_mock_job(external_job_id="dash-fixture-1", is_test_fixture=True))
    client = TestClient(app)

    resp = client.post(
        f"/jobs/{job.id}/applications/prepare", data={"mode": "AUTO_PERMITTED"}, follow_redirects=False,
    )
    assert resp.status_code == 303

    apps_page = client.get("/applications")
    assert apps_page.status_code == 200
    assert "Acme Corp" not in apps_page.text

    from app.applications import repo as applications_repo

    rows = applications_repo.list_executions_with_jobs()
    assert all(r["job_id"] != job.id for r in rows)
    counts = applications_repo.bucket_counts()
    assert sum(counts.values()) == 0
