import json

from app.registry import store
from app.registry.analytics import provider_breakdown, snapshot
from app.registry.export import export_json, export_jsonl
from app.registry.models import CareerPortal, Company, PortalStatus, RegistryProvenance


def _company(**overrides) -> Company:
    defaults = dict(normalized_name="acme", display_name="Acme Corp", primary_domain="acme.com")
    defaults.update(overrides)
    return Company(**defaults)


def _setup_mixed_registry():
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="a",
                                      verification_status=PortalStatus.ACTIVE, current_job_count=5))
    store.insert_portal(CareerPortal(company_id=cid, provider="lever", tenant_identifier="b",
                                      verification_status=PortalStatus.CANDIDATE))
    store.insert_portal(CareerPortal(company_id=cid, provider="lever", tenant_identifier="c",
                                      verification_status=PortalStatus.QUARANTINED))
    return cid


def test_snapshot_counts_are_real_db_derived(tmp_env):
    _setup_mixed_registry()
    snap = snapshot()
    assert snap["companies"] == 1
    assert snap["portals"] == 3
    assert snap["active"] == 1
    assert snap["candidate"] == 1
    assert snap["quarantined"] == 1


def test_snapshot_empty_registry_all_zero(tmp_env):
    snap = snapshot()
    assert all(v == 0 for v in snap.values())


def test_provider_breakdown_groups_by_provider(tmp_env):
    _setup_mixed_registry()
    rows = {r["provider"]: r for r in provider_breakdown()}
    assert rows["greenhouse"]["active_portals"] == 1
    assert rows["lever"]["total_portals"] == 2
    assert rows["greenhouse"]["jobs_seen"] == 5


def test_export_jsonl_streams_all_portals_with_provenance(tmp_env, tmp_path):
    cid = _setup_mixed_registry()
    portal = store.list_portals()[0]
    store.upsert_provenance(RegistryProvenance(portal_id=portal.id, company_id=cid, source_type="manual_seed", source_name="seed"))

    out = tmp_path / "export.jsonl"
    n = export_jsonl(out)
    assert n == 3
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 3
    records = [json.loads(l) for l in lines]
    assert any(r["provenance"] for r in records)
    # No candidate data of any kind should ever appear in a registry export.
    for r in records:
        assert "resume" not in json.dumps(r).lower()
        assert "ssn" not in json.dumps(r).lower()


def test_export_json_writes_array(tmp_env, tmp_path):
    _setup_mixed_registry()
    out = tmp_path / "export.json"
    n = export_json(out)
    assert n == 3
    data = json.loads(out.read_text())
    assert isinstance(data, list)
    assert len(data) == 3
