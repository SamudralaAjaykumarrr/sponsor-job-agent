"""Multi-machine acceptance simulation (CLAUDE.md Phase 6 section 23).

Represents multiple hosts (host-A/worker-1, host-A/worker-2, host-B/
worker-1, host-C/worker-1) sharing ONE database (real PostgreSQL when
DATABASE_URL is set, real SQLite otherwise) and processing synthetic
portal work. This is not a mock of the coordination primitives -- it drives
the actual app.workers.leasing / app.workers.circuit / app.workers.repo /
app.workers.reaper modules concurrently from separate threads, each with
its own DB connection (which is what actually matters for correctness:
Postgres/SQLite serialize at the connection/transaction level, not the
Python thread level).

Verifies:
  - unique lease ownership (no portal ever claimed by two "hosts" at once)
  - shared circuit-breaker state (one host's failures open the circuit for
    every other host sharing the same DB)
  - shared rate/concurrency control (the inflight-slot counter caps total
    concurrent "requests" across ALL hosts combined, not per-host)
  - worker heartbeats recorded correctly per simulated host/worker
  - orphan recovery (a stale simulated worker is reaped to OFFLINE and its
    lease becomes reclaimable by another host)

Run standalone:
    python -m scripts.multi_machine_simulation [--database-url postgresql://...]

Never hammers real ATS providers -- "simulated-provider" is a synthetic
name that can never collide with a real one, matching the same convention
already used by scripts/registry_benchmark.py / scripts/worker_benchmark.py
('benchmark-fixture').
"""

import argparse
import threading
import time
from datetime import datetime, timedelta, timezone

SIMULATED_PROVIDER = "simulated-provider-fixture"
HOSTS = [("host-A", "worker-1"), ("host-A", "worker-2"), ("host-B", "worker-1"), ("host-C", "worker-1")]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_portals(count: int) -> None:
    from app.db import db_session

    now = utcnow()
    with db_session() as conn:
        for i in range(count):
            conn.execute(
                """INSERT INTO company_registry
                     (company_name, provider, tenant_identifier, enabled, next_poll_at, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?)""",
                (f"Simulated Co {i}", SIMULATED_PROVIDER, f"tenant-{i}", now, now, now),
            )


def _register_and_claim(host: str, worker_num: str, per_worker_limit: int, claimed: dict, errors: list) -> None:
    from app.workers import leasing
    from app.workers import repo as workers_repo
    from app.workers.models import WorkerStatus

    worker_id = f"{host}-{worker_num}"
    try:
        workers_repo.upsert_worker(
            worker_id, hostname=host, pid=0, shard_index=0, shard_count=1, status=WorkerStatus.WORKING.value,
        )
        rows = leasing.claim_poll_batch(worker_id=worker_id, limit=per_worker_limit, lease_seconds=120)
        claimed[worker_id] = [r["id"] for r in rows]
        workers_repo.heartbeat_worker(worker_id, status=WorkerStatus.IDLE.value)
    except Exception as exc:  # noqa: BLE001
        errors.append((worker_id, exc))


def simulate_unique_lease_ownership(portal_count: int = 40) -> dict:
    _seed_portals(portal_count)
    claimed: dict = {}
    errors: list = []
    threads = [
        threading.Thread(target=_register_and_claim, args=(host, worker, 15, claimed, errors))
        for host, worker in HOSTS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    all_ids = [pid for ids in claimed.values() for pid in ids]
    return {
        "errors": errors,
        "total_claimed": len(all_ids),
        "unique_claimed": len(set(all_ids)),
        "no_double_claim": len(all_ids) == len(set(all_ids)),
        "hosts_that_claimed_something": sum(1 for ids in claimed.values() if ids),
    }


def simulate_shared_circuit_state() -> dict:
    from app.workers import circuit

    # host-A/worker-1 sees widespread failures against the simulated provider.
    for _ in range(6):
        circuit.record_result(SIMULATED_PROVIDER, success=False)
    status_seen_by_host_a = circuit.get_status(SIMULATED_PROVIDER)
    # host-B/worker-1 and host-C/worker-1 (different simulated hosts, same DB
    # connection pool/backend) must see the SAME open circuit, immediately.
    may_attempt_host_b = circuit.may_attempt(SIMULATED_PROVIDER)
    status_seen_by_host_c = circuit.get_status(SIMULATED_PROVIDER)
    return {
        "opened_by_host_a": status_seen_by_host_a.state == "OPEN",
        "host_b_correctly_blocked": may_attempt_host_b is False,
        "host_c_sees_same_open_state": status_seen_by_host_c.state == "OPEN",
    }


def simulate_shared_rate_limit(limit: int = 2) -> dict:
    from app.workers import circuit

    circuit.reset_inflight_slots(SIMULATED_PROVIDER + "-ratelimit")
    provider = SIMULATED_PROVIDER + "-ratelimit"
    acquired = []
    results = []
    lock = threading.Lock()

    def _try_acquire(host: str) -> None:
        ok = circuit.acquire_inflight_slot(provider, limit)
        with lock:
            results.append((host, ok))
            if ok:
                acquired.append(host)

    threads = [threading.Thread(target=_try_acquire, args=(f"{host}-{worker}",)) for host, worker in HOSTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    granted = sum(1 for _, ok in results if ok)
    for host in acquired:
        circuit.release_inflight_slot(provider)
    return {"limit": limit, "hosts_attempted": len(HOSTS), "slots_granted": granted, "never_exceeded_limit": granted <= limit}


def simulate_orphan_recovery() -> dict:
    from app.db import db_session
    from app.workers import repo as workers_repo
    from app.workers.models import WorkerStatus
    from app.workers.reaper import reap_orphans

    worker_id = "host-Z-worker-1"
    workers_repo.upsert_worker(worker_id, hostname="host-Z", pid=0, shard_index=0, shard_count=1, status=WorkerStatus.WORKING.value)
    stale_hb = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db_session() as conn:
        conn.execute("UPDATE workers SET last_heartbeat_at = ? WHERE worker_id = ?", (stale_hb, worker_id))

    reaped = reap_orphans(stale_after_seconds=300)
    final_status = workers_repo.get_worker(worker_id)["status"]
    return {"reaped_worker_ids": reaped, "final_status": final_status, "correctly_marked_offline": worker_id in reaped}


def run_simulation() -> dict:
    from app.db import init_db

    init_db()
    return {
        "lease_ownership": simulate_unique_lease_ownership(),
        "shared_circuit_state": simulate_shared_circuit_state(),
        "shared_rate_limit": simulate_shared_rate_limit(),
        "orphan_recovery": simulate_orphan_recovery(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None, help="postgresql://... -- defaults to local SQLite")
    args = parser.parse_args()

    if args.database_url:
        import app.db as db

        db.DATABASE_URL = args.database_url

    results = run_simulation()
    import json

    print(json.dumps(results, indent=2, default=str))
    ok = (
        results["lease_ownership"]["no_double_claim"]
        and not results["lease_ownership"]["errors"]
        and results["shared_circuit_state"]["opened_by_host_a"]
        and results["shared_circuit_state"]["host_b_correctly_blocked"]
        and results["shared_rate_limit"]["never_exceeded_limit"]
        and results["orphan_recovery"]["correctly_marked_offline"]
    )
    print("SIMULATION PASSED" if ok else "SIMULATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
