from datetime import datetime, timezone

import httpx

from app.agent import cycle as cycle_mod
from app.candidate.profile import save_profile
from app.jobs_repo import list_discovery_log, list_jobs
from app.models import ApplicationState
from app.registry.models import CompanyRegistryEntry
from app.registry.repo import get_entry, insert_entry
from app.registry.scheduling import TenantHealth, compute_health


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _confirmed_greenhouse_response():
    return httpx.Response(200, json={"jobs": [{
        "id": 1, "title": "Backend Software Engineer",
        "content": (
            "We are hiring a Backend Software Engineer to build REST APIs in Python "
            "using FastAPI and PostgreSQL. Fully remote. Visa sponsorship available."
        ),
        "absolute_url": "https://boards.greenhouse.io/goodco/jobs/1",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "location": {"name": "Remote (US)"},
    }]})


def test_registry_driven_discovery_processes_due_tenants(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [])  # no legacy providers

    entry_id = insert_entry(CompanyRegistryEntry(
        company_name="GoodCo", provider="greenhouse", tenant_identifier="goodco",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        return _confirmed_greenhouse_response()

    def fake_build_provider(provider_name, tenant_identifier):
        from app.providers.greenhouse import GreenhouseProvider
        return GreenhouseProvider([tenant_identifier], client=_client_returning(handler))

    monkeypatch.setattr(cycle_mod, "build_provider_for_tenant", fake_build_provider)

    summary = cycle_mod.run_discovery_cycle()
    assert summary["jobs_new"] == 1
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0].application_state == ApplicationState.READY_TO_APPLY

    entry = get_entry(entry_id)
    assert entry.last_success_at is not None
    assert entry.consecutive_failures == 0

    logs = list_discovery_log()
    assert len(logs) == 1
    assert logs[0]["provider"] == "greenhouse"
    assert logs[0]["jobs_new"] == 1
    assert logs[0]["error_type"] == ""


def test_scenario_e_failing_tenant_marked_degraded_others_still_process(tmp_env, sample_profile, monkeypatch):
    """A tenant whose fetch raises outright (e.g. a structural/DNS failure,
    not a single-board hiccup a provider already isolates internally) must be
    marked degraded/failing via adaptive polling while other tenants keep
    processing normally in the same cycle."""
    save_profile(sample_profile)
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [])

    failing_id = insert_entry(CompanyRegistryEntry(
        company_name="FailingCo", provider="greenhouse", tenant_identifier="failingco",
    ))
    healthy_id = insert_entry(CompanyRegistryEntry(
        company_name="HealthyCo", provider="greenhouse", tenant_identifier="healthyco",
    ))

    class RaisingProvider:
        name = "greenhouse"

        def fetch_jobs(self, max_jobs):
            raise RuntimeError("tenant endpoint unreachable")

    def fake_build_provider(provider_name, tenant_identifier):
        from app.providers.greenhouse import GreenhouseProvider

        if tenant_identifier == "failingco":
            return RaisingProvider()

        def handler(request):
            return _confirmed_greenhouse_response()
        return GreenhouseProvider([tenant_identifier], client=_client_returning(handler))

    monkeypatch.setattr(cycle_mod, "build_provider_for_tenant", fake_build_provider)

    # Adaptive backoff intentionally makes a failing tenant NOT due again
    # immediately -- force each tenant due (as if enough wall-clock time had
    # passed) before every cycle to exercise several consecutive polls.
    from app.registry.repo import update_entry
    for _ in range(4):
        update_entry(failing_id, next_poll_at=None)
        update_entry(healthy_id, next_poll_at=None)
        cycle_mod.run_discovery_cycle()

    failing_entry = get_entry(failing_id)
    healthy_entry = get_entry(healthy_id)

    assert failing_entry.consecutive_failures >= 3
    assert compute_health(failing_entry.consecutive_failures, failing_entry.last_success_at) in (
        TenantHealth.DEGRADED, TenantHealth.FAILING,
    )
    assert compute_health(healthy_entry.consecutive_failures, healthy_entry.last_success_at) == TenantHealth.HEALTHY

    jobs = list_jobs()
    assert len(jobs) == 1  # the healthy tenant's job was still discovered
    assert jobs[0].company.lower().startswith("healthyco") or "healthyco" in jobs[0].url

    error_logs = [l for l in list_discovery_log(limit=100) if l["tenant"] == "failingco"]
    assert all(l["error_type"] for l in error_logs)


def test_disabled_registry_entry_is_never_polled(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [])
    calls = {"n": 0}

    insert_entry(CompanyRegistryEntry(
        company_name="DisabledCo", provider="greenhouse", tenant_identifier="disabledco", enabled=False,
    ))

    def fake_build_provider(provider_name, tenant_identifier):
        calls["n"] += 1
        raise AssertionError("should never be called for a disabled tenant")

    monkeypatch.setattr(cycle_mod, "build_provider_for_tenant", fake_build_provider)
    cycle_mod.run_discovery_cycle()
    assert calls["n"] == 0


def test_registry_tenant_backs_off_and_is_not_immediately_repolled(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [])

    entry_id = insert_entry(CompanyRegistryEntry(
        company_name="GoodCo", provider="greenhouse", tenant_identifier="goodco",
    ))

    call_count = {"n": 0}

    def fake_build_provider(provider_name, tenant_identifier):
        from app.providers.greenhouse import GreenhouseProvider
        call_count["n"] += 1

        def handler(request):
            return httpx.Response(200, json={"jobs": []})  # no new jobs -> slows down

        return GreenhouseProvider([tenant_identifier], client=_client_returning(handler))

    monkeypatch.setattr(cycle_mod, "build_provider_for_tenant", fake_build_provider)

    cycle_mod.run_discovery_cycle()
    assert call_count["n"] == 1
    entry = get_entry(entry_id)
    assert entry.next_poll_at is not None

    # A second cycle immediately after should NOT repoll (next_poll_at is in the future).
    cycle_mod.run_discovery_cycle()
    assert call_count["n"] == 1


def test_scheduler_handles_many_registry_tenants_in_one_cycle(tmp_env, sample_profile, monkeypatch):
    """Adaptive scheduling must scale to many configured tenants in a single
    cycle without special-casing -- this is the mechanism Phase 4 relies on."""
    save_profile(sample_profile)
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [])

    for i in range(25):
        insert_entry(CompanyRegistryEntry(
            company_name=f"Company {i}", provider="greenhouse", tenant_identifier=f"tenant{i}",
        ))

    def fake_build_provider(provider_name, tenant_identifier):
        from app.providers.greenhouse import GreenhouseProvider

        def handler(request):
            return httpx.Response(200, json={"jobs": [{
                "id": tenant_identifier, "title": "Backend Software Engineer",
                "content": (
                    "Build REST APIs in Python with FastAPI and PostgreSQL. Fully remote. "
                    "Visa sponsorship available."
                ),
                "absolute_url": f"https://boards.greenhouse.io/{tenant_identifier}/jobs/1",
                "location": {"name": "Remote (US)"},
            }]})
        return GreenhouseProvider([tenant_identifier], client=_client_returning(handler))

    monkeypatch.setattr(cycle_mod, "build_provider_for_tenant", fake_build_provider)

    summary = cycle_mod.run_discovery_cycle()
    assert summary["jobs_new"] == 25
    assert len(list_jobs()) == 25
    assert len(list_discovery_log(limit=100)) == 25
