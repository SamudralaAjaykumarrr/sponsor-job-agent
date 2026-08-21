from fastapi.testclient import TestClient

from app.main import app
from app.registry.models import CompanyRegistryEntry
from app.registry.repo import insert_entry, mark_poll_result


def test_providers_page_loads_and_lists_every_provider(tmp_env):
    client = TestClient(app)
    resp = client.get("/providers")
    assert resp.status_code == 200
    assert "greenhouse" in resp.text
    assert "FULL" in resp.text
    assert "UNSUPPORTED" in resp.text


def test_registry_page_loads_empty(tmp_env):
    client = TestClient(app)
    resp = client.get("/registry")
    assert resp.status_code == 200
    assert "No registry entries yet" in resp.text


def test_registry_page_shows_entries_and_health(tmp_env):
    entry_id = insert_entry(CompanyRegistryEntry(
        company_name="Acme Corp", provider="greenhouse", tenant_identifier="acme",
    ))
    mark_poll_result(entry_id, success=True, jobs_new=2)

    client = TestClient(app)
    resp = client.get("/registry")
    assert resp.status_code == 200
    assert "Acme Corp" in resp.text
    assert "HEALTHY" in resp.text


def test_registry_add_endpoint_creates_entry(tmp_env):
    client = TestClient(app)
    resp = client.post("/registry/add", data={
        "company_name": "New Co", "provider": "lever", "tenant_identifier": "newco",
    }, follow_redirects=False)
    assert resp.status_code == 303

    resp2 = client.get("/registry")
    assert "New Co" in resp2.text


def test_registry_page_filters_by_provider(tmp_env):
    insert_entry(CompanyRegistryEntry(company_name="GH Co", provider="greenhouse", tenant_identifier="ghco"))
    insert_entry(CompanyRegistryEntry(company_name="Lever Co", provider="lever", tenant_identifier="leverco"))

    client = TestClient(app)
    resp = client.get("/registry?provider=greenhouse")
    assert "GH Co" in resp.text
    assert "Lever Co" not in resp.text


def test_discovery_log_endpoint_returns_json(tmp_env):
    client = TestClient(app)
    resp = client.get("/discovery-log")
    assert resp.status_code == 200
    assert resp.json() == []


def test_dashboard_links_to_providers_and_registry(tmp_env):
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert '/providers' in resp.text
    assert '/registry' in resp.text
