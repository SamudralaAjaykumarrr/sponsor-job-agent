"""CLAUDE.md Phase 7 sections 28-31, 51: dashboard/API verification for the
sponsorship intelligence layer."""

from fastapi.testclient import TestClient

from app.candidate.profile import save_profile
from app.main import app
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process
from app.registry.models import Company
from app.registry import store


def test_companies_page_loads(tmp_env):
    client = TestClient(app)
    resp = client.get("/companies")
    assert resp.status_code == 200
    assert "Sponsorship companies" in resp.text


def test_company_detail_page_loads_and_shows_disclaimer(tmp_env):
    cid = store.insert_company(Company(normalized_name="dashco", display_name="DashCo", primary_domain="dashco.com"))
    client = TestClient(app)
    resp = client.get(f"/companies/{cid}")
    assert resp.status_code == 200
    assert "HISTORICAL EVIDENCE" in resp.text
    assert "DashCo" in resp.text


def test_company_detail_404_for_missing(tmp_env):
    client = TestClient(app)
    resp = client.get("/companies/99999")
    assert resp.status_code == 404


def test_review_queue_page_loads(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote",
        description="We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI and PostgreSQL.",
        mode=ApplicationMode.ASSIST,
    )
    ingest_and_process(job)
    client = TestClient(app)
    resp = client.get("/sponsorship/review-queue")
    assert resp.status_code == 200
    assert "Acme Corp" in resp.text


def test_sponsorship_doctor_page_loads(tmp_env):
    client = TestClient(app)
    resp = client.get("/sponsorship/doctor")
    assert resp.status_code == 200
    assert "Sponsorship Doctor" in resp.text


def test_identity_review_page_loads_and_resolve_action(tmp_env):
    store.insert_company(Company(normalized_name="ambig", display_name="Ambig", primary_domain="a1.com"))
    store.insert_company(Company(normalized_name="ambig", display_name="Ambig", primary_domain="a2.com"))
    from app.sponsorship.identity import resolve_company

    resolve_company("Ambig")
    client = TestClient(app)
    resp = client.get("/sponsorship/identity-review")
    assert resp.status_code == 200
    assert "Ambig" in resp.text

    from app.sponsorship.identity import list_pending_reviews

    review_id = list_pending_reviews()[0]["id"]
    resp2 = client.post(f"/sponsorship/identity-review/{review_id}/resolve", data={"company_id": ""}, follow_redirects=False)
    assert resp2.status_code == 303
    assert len(list_pending_reviews()) == 0


def test_job_detail_shows_sponsorship_decision_panel(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = Job(
        title="Backend Software Engineer", company="PanelCo", location="Remote",
        description="We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI and PostgreSQL. We sponsor H-1B visas.",
        mode=ApplicationMode.ASSIST,
    )
    result = ingest_and_process(job)
    client = TestClient(app)
    resp = client.get(f"/jobs/{result.id}")
    assert resp.status_code == 200
    assert "Sponsorship decision explanation" in resp.text
    assert "CONFIRMED_SPONSOR" in resp.text


def test_dashboard_historical_strength_filter_query_param(tmp_env):
    client = TestClient(app)
    resp = client.get("/?historical_strength=STRONG_RECENT")
    assert resp.status_code == 200


def test_api_company_sponsorship_endpoint(tmp_env):
    cid = store.insert_company(Company(normalized_name="apico", display_name="ApiCo", primary_domain="apico.com"))
    client = TestClient(app)
    resp = client.get(f"/api/companies/{cid}/sponsorship")
    assert resp.status_code == 200
    data = resp.json()
    assert data["historical_strength"] == "NONE"
    assert "NOT A GUARANTEE" in data["label"]


def test_api_job_sponsorship_endpoint(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = Job(
        title="Backend Software Engineer", company="ApiJobCo", location="Remote",
        description="We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI and PostgreSQL.",
        mode=ApplicationMode.ASSIST,
    )
    result = ingest_and_process(job)
    client = TestClient(app)
    resp = client.get(f"/api/jobs/{result.id}/sponsorship")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_status"] == "UNKNOWN"
    assert len(data["decision_history"]) == 1


def test_api_review_queue_endpoint(tmp_env):
    client = TestClient(app)
    resp = client.get("/api/sponsorship/review-queue")
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_datasets_and_stats_endpoints(tmp_env):
    client = TestClient(app)
    resp = client.get("/api/sponsorship/datasets")
    assert resp.status_code == 200
    resp2 = client.get("/api/sponsorship/stats")
    assert resp2.status_code == 200
    assert "sponsorship_evidence_records" in resp2.json()


def test_metrics_endpoint_includes_sponsorship_metrics(tmp_env):
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "sponsor_job_agent_sponsorship_evidence_records" in resp.text
