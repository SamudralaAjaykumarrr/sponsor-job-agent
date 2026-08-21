import httpx

from app.providers.greenhouse import GreenhouseProvider
from app.registry import lifecycle, store, sync
from app.registry.models import CareerPortal, Company, IdentityStatus, PortalStatus, VerificationResult
from app.registry.verification import verify_portal


def _company(**overrides) -> Company:
    defaults = dict(normalized_name="acme", display_name="Acme Corp", primary_domain="acme.com")
    defaults.update(overrides)
    return Company(**defaults)


def _portal(company_id: int, **overrides) -> CareerPortal:
    defaults = dict(company_id=company_id, provider="greenhouse", tenant_identifier="acme",
                     support_level="FULL", verification_status=PortalStatus.CANDIDATE)
    defaults.update(overrides)
    return CareerPortal(**defaults)


def _factory_returning(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))

    def factory(provider, tenant):
        return GreenhouseProvider([tenant], client=client)

    return factory


def test_verify_portal_success_returns_verified(tmp_env):
    def handler(request):
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "SWE", "content": "desc", "location": {"name": "Remote"}}]})

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    portal = store.get_portal(pid)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = verify_portal(portal, company_display_name="Acme Corp", client=mock_client,
                             provider_factory=_factory_returning(handler))
    assert outcome.result == VerificationResult.VERIFIED
    assert outcome.jobs_seen == 1
    assert outcome.identity_status == IdentityStatus.MATCHED


def test_verify_portal_permanent_404_is_failed_not_temporary(tmp_env):
    def handler(request):
        return httpx.Response(404, text="not found")

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    portal = store.get_portal(pid)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = verify_portal(portal, company_display_name="Acme Corp", client=mock_client,
                             provider_factory=_factory_returning(handler))
    assert outcome.result == VerificationResult.FAILED
    assert outcome.is_permanent_failure


def test_verify_portal_timeout_is_temporary_failure(tmp_env):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    portal = store.get_portal(pid)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = verify_portal(portal, company_display_name="Acme Corp", client=mock_client,
                             provider_factory=_factory_returning(handler))
    assert outcome.result == VerificationResult.TEMPORARY_FAILURE
    assert not outcome.is_permanent_failure


def test_verify_portal_no_tenant_never_fabricates(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid, tenant_identifier=""))
    portal = store.get_portal(pid)

    outcome = verify_portal(portal, company_display_name="Acme Corp")
    assert outcome.result == VerificationResult.FAILED
    assert "tenant" in outcome.detail


def test_verify_portal_unsupported_provider(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid, provider="icims", tenant_identifier="acme", support_level="UNSUPPORTED"))
    portal = store.get_portal(pid)

    outcome = verify_portal(portal, company_display_name="Acme Corp")
    assert outcome.result == VerificationResult.UNSUPPORTED


def test_verify_portal_identity_mismatch_is_ambiguous(tmp_env):
    def handler(request):
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "SWE", "content": "desc", "location": {"name": "Remote"}}]})

    cid = store.insert_company(_company(display_name="Totally Different Staffing Co"))
    pid = store.insert_portal(_portal(cid, tenant_identifier="acme"))
    portal = store.get_portal(pid)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = verify_portal(portal, company_display_name="Totally Different Staffing Co", client=mock_client,
                             provider_factory=_factory_returning(handler))
    assert outcome.result == VerificationResult.AMBIGUOUS
    assert outcome.identity_status == IdentityStatus.MISMATCH


# --- lifecycle ---------------------------------------------------------

def test_apply_verified_outcome_promotes_to_verified(tmp_env):
    from app.registry.verification import VerificationOutcome

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    outcome = VerificationOutcome(VerificationResult.VERIFIED, detail="ok", jobs_seen=3, identity_status=IdentityStatus.MATCHED)
    updated = lifecycle.apply_verification_outcome(pid, outcome)
    assert updated.verification_status == PortalStatus.VERIFIED
    assert updated.current_job_count == 3


def test_temporary_failure_never_permanently_discards_portal(tmp_env):
    """Scenario G: a temporary provider timeout must not permanently disable
    the portal or count toward permanent-failure demotion."""
    from app.registry.verification import VerificationOutcome

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid, verification_status=PortalStatus.VERIFIED))
    outcome = VerificationOutcome(VerificationResult.TEMPORARY_FAILURE, detail="timeout")
    for _ in range(10):
        lifecycle.apply_verification_outcome(pid, outcome)
    portal = store.get_portal(pid)
    assert portal.verification_status == PortalStatus.VERIFIED
    assert portal.consecutive_permanent_failures == 0


def test_persistent_permanent_failure_demotes_active_portal_to_stale(tmp_env):
    """Scenario H: repeated permanent (404-like) failures eventually demote."""
    from app import config
    from app.registry.verification import VerificationOutcome

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid, verification_status=PortalStatus.ACTIVE))
    outcome = VerificationOutcome(VerificationResult.FAILED, detail="404", is_permanent_failure=True)
    for _ in range(config.REGISTRY_STALE_AFTER_PERMANENT_FAILURES):
        lifecycle.apply_verification_outcome(pid, outcome)
    portal = store.get_portal(pid)
    assert portal.verification_status == PortalStatus.STALE


def test_ambiguous_identity_quarantines(tmp_env):
    from app.registry.verification import VerificationOutcome

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    outcome = VerificationOutcome(VerificationResult.AMBIGUOUS, detail="mismatch", identity_status=IdentityStatus.MISMATCH)
    updated = lifecycle.apply_verification_outcome(pid, outcome)
    assert updated.verification_status == PortalStatus.QUARANTINED


def test_health_events_are_bounded(tmp_env):
    from app.registry.verification import VerificationOutcome

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid))
    outcome = VerificationOutcome(VerificationResult.TEMPORARY_FAILURE, detail="timeout")
    for _ in range(80):
        lifecycle.apply_verification_outcome(pid, outcome)
    from app.db import db_session

    with db_session() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM registry_portal_health_events WHERE portal_id = ?", (pid,)).fetchone()["c"]
    assert count <= 50


# --- sync ----------------------------------------------------------------

def test_sync_mirrors_verified_portal_into_operational_registry(tmp_env):
    from app.registry.repo import get_entry, get_entry_by_tenant

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid, verification_status=PortalStatus.VERIFIED))
    updated = sync.sync_portal_to_operational_registry(pid)

    assert updated.verification_status == PortalStatus.ACTIVE
    assert updated.registry_entry_id is not None
    mirrored = get_entry(updated.registry_entry_id)
    assert mirrored is not None
    assert mirrored.provider == "greenhouse"
    assert mirrored.tenant_identifier == "acme"
    assert mirrored.enabled is True


def test_sync_is_idempotent(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid, verification_status=PortalStatus.VERIFIED))
    sync.sync_portal_to_operational_registry(pid)
    from app.registry.repo import list_entries

    sync.sync_portal_to_operational_registry(pid)
    assert len(list_entries()) == 1  # no duplicate mirrored row


def test_sync_disables_operational_mirror_on_stale(tmp_env):
    from app.registry.repo import get_entry

    cid = store.insert_company(_company())
    pid = store.insert_portal(_portal(cid, verification_status=PortalStatus.VERIFIED))
    sync.sync_portal_to_operational_registry(pid)
    portal = store.get_portal(pid)

    store.update_portal(pid, verification_status=PortalStatus.STALE.value)
    sync.sync_portal_to_operational_registry(pid)

    mirrored = get_entry(portal.registry_entry_id)
    assert mirrored.enabled is False  # disabled, not deleted


# --- migration -------------------------------------------------------------

def test_migration_detected_when_old_portal_stale_and_new_verified(tmp_env):
    cid = store.insert_company(_company())
    old_id = store.insert_portal(_portal(cid, provider="greenhouse", tenant_identifier="acme",
                                          verification_status=PortalStatus.STALE, consecutive_permanent_failures=5))
    new_id = store.insert_portal(_portal(cid, provider="ashby", tenant_identifier="acme",
                                          verification_status=PortalStatus.VERIFIED))
    migration = lifecycle.maybe_detect_migration(cid, store.get_portal(new_id))
    assert migration is not None
    assert store.get_portal(old_id).superseded_by_portal_id == new_id


def test_no_migration_when_both_portals_healthy_two_legit_portals(tmp_env):
    """Scenario E: a company legitimately using two different ATSes at once
    must not trigger a spurious migration record."""
    cid = store.insert_company(_company())
    store.insert_portal(_portal(cid, provider="greenhouse", tenant_identifier="acme", verification_status=PortalStatus.ACTIVE))
    new_id = store.insert_portal(_portal(cid, provider="lever", tenant_identifier="acme", verification_status=PortalStatus.VERIFIED))
    migration = lifecycle.maybe_detect_migration(cid, store.get_portal(new_id))
    assert migration is None
