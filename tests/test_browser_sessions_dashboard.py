"""CLAUDE.md Phase 10 sections 47-49: the browser-assist session dashboard
pages and actions."""

import json

from fastapi.testclient import TestClient

from app import config
from app.agent import state as agent_state
from app.applications import browser_runtime
from app.applications import repo as executions_repo
from app.candidate.profile import save_profile
from app.jobs_repo import get_job, insert_job, update_job
from app.main import app
from app.models import ApplicationState, Job, SponsorshipStatus


def _job_with_execution(tmp_env) -> tuple[Job, str]:
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", description="Full-time. H-1B sponsorship available.",
        employment_type="full_time", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        technical_match_score=80.0, application_state=ApplicationState.READY_TO_APPLY,
        provider="never_configured", canonical_url="https://x/1", url="https://x/1",
    )
    job_id = insert_job(job)
    job_dir = tmp_env["output_dir"] / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "resume.pdf").write_bytes(b"%PDF fake")
    (job_dir / "resume.docx").write_bytes(b"fake")
    (job_dir / "application_answers.json").write_text(json.dumps({
        "full_name": "Test Candidate", "email": "t@example.com", "phone": "555", "do_you_require_sponsorship": "No",
    }))
    update_job(job_id, resume_pdf_path=str(job_dir / "resume.pdf"), resume_docx_path=str(job_dir / "resume.docx"),
               application_answers_path=str(job_dir / "application_answers.json"))
    execution_id = executions_repo.create_execution(job_id, provider="never_configured", mode="ASSIST")
    return get_job(job_id), execution_id


def test_browser_sessions_page_loads_when_disabled(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    client = TestClient(app)
    resp = client.get("/applications/browser-sessions")
    assert resp.status_code == 200
    assert "Browser assist" in resp.text
    assert "OFF" in resp.text


def test_start_session_via_dashboard_and_view_detail_page(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    job, execution_id = _job_with_execution(tmp_env)

    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: browser_runtime.DiscoveryOutcome(
        pause_reason=None, current_url="https://x/1",
        fields=[{"index": 0, "label": "Full Name", "name": "full_name", "type": "text", "required": True, "choices": []}],
        fingerprint="fp1", submit_button={"text": "Submit"},
    ))
    monkeypatch.setattr(browser_runtime, "fill_fields",
                         lambda *a, **k: browser_runtime.FillOutcome(filled=["Full Name"]))

    client = TestClient(app)
    resp = client.post(f"/jobs/{job.id}/browser-assist/start", follow_redirects=False)
    assert resp.status_code == 303
    detail_url = resp.headers["location"]
    assert detail_url.startswith("/applications/browser-sessions/")

    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert "READY_FOR_FINAL_SUBMIT" in detail.text

    listing = client.get("/applications/browser-sessions")
    assert listing.status_code == 200
    assert "READY_FOR_FINAL_SUBMIT" in listing.text

    job_page = client.get(f"/jobs/{job.id}")
    assert job_page.status_code == 200
    assert "browser-assist session" in job_page.text.lower()


def test_start_session_via_dashboard_rejects_ineligible_job(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)

    job = Job(
        title="Backend Software Engineer", company="Acme Corp", description="Contract role.",
        employment_type="contract", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        application_state=ApplicationState.READY_TO_APPLY,
    )
    job_id = insert_job(job)
    executions_repo.create_execution(job_id, provider="never_configured", mode="ASSIST")

    client = TestClient(app)
    resp = client.post(f"/jobs/{job_id}/browser-assist/start")
    assert resp.status_code == 400


def test_start_session_requires_an_active_execution(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)

    job_id = insert_job(Job(title="X", company="Acme", description="Full-time."))
    client = TestClient(app)
    resp = client.post(f"/jobs/{job_id}/browser-assist/start")
    assert resp.status_code == 400


def test_close_session_route(tmp_env, sample_profile, monkeypatch):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    job, execution_id = _job_with_execution(tmp_env)

    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: browser_runtime.DiscoveryOutcome(
        pause_reason=None, current_url="https://x/1", fields=[], fingerprint="fp1", submit_button={"text": "Submit"},
    ))
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: browser_runtime.FillOutcome())

    client = TestClient(app)
    start_resp = client.post(f"/jobs/{job.id}/browser-assist/start", follow_redirects=False)
    session_id = start_resp.headers["location"].rsplit("/", 1)[-1]

    close_resp = client.post(f"/browser-sessions/{session_id}/close", follow_redirects=False)
    assert close_resp.status_code == 303
    detail = client.get(f"/applications/browser-sessions/{session_id}")
    assert "CLOSED" in detail.text


def test_unknown_session_detail_returns_404(tmp_env, sample_profile):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/applications/browser-sessions/does-not-exist")
    assert resp.status_code == 404
