from fastapi.testclient import TestClient

from app.main import app
from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo


def test_fleet_page_loads(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/fleet")
        assert resp.status_code == 200
        assert "Fleet Operations" in resp.text


def test_acquisition_page_loads(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/acquisition")
        assert resp.status_code == 200
        assert "Registry Acquisition" in resp.text


def test_fleet_metrics_json_endpoint(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/fleet/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "snapshot" in data
        assert "discovery_latency" in data
        assert "stored_portals" in data["snapshot"]


def test_dashboard_links_to_fleet_and_acquisition(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert '/fleet' in resp.text
        assert '/acquisition' in resp.text


def test_fleet_dead_letter_requeue_route(tmp_env):
    from app.workers import dead_letter

    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    dead_letter.record_permanent_failure(
        portal_type="company_registry", portal_id=entry_id, provider="greenhouse",
        consecutive_permanent_failures=8, last_error="404", last_attempt_id="a1", threshold=8,
    )
    dl = dead_letter.list_dead_letters()[0]

    with TestClient(app) as client:
        resp = client.post(f"/fleet/dead-letter/{dl['id']}/requeue", follow_redirects=False)
        assert resp.status_code == 303

    entry = registry_repo.get_entry(entry_id)
    assert entry.enabled is True


def test_acquisition_resume_route_rejects_non_resumable_batch(tmp_env, tmp_path):
    from app.registry import acquisition

    # A COMPLETED batch cannot be resumed via the HTTP endpoint.
    batch_id = acquisition._create_batch(source_name="s", source_type="CSV", path=str(tmp_path / "x.csv"))
    acquisition._update_batch(batch_id, status="COMPLETED")

    with TestClient(app) as client:
        resp = client.post(f"/acquisition/batches/{batch_id}/resume")
        assert resp.status_code == 400


def test_acquisition_resume_route_unknown_batch_404s(tmp_env):
    with TestClient(app) as client:
        resp = client.post("/acquisition/batches/999999/resume")
        assert resp.status_code == 404
