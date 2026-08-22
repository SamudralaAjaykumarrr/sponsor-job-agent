"""CLAUDE.md Phase 12 sections 66, 68: workday-tenants dashboard page must
show per-tenant stability, never a collapsed claim."""

from fastapi.testclient import TestClient

from app.agent import state as agent_state
from app.applications.workday_tenant import record_attempt
from app.main import app


def test_workday_tenants_page_shows_stability(tmp_env, sample_profile):
    agent_state.set_enabled(False)
    from app.candidate.profile import save_profile

    save_profile(sample_profile)
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="LOGIN_TRIGGER")

    client = TestClient(app)
    resp = client.get("/applications/workday-tenants")
    assert resp.status_code == 200
    assert "VARIABLE" in resp.text
    assert "acme" in resp.text


def test_workday_tenants_page_empty_state_honest(tmp_env, sample_profile):
    agent_state.set_enabled(False)
    from app.candidate.profile import save_profile

    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/applications/workday-tenants")
    assert resp.status_code == 200
    assert "No repeated attempts recorded yet" in resp.text
