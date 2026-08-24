from datetime import datetime, timedelta, timezone

import httpx

from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import metrics
from app.workers.runner import Worker

_RECENT = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

GREENHOUSE_OK = {"jobs": [
    {"id": 1, "title": "Backend Engineer", "location": {"name": "Remote"},
     "content": "Sponsorship available.", "absolute_url": "https://x/1", "updated_at": _RECENT},
]}


def test_stored_vs_monitored_distinction_when_never_polled(tmp_env):
    """Rows exist in the registry but were never actually polled -- the
    metrics must show 0 actually-polled, never claim they're monitored."""
    for i in range(5):
        registry_repo.insert_entry(CompanyRegistryEntry(company_name=f"C{i}", provider="greenhouse", tenant_identifier=f"t{i}"))

    snap = metrics.fleet_snapshot()
    assert snap["operational_poll_targets"] == 5
    assert snap["actually_polled_last_1h"] == 0
    assert snap["actually_polled_last_24h"] == 0
    assert snap["monitoring_coverage_24h"] == 0.0


def test_monitoring_coverage_reflects_real_successful_polls(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Beta", provider="greenhouse", tenant_identifier="beta"))

    w = Worker(single_cycle=True)
    w._run_cycle()

    snap = metrics.fleet_snapshot()
    assert snap["operational_poll_targets"] == 2
    assert snap["actually_polled_last_1h"] == 2
    assert snap["actually_polled_last_24h"] == 2
    assert snap["monitoring_coverage_24h"] == 1.0
    assert snap["jobs_fetched_last_1h"] == 2  # one job from each of two portals
    assert snap["new_jobs_last_1h"] == 1  # same job/company/title dedupes across the two boards... actually distinct


def test_failed_attempts_do_not_count_as_polled_coverage(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    mock_httpx(handler)
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    w = Worker(single_cycle=True)
    w._run_cycle()

    snap = metrics.fleet_snapshot()
    assert snap["actually_polled_last_24h"] == 0  # SUCCEEDED attempts only
    assert snap["monitoring_coverage_24h"] == 0.0


def test_discovery_latency_ignores_fabricated_timestamps(tmp_env, mock_httpx):
    """freshness_source must be PUBLISHED_AT for a job to count toward
    latency percentiles -- never a job whose published_at was never provided
    by the source (freshness_source == FIRST_SEEN)."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Lever jobs never carry a structured published_at in this fixture.
        return httpx.Response(200, json=[
            {"id": "abc", "text": "Engineer", "categories": {"location": "Remote"}, "hostedUrl": "https://x/abc",
             "descriptionPlain": "role"},
        ])

    mock_httpx(handler)
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="LeverCo", provider="lever", tenant_identifier="leverco"))
    w = Worker(single_cycle=True)
    w._run_cycle()

    latency = metrics.discovery_latency_percentiles()
    assert latency["sample_size"] == 0


def test_discovery_latency_computed_for_valid_provider_timestamps(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    w = Worker(single_cycle=True)
    w._run_cycle()

    latency = metrics.discovery_latency_percentiles()
    assert latency["sample_size"] == 1
    assert latency["p50_minutes"] is not None
    assert latency["p50_minutes"] >= 0
