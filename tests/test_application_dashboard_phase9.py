"""CLAUDE.md Phase 9 sections 45-47: dashboard/fleet-page routes."""

import json

from fastapi.testclient import TestClient

from app import config
from app.agent import state as agent_state
from app.applications.executor import process_execution, queue_application
from app.candidate.profile import save_profile
from app.main import app
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


def test_application_workers_page_loads_when_empty(tmp_env, sample_profile):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/application-workers")
    assert resp.status_code == 200
    assert "Application Worker Fleet" in resp.text


def test_capability_matrix_page_loads(tmp_env, sample_profile):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/applications/capability-matrix")
    assert resp.status_code == 200
    assert "mock_ats" in resp.text
    assert "greenhouse" in resp.text


def test_applications_page_shows_budget_and_fleet_sections(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    client = TestClient(app)
    resp = client.get("/applications")
    assert resp.status_code == 200
    assert "submitted today" in resp.text
    assert "workers online" in resp.text


def test_drain_and_resume_drain_worker_admin_actions(tmp_env, sample_profile, monkeypatch):
    from app.workers import repo as workers_repo
    from app.workers.models import WorkerStatus

    agent_state.set_enabled(False)
    save_profile(sample_profile)
    workers_repo.upsert_worker("app-worker-x", hostname="h", pid=1, shard_index=0, shard_count=1,
                                status=WorkerStatus.IDLE.value)
    client = TestClient(app)

    resp = client.post("/application-workers/app-worker-x/drain", follow_redirects=False)
    assert resp.status_code == 303
    assert workers_repo.get_worker("app-worker-x")["status"] == "DRAINING"

    resp2 = client.post("/application-workers/app-worker-x/resume-drain", follow_redirects=False)
    assert resp2.status_code == 303
    assert workers_repo.get_worker("app-worker-x")["status"] == "IDLE"


def test_scheduler_and_reconcile_worker_manual_trigger_endpoints(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_AUTO_PREPARE_ENABLED", True)
    ingest_and_process(_mock_job("dash9-1"))
    client = TestClient(app)

    resp = client.post("/applications/scheduler/run")
    assert resp.status_code == 200
    assert resp.json()["queued"] == 1

    resp2 = client.post("/applications/reconcile-worker/run")
    assert resp2.status_code == 200


def test_circuit_admin_actions_on_dashboard(tmp_env, sample_profile, monkeypatch):
    from app.applications import circuit as app_circuit

    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD", 1)
    app_circuit.record_result("mock_ats", success=False)
    assert app_circuit.get_status("mock_ats").state == "OPEN"

    client = TestClient(app)
    resp = client.post("/applications/circuit/mock_ats/close", follow_redirects=False)
    assert resp.status_code == 303
    assert app_circuit.get_status("mock_ats").state == "CLOSED"
