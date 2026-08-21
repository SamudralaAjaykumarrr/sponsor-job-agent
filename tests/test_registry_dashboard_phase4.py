from fastapi.testclient import TestClient

from app.main import app
from app.registry import store
from app.registry.models import CareerPortal, Company, PortalStatus, RegistryProvenance


def _company(**overrides) -> Company:
    defaults = dict(normalized_name="acme", display_name="Acme Corp", primary_domain="acme.com")
    defaults.update(overrides)
    return Company(**defaults)


def test_registry_page_shows_phase4_summary_cards(tmp_env):
    client = TestClient(app)
    resp = client.get("/registry")
    assert resp.status_code == 200
    assert "Companies" in resp.text
    assert "Acquisition / Verification Registry" in resp.text


def test_registry_page_lists_portals(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                      support_level="FULL", verification_status=PortalStatus.CANDIDATE))
    client = TestClient(app)
    resp = client.get("/registry")
    assert "Acme Corp" in resp.text
    assert "CANDIDATE" in resp.text


def test_registry_page_filters_by_portal_status(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="a",
                                      verification_status=PortalStatus.ACTIVE))
    store.insert_portal(CareerPortal(company_id=cid, provider="lever", tenant_identifier="b",
                                      verification_status=PortalStatus.QUARANTINED))
    client = TestClient(app)
    resp = client.get("/registry?portal_status=ACTIVE")
    assert resp.status_code == 200


def test_portal_detail_page_loads(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                            careers_url="https://boards.greenhouse.io/acme"))
    store.upsert_provenance(RegistryProvenance(portal_id=pid, company_id=cid, source_type="manual_seed", source_name="seed"))

    client = TestClient(app)
    resp = client.get(f"/registry/portals/{pid}")
    assert resp.status_code == 200
    assert "Acme Corp" in resp.text
    assert "manual_seed" in resp.text


def test_portal_detail_404_for_missing_portal(tmp_env):
    client = TestClient(app)
    resp = client.get("/registry/portals/999999")
    assert resp.status_code == 404


def test_portal_enable_disable_actions(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme"))
    client = TestClient(app)

    resp = client.post(f"/registry/portals/{pid}/disable", follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_portal(pid).enabled is False

    resp = client.post(f"/registry/portals/{pid}/enable", follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_portal(pid).enabled is True


def test_portal_quarantine_action(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme"))
    client = TestClient(app)
    resp = client.post(f"/registry/portals/{pid}/quarantine", follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_portal(pid).verification_status == PortalStatus.QUARANTINED


def test_registry_doctor_page_loads(tmp_env):
    client = TestClient(app)
    resp = client.get("/registry/doctor")
    assert resp.status_code == 200
    assert "Registry Doctor" in resp.text


def test_portal_action_routes_are_post_only_not_get(tmp_env):
    """Actions that mutate state must be safe POST routes, never a bare GET link."""
    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme"))
    client = TestClient(app)
    resp = client.get(f"/registry/portals/{pid}/disable")
    assert resp.status_code == 405
