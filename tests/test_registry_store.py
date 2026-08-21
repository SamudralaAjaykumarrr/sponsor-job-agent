from app.registry import store
from app.registry.models import CareerPortal, Company, PortalStatus, RegistryProvenance


def _company(**overrides) -> Company:
    defaults = dict(normalized_name="acme", display_name="Acme Corp", primary_domain="acme.com")
    defaults.update(overrides)
    return Company(**defaults)


def _portal(company_id: int, **overrides) -> CareerPortal:
    defaults = dict(company_id=company_id, provider="greenhouse", tenant_identifier="acme")
    defaults.update(overrides)
    return CareerPortal(**defaults)


def test_insert_and_get_company(tmp_env):
    cid = store.insert_company(_company())
    company = store.get_company(cid)
    assert company.display_name == "Acme Corp"
    assert company.primary_domain == "acme.com"
    assert company.enabled is True


def test_company_identity_lookup_requires_name_and_domain(tmp_env):
    store.insert_company(_company())
    assert store.get_company_by_identity("acme", "acme.com") is not None
    assert store.get_company_by_identity("acme", "different.com") is None


def test_two_companies_same_name_different_domain_stay_distinct(tmp_env):
    id1 = store.insert_company(_company(primary_domain="acme.com"))
    id2 = store.insert_company(_company(primary_domain="acme.io"))
    assert id1 != id2
    assert store.count_companies() == 2


def test_insert_and_get_portal(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    portal = store.get_portal(pid)
    assert portal.provider == "greenhouse"
    assert portal.tenant_identifier == "acme"
    assert portal.verification_status == PortalStatus.DISCOVERED


def test_get_portal_by_provider_tenant(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(_portal(cid))
    found = store.get_portal_by_provider_tenant("greenhouse", "acme")
    assert found is not None
    assert store.get_portal_by_provider_tenant("greenhouse", "nonexistent") is None


def test_get_portal_by_canonical_url(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(_portal(cid, canonical_url="https://boards.greenhouse.io/acme"))
    found = store.get_portal_by_canonical_url("https://boards.greenhouse.io/acme")
    assert found is not None


def test_update_portal_is_additive(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    store.update_portal(pid, notes="checked", confidence=42)
    portal = store.get_portal(pid)
    assert portal.notes == "checked"
    assert portal.confidence == 42
    assert portal.provider == "greenhouse"  # untouched


def test_list_portals_filters_and_bounds(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(_portal(cid, provider="greenhouse", tenant_identifier="a"))
    store.insert_portal(_portal(cid, provider="lever", tenant_identifier="b"))
    gh = store.list_portals(provider="greenhouse")
    assert len(gh) == 1
    assert gh[0].provider == "greenhouse"

    limited = store.list_portals(limit=1)
    assert len(limited) == 1


def test_list_portals_for_company(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(_portal(cid, provider="greenhouse", tenant_identifier="a"))
    store.insert_portal(_portal(cid, provider="lever", tenant_identifier="b"))
    portals = store.list_portals_for_company(cid)
    assert len(portals) == 2


def test_provenance_upsert_idempotent_on_portal_source(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    store.upsert_provenance(RegistryProvenance(portal_id=pid, company_id=cid, source_type="bulk_import",
                                                source_name="file.csv", evidence="row 1", confidence=40))
    store.upsert_provenance(RegistryProvenance(portal_id=pid, company_id=cid, source_type="bulk_import",
                                                source_name="file.csv", evidence="row 1 (re-import)", confidence=40))
    provenance = store.list_provenance_for_portal(pid)
    assert len(provenance) == 1
    assert provenance[0].evidence == "row 1 (re-import)"


def test_provenance_from_different_sources_both_retained(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    store.upsert_provenance(RegistryProvenance(portal_id=pid, company_id=cid, source_type="bulk_import", source_name="a.csv"))
    store.upsert_provenance(RegistryProvenance(portal_id=pid, company_id=cid, source_type="page_discovery", source_name="acme.com"))
    assert len(store.list_provenance_for_portal(pid)) == 2


def test_has_provenance(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    assert store.has_provenance(pid) is False
    store.upsert_provenance(RegistryProvenance(portal_id=pid, company_id=cid, source_type="manual_seed", source_name="s"))
    assert store.has_provenance(pid) is True


def test_bulk_insert_portals_raw_for_benchmark(tmp_env):
    cid = store.insert_company(_company())
    from app.registry.store import utcnow

    now = utcnow()
    rows = [dict(
        company_id=cid, provider="greenhouse", tenant_identifier=f"synthetic-{i}", careers_url="", jobs_url="",
        canonical_url=f"https://boards.greenhouse.io/synthetic-{i}", support_level="FULL",
        discovery_status="IMPORTED", verification_status="DISCOVERED", identity_status="UNKNOWN",
        enabled=1, confidence=0, confidence_reasons="[]", last_verified_at=None, last_polled_at=None,
        next_poll_at=None, last_success_at=None, last_failure_at=None, consecutive_failures=0,
        consecutive_permanent_failures=0, average_job_yield=0.0, average_latency_ms=0.0, current_job_count=0,
        poll_interval_minutes=15, registry_entry_id=None, superseded_by_portal_id=None, notes="",
        created_at=now, updated_at=now,
    ) for i in range(50)]
    n = store.bulk_insert_portals_raw(rows)
    assert n == 50
    assert store.count_portals() == 50
