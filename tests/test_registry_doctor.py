from app.registry import store
from app.registry.doctor import run_doctor
from app.registry.models import CareerPortal, Company, PortalStatus


def _company(**overrides) -> Company:
    defaults = dict(normalized_name="acme", display_name="Acme Corp", primary_domain="acme.com")
    defaults.update(overrides)
    return Company(**defaults)


def test_doctor_clean_registry_has_no_issues(tmp_env):
    report = run_doctor()
    assert report.serious_count == 0
    assert report.warning_count == 0


def test_doctor_flags_active_missing_tenant(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="",
                                      verification_status=PortalStatus.ACTIVE))
    report = run_doctor()
    assert any(i.check == "active_missing_tenant" for i in report.issues)
    assert report.serious_count >= 1


def test_doctor_flags_verified_without_provenance(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                      verification_status=PortalStatus.VERIFIED))
    report = run_doctor()
    assert any(i.check == "verified_without_provenance" for i in report.issues)


def test_doctor_flags_unsupported_marked_active(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="icims", tenant_identifier="acme",
                                      support_level="UNSUPPORTED", verification_status=PortalStatus.ACTIVE))
    report = run_doctor()
    assert any(i.check == "unsupported_marked_active" for i in report.issues)


def test_doctor_flags_invalid_careers_url(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                      careers_url="not-a-url"))
    report = run_doctor()
    assert any(i.check == "invalid_careers_url" for i in report.issues)


def test_doctor_flags_contradictory_domain_mapping(tmp_env):
    store.insert_company(_company(normalized_name="acme", primary_domain="shared.com"))
    store.insert_company(_company(normalized_name="beta", primary_domain="shared.com"))
    report = run_doctor()
    assert any(i.check == "contradictory_domain_mapping" for i in report.issues)


def test_doctor_flags_impossible_scheduler_state(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                            verification_status=PortalStatus.STALE))
    store.update_portal(pid, next_poll_at="2026-08-22T00:00:00Z")
    report = run_doctor()
    assert any(i.check == "impossible_scheduler_state" for i in report.issues)


def test_doctor_healthy_active_portal_with_provenance_has_no_issues(tmp_env):
    from app.registry.models import RegistryProvenance

    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                            support_level="FULL", verification_status=PortalStatus.ACTIVE,
                                            careers_url="https://boards.greenhouse.io/acme",
                                            canonical_url="https://boards.greenhouse.io/acme"))
    store.upsert_provenance(RegistryProvenance(portal_id=pid, company_id=cid, source_type="manual_seed", source_name="seed"))
    report = run_doctor()
    assert report.serious_count == 0
