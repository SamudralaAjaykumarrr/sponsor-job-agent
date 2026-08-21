import json

from fastapi.testclient import TestClient

from app.candidate.profile import save_profile
from app.main import app


def test_health(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_dashboard_loads(tmp_env):
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sponsor Job Agent" in resp.text


def test_manual_ingestion_via_form(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.post(
        "/jobs/ingest",
        data={
            "title": "Backend Software Engineer",
            "company": "Acme Corp",
            "location": "Remote (US)",
            "description": (
                "We are hiring a Backend Software Engineer to build REST APIs in Python "
                "using FastAPI. This is a fully remote position. Visa sponsorship available."
            ),
            "url": "",
            "published_at": "",
            "mode": "ASSIST",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    detail = client.get(location)
    assert detail.status_code == 200
    assert "READY_TO_APPLY" in detail.text or "resume.docx" in detail.text


def test_candidate_status_endpoint_reports_missing_fields(tmp_env):
    client = TestClient(app)
    resp = client.get("/candidate/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_count"] > 0
    assert "contact.full_name" in body["missing_fields"]
