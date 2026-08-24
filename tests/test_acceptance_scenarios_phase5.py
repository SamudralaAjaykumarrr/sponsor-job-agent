"""Deterministic local multi-worker acceptance scenario -- CLAUDE.md Phase 5
section 39. No internet dependency (httpx is mocked). Runs 4 real worker
threads concurrently against 100 synthetic ACTIVE portals across 4 shards
and verifies every required property in one place."""

import random
import threading
from datetime import datetime, timedelta, timezone

import httpx

from app import config
from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import circuit
from app.workers import repo as workers_repo
from app.workers.leasing import claim_poll_batch
from app.workers.runner import Worker

_N_PORTALS = 100
_N_WORKERS = 4
_RECENT = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_portals(n: int) -> list[int]:
    ids = []
    for i in range(n):
        ids.append(registry_repo.insert_entry(
            CompanyRegistryEntry(company_name=f"Company{i}", provider="greenhouse", tenant_identifier=f"tenant{i}")
        ))
    return ids


def _handler_factory(fail_tenants: set[str], zero_job_tenants: set[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs[?content=true]
        tenant = url.split("/boards/", 1)[1].split("/")[0]
        if tenant in fail_tenants:
            return httpx.Response(500, text="transient failure")
        if tenant in zero_job_tenants:
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(200, json={"jobs": [
            {"id": f"{tenant}-1", "title": "Backend Software Engineer", "location": {"name": "Remote - US"},
             "content": "H-1B sponsorship available. Python role. Full-time.",
             "absolute_url": f"https://boards.greenhouse.io/{tenant}/jobs/1", "updated_at": _RECENT},
        ]})
    return handler


def test_local_four_worker_acceptance_scenario(tmp_env, mock_httpx, monkeypatch):
    # This scenario deliberately mixes healthy and failing portals on the
    # SAME provider to exercise leasing/sharding/idempotency/crash-recovery
    # at scale. Two provider-protection mechanisms are exercised precisely
    # and deterministically in their own dedicated tests instead of here,
    # since both are provider-wide and would otherwise nondeterministically
    # defer/cancel a variable number of these 100 portals depending on
    # thread interleaving: the circuit breaker (test_workers_retry_circuit.py)
    # and the provider concurrency limit (also there) -- both are
    # neutralized here so every portal actually resolves within one bounded
    # cycle, which is what this scenario is checking.
    monkeypatch.setattr(circuit, "may_attempt", lambda provider: True)
    monkeypatch.setattr(config, "PROVIDER_CONCURRENCY_DEFAULT", 1000)

    ids = _seed_portals(_N_PORTALS)
    tenants = [f"tenant{i}" for i in range(_N_PORTALS)]
    rng = random.Random(42)
    fail_tenants = set(rng.sample(tenants, 10))          # 10 portals: retryable failures
    zero_job_tenants = set(rng.sample([t for t in tenants if t not in fail_tenants], 15))  # 15: empty boards

    mock_httpx(_handler_factory(fail_tenants, zero_job_tenants))

    workers = [Worker(shard_index=i, shard_count=_N_WORKERS, single_cycle=True) for i in range(_N_WORKERS)]
    threads = [threading.Thread(target=w._run_cycle) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    attempts = workers_repo.list_recent_attempts(limit=500)
    assert len(attempts) == _N_PORTALS, "every eligible portal must be attempted exactly once"

    # No two attempts for the same portal overlap in time in a way that
    # would indicate a duplicate concurrent lease -- more directly: exactly
    # one attempt exists per portal (leasing already proven exclusive).
    by_portal: dict[int, list] = {}
    for a in attempts:
        by_portal.setdefault(a["portal_id"], []).append(a)
    assert all(len(v) == 1 for v in by_portal.values()), "no portal was polled more than once this cycle"

    succeeded = [a for a in attempts if a["status"] == "SUCCEEDED"]
    retryable = [a for a in attempts if a["status"] == "RETRYABLE_FAILURE"]
    assert len(retryable) == 10
    assert len(succeeded) == 90  # 15 empty-board + 75 with-jobs, all SUCCEEDED

    empty_board_succeeded = [a for a in succeeded if a["jobs_received"] == 0]
    assert len(empty_board_succeeded) == 15

    # Empty boards remain healthy/enabled, not quarantined.
    for entry_id in ids:
        entry = registry_repo.get_entry(entry_id)
        assert entry.enabled is True

    # Clean shutdown: no lease left held after all threads finished.
    for entry_id in ids:
        entry = registry_repo.get_entry(entry_id)
        assert entry.lease_owner is None

    # Retryable failures were retried-scheduled, not dead-lettered (below threshold).
    from app.workers import dead_letter
    assert dead_letter.list_dead_letters() == []

    # Duplicate jobs remain deduplicated: run a second, identical cycle
    # immediately (forcing due-again) and confirm no new canonical jobs.
    from app.jobs_repo import list_jobs

    jobs_before = len(list_jobs())
    for entry_id in ids:
        registry_repo.update_entry(entry_id, next_poll_at=None)
    workers2 = [Worker(shard_index=i, shard_count=_N_WORKERS, single_cycle=True) for i in range(_N_WORKERS)]
    threads2 = [threading.Thread(target=w._run_cycle) for w in workers2]
    for t in threads2:
        t.start()
    for t in threads2:
        t.join()
    jobs_after = len(list_jobs())
    assert jobs_after == jobs_before, "re-polling must not create duplicate canonical jobs"


def test_worker_crash_recovery_within_multi_worker_scenario(tmp_env, mock_httpx):
    """One extra scenario from section 7/39: worker A leases a portal and
    crashes (lease left to expire); worker B later claims and completes it
    successfully; the portal is never lost and no duplicate job is created."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [
            {"id": "1", "title": "Backend Software Engineer", "location": {"name": "Remote - US"},
             "content": "Sponsorship available. Python.", "absolute_url": "https://x/1",
             "updated_at": _RECENT},
        ]})

    mock_httpx(handler)
    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))

    # Worker A crashes mid-lease.
    claim_poll_batch(worker_id="worker-A", limit=1, lease_seconds=-1)  # expired immediately -- simulates a crash

    worker_b = Worker(single_cycle=True)
    worker_b._run_cycle()

    from app.jobs_repo import list_jobs
    assert len(list_jobs()) == 1

    all_attempts = workers_repo.list_attempts_for_portal("company_registry", entry_id, limit=10)
    assert len(all_attempts) == 1
    assert all_attempts[0]["status"] == "SUCCEEDED"
    assert all_attempts[0]["worker_id"] == worker_b.identity.worker_id

    entry = registry_repo.get_entry(entry_id)
    assert entry.lease_owner is None
    assert entry.next_poll_at is not None  # final schedule computed correctly
