"""Explicit, traceable tests for the Phase 4 acceptance scenarios listed in
CLAUDE.md section 37 (A-J). Most of the underlying behavior is already
exercised in more granular test files (test_registry_import.py,
test_registry_verification.py, test_registry_sharding.py, ...) -- this file
exists purely so each lettered scenario has one obvious, named test."""

import httpx

from app.providers.greenhouse import GreenhouseProvider
from app.registry import lifecycle, store, sync
from app.registry.importers import import_file
from app.registry.models import CareerPortal, Company, PortalStatus, VerificationResult
from app.registry.sharding import in_shard, shard_for_portal
from app.registry.verification import VerificationOutcome, verify_portal


def _company(**overrides) -> Company:
    defaults = dict(normalized_name="acme", display_name="Acme Corp", primary_domain="acme.com")
    defaults.update(overrides)
    return Company(**defaults)


def test_scenario_a_csv_100_companies_import_no_dupes_provenance_retained(tmp_env, tmp_path):
    header = "company_name,company_domain,provider,tenant_identifier,careers_url,country,source,source_url\n"
    rows = [header.strip()]
    for i in range(100):
        rows.append(f"Company {i},company{i}.com,greenhouse,company{i},https://boards.greenhouse.io/company{i},US,scenario_a,")
    path = tmp_path / "hundred.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    s1 = import_file(path)
    s2 = import_file(path)

    assert s1.rows_created == 100
    assert s2.rows_created == 0  # no duplicates on second import
    assert store.count_companies() == 100
    assert store.count_portals() == 100
    portal = store.list_portals(limit=1)[0]
    assert store.has_provenance(portal.id)  # provenance retained


def test_scenario_b_direct_greenhouse_link_verifies_and_activates(tmp_env):
    def handler(request):
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "SWE", "content": "d", "location": {"name": "Remote"}}]})

    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(
        company_id=cid, provider="greenhouse", tenant_identifier="acme", support_level="FULL",
        careers_url="https://boards.greenhouse.io/acme", verification_status=PortalStatus.CANDIDATE,
    ))
    portal = store.get_portal(pid)
    mock_client = httpx.Client(transport=httpx.MockTransport(handler))

    def factory(provider, tenant):
        return GreenhouseProvider([tenant], client=mock_client)

    outcome = verify_portal(portal, company_display_name="Acme Corp", client=mock_client, provider_factory=factory)
    lifecycle.apply_verification_outcome(pid, outcome)
    synced = sync.sync_portal_to_operational_registry(pid)

    assert synced.verification_status == PortalStatus.ACTIVE


def test_scenario_c_ambiguous_workday_like_url_stays_candidate_no_fake_tenant(tmp_env):
    from app.registry.importers import RegistryCandidate, import_candidates

    candidates = [RegistryCandidate(company_name="Ambiguous Co", careers_url="https://careers.ambiguousco.example/portal")]
    import_candidates(candidates, source_name="scenario_c")
    portal = store.list_portals()[0]
    assert portal.tenant_identifier == ""
    assert portal.verification_status in (PortalStatus.DISCOVERED, PortalStatus.CANDIDATE)


def test_scenario_d_two_sources_same_portal_one_portal_two_provenance(tmp_env, tmp_path):
    header = "company_name,company_domain,provider,tenant_identifier,careers_url,country,source,source_url\n"
    path_a = tmp_path / "source_a.csv"
    path_a.write_text(header + "Acme Corp,acme.com,greenhouse,acme,https://boards.greenhouse.io/acme,US,source_a,\n", encoding="utf-8")
    path_b = tmp_path / "source_b.csv"
    path_b.write_text(header + "Acme Corp,acme.com,greenhouse,acme,https://boards.greenhouse.io/acme,US,source_b,\n", encoding="utf-8")

    import_file(path_a, source_name="source_a")
    import_file(path_b, source_name="source_b")

    assert store.count_portals() == 1  # one portal
    portal = store.list_portals()[0]
    provenance = store.list_provenance_for_portal(portal.id)
    assert len(provenance) == 2  # two provenance records
    assert {p.source_name for p in provenance} == {"source_a", "source_b"}


def test_scenario_e_two_legit_portals_for_same_company_both_retained(tmp_env):
    cid = store.insert_company(_company())
    store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme", verification_status=PortalStatus.ACTIVE))
    new_id = store.insert_portal(CareerPortal(company_id=cid, provider="lever", tenant_identifier="acme", verification_status=PortalStatus.VERIFIED))

    migration = lifecycle.maybe_detect_migration(cid, store.get_portal(new_id))
    assert migration is None
    assert len(store.list_portals_for_company(cid)) == 2  # both retained


def test_scenario_f_ats_migration_detected_old_stale_new_active_history_retained(tmp_env):
    cid = store.insert_company(_company())
    old_id = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                               verification_status=PortalStatus.STALE, consecutive_permanent_failures=5))
    new_id = store.insert_portal(CareerPortal(company_id=cid, provider="ashby", tenant_identifier="acme",
                                               verification_status=PortalStatus.VERIFIED))

    migration = lifecycle.maybe_detect_migration(cid, store.get_portal(new_id))
    assert migration is not None
    assert store.get_portal(old_id).verification_status == PortalStatus.STALE  # old portal retained, marked stale
    assert store.get_portal(old_id).superseded_by_portal_id == new_id
    history = lifecycle.list_migrations_for_company(cid)
    assert len(history) == 1


def test_scenario_g_temporary_timeout_not_permanently_disabled(tmp_env):
    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="acme",
                                            verification_status=PortalStatus.ACTIVE))
    outcome = VerificationOutcome(VerificationResult.TEMPORARY_FAILURE, detail="timed out")
    lifecycle.apply_verification_outcome(pid, outcome)
    portal = store.get_portal(pid)
    assert portal.verification_status == PortalStatus.ACTIVE
    assert portal.enabled is True


def test_scenario_h_persistent_invalid_tenant_marked_failing_deterministically(tmp_env):
    from app import config

    cid = store.insert_company(_company())
    pid = store.insert_portal(CareerPortal(company_id=cid, provider="greenhouse", tenant_identifier="doesnotexist",
                                            verification_status=PortalStatus.CANDIDATE))
    outcome = VerificationOutcome(VerificationResult.FAILED, detail="404 not found", is_permanent_failure=True)
    for _ in range(config.REGISTRY_STALE_AFTER_PERMANENT_FAILURES):
        lifecycle.apply_verification_outcome(pid, outcome)
    portal = store.get_portal(pid)
    assert portal.verification_status == PortalStatus.QUARANTINED


def test_scenario_i_50k_synthetic_portals_bounded_due_query_no_full_load(tmp_env):
    """A lighter in-process version of the dedicated scripts/registry_benchmark.py
    50k run -- confirms list_due_for_verification never grows unbounded
    with table size, without slowing down the main pytest suite."""
    cid = store.insert_company(_company())
    from app.registry.store import bulk_insert_portals_raw, utcnow

    now = utcnow()
    n = 5000  # smaller than the full 50k benchmark to keep pytest fast; same code path
    rows = [dict(
        company_id=cid, provider="benchmark-fixture", tenant_identifier=f"synthetic-{i}", careers_url="", jobs_url="",
        canonical_url=f"https://benchmark-fixture.invalid/{i}", support_level="UNSUPPORTED",
        discovery_status="IMPORTED", verification_status="DISCOVERED", identity_status="UNKNOWN",
        enabled=1, confidence=0, confidence_reasons="[]", last_verified_at=None, last_polled_at=None,
        next_poll_at=None, last_success_at=None, last_failure_at=None, consecutive_failures=0,
        consecutive_permanent_failures=0, average_job_yield=0.0, average_latency_ms=0.0, current_job_count=0,
        poll_interval_minutes=15, registry_entry_id=None, superseded_by_portal_id=None, notes="synthetic",
        created_at=now, updated_at=now,
    ) for i in range(n)]
    bulk_insert_portals_raw(rows)

    due = store.list_due_for_verification(limit=200)
    assert len(due) == 200  # bounded regardless of table having 5000 rows


def test_scenario_j_4_shard_configuration_every_portal_exactly_one_shard():
    shard_count = 4
    for portal_id in range(1, 501):
        matches = sum(1 for idx in range(shard_count) if in_shard(portal_id, shard_count, idx))
        assert matches == 1
