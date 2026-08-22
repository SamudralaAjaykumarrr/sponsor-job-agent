"""CLAUDE.md Phase 6 section 16/17: persistent schema-drift tracking
distinct from an empty board, and provider-wide drift (many tenants)
feeding the shared circuit breaker -- one oddball tenant must not."""

import httpx

from app import config
from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import circuit
from app.workers import schema_drift_repo
from app.workers.runner import Worker


def _seed(tenant: str) -> int:
    return registry_repo.insert_entry(
        CompanyRegistryEntry(company_name=f"Co {tenant}", provider="greenhouse", tenant_identifier=tenant)
    )


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    # Missing the expected top-level "jobs" key -- schema_check flags this
    # as drift, not as a healthy empty board.
    return httpx.Response(200, json={"unexpected_shape": True})


def test_single_tenant_drift_does_not_trip_circuit(tmp_env, mock_httpx):
    mock_httpx(_malformed_handler)
    _seed("acme")

    w = Worker(single_cycle=True)
    summary = w._run_cycle()

    drift_rows = schema_drift_repo.list_recent_drift()
    assert len(drift_rows) == 1
    assert drift_rows[0]["provider"] == "greenhouse"
    assert drift_rows[0]["occurrence_count"] == 1

    status = circuit.get_status("greenhouse")
    assert status.state == "CLOSED"


def test_repeated_drift_for_same_tenant_increments_occurrence_count(tmp_env, mock_httpx):
    mock_httpx(_malformed_handler)
    _seed("acme")

    Worker(single_cycle=True)._run_cycle()
    from app.registry import repo as registry_repo2

    # Force the portal due again immediately (bypass normal backoff scheduling).
    with __import__("app.db", fromlist=["db_session"]).db_session() as conn:
        conn.execute("UPDATE company_registry SET next_poll_at = NULL, lease_expires_at = NULL")
    Worker(single_cycle=True)._run_cycle()

    drift_rows = schema_drift_repo.list_recent_drift()
    assert len(drift_rows) == 1
    assert drift_rows[0]["occurrence_count"] == 2


def test_provider_wide_drift_across_many_tenants_trips_circuit(tmp_env, mock_httpx, monkeypatch):
    monkeypatch.setattr(config, "SCHEMA_DRIFT_CIRCUIT_TENANT_THRESHOLD", 3)
    mock_httpx(_malformed_handler)
    for i in range(3):
        _seed(f"tenant-{i}")

    w = Worker(single_cycle=True)
    w._run_cycle()

    drift_rows = schema_drift_repo.list_recent_drift(limit=10)
    assert len(drift_rows) == 3

    distinct = schema_drift_repo.distinct_tenants_with_recent_drift("greenhouse")
    assert distinct == 3

    status = circuit.get_status("greenhouse")
    assert status.consecutive_failures >= 1
