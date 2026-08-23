"""CLAUDE.md Phase 14 sections 44-55, 64, 79: unified dashboard + safe API
endpoints."""

from fastapi.testclient import TestClient

from app.candidate.profile import save_profile
from app.main import app
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process


def _confirmed_remote_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer",
        company="Acme Corp",
        location="Remote (US)",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python "
            "using FastAPI and PostgreSQL. Fully remote. Visa sponsorship available. "
            "Required: Python, FastAPI, PostgreSQL, Docker."
        ),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_dashboard_shows_summary_cards(tmp_env, sample_profile):
    """CLAUDE.md one-click-agent section 23 redefines the dashboard's card
    row -- these labels supersede the older Phase 14 wording."""
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Jobs found" in resp.text
    assert "One-page resumes ready" in resp.text
    assert "Strong matches" in resp.text


def test_dashboard_pipeline_table_shows_jd_coverage_column(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "JD coverage" in resp.text
    assert job.company in resp.text


def test_resume_analyze_and_optimize_actions(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())

    resp = client.post(f"/jobs/{job.id}/resume/analyze", follow_redirects=False)
    assert resp.status_code == 303

    resp2 = client.get(f"/api/jobs/{job.id}/jd-analysis")
    assert resp2.status_code == 200
    assert resp2.json()["analyzed"] is True

    resp3 = client.post(f"/jobs/{job.id}/resume/optimize", follow_redirects=False)
    assert resp3.status_code == 303

    resp4 = client.get(f"/api/jobs/{job.id}/resume-quality")
    assert resp4.status_code == 200
    body = resp4.json()
    assert "required_skill_coverage" in body
    assert "98%" not in str(body)

    resp5 = client.get(f"/api/jobs/{job.id}/resume-evidence")
    assert resp5.status_code == 200
    assert "evidence_links" in resp5.json()


def test_job_detail_page_shows_resume_diagnostics(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    client.post(f"/jobs/{job.id}/resume/optimize")

    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    assert "JD coverage" in resp.text
    assert "Internal alignment" in resp.text
    assert "Generate/Regenerate Resume" in resp.text or "Regenerate Resume" in resp.text


def test_resume_download_after_generation(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    client.post(f"/jobs/{job.id}/resume/optimize")

    resp = client.get(f"/jobs/{job.id}/resume/download/docx")
    assert resp.status_code == 200
    resp2 = client.get(f"/jobs/{job.id}/resume/download/pdf")
    assert resp2.status_code == 200
    resp3 = client.get(f"/jobs/{job.id}/resume/download/txt")
    assert resp3.status_code == 200


def test_resume_optimizer_doctor_page(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/resume-optimizer/doctor")
    assert resp.status_code == 200


def test_api_pipeline_summary(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    ingest_and_process(_confirmed_remote_job())
    resp = client.get("/api/pipeline/summary")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("jobs_discovered", "full_time_eligible", "sponsor_confirmed", "resume_ready", "applied"):
        assert key in body


def test_api_resume_optimizer_metrics(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/api/resume-optimizer/metrics")
    assert resp.status_code == 200
    assert "resume_optimizations_total" in resp.json()


def test_dashboard_resume_status_filter(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    client.post(f"/jobs/{job.id}/resume/optimize")

    resp = client.get("/?resume_status=READY")
    assert resp.status_code == 200
    assert job.company in resp.text
