#!/usr/bin/env python3
"""Synthetic Phase 5 worker-fleet scale benchmark -- CLAUDE.md Phase 5
section 38.

Generates synthetic company_registry rows ONLY in a throwaway temp SQLite
database (never the real data/app.db) and measures: lease acquisition, due
work selection, attempt recording, worker heartbeat, multi-worker
contention, retry-queue filtering, and a bounded due-query at 1k/10k/50k
(and optionally 100k) rows.

This is a DB-only benchmark: it says nothing about real network-polling
capacity (no HTTP requests are made), only about whether the leasing/
execution-bookkeeping layer's SQLite queries hold up at these row counts on
a single machine -- see CLAUDE.md Phase 5 section 37/section 38's own
"Do not claim these measure network capacity" instruction.

Usage:
    python3 scripts/worker_benchmark.py [--sizes 1000,10000,50000] [--include-100k]

Every synthetic row's tenant_identifier is prefixed "synthetic-" and its
provider is always "benchmark-fixture" (matching scripts/registry_benchmark.py's
convention) -- never a real provider name, and always in an isolated temp DB."""

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SYNTHETIC_PROVIDER = "benchmark-fixture"


def _build_temp_db():
    import app.config as config
    import app.db as db

    tmp_dir = Path(tempfile.mkdtemp(prefix="worker_benchmark_"))
    db_path = tmp_dir / "benchmark.db"
    config.DB_PATH = db_path
    db.DB_PATH = db_path
    db.init_db()
    return db_path


def _bulk_seed_company_registry(n: int) -> None:
    """Direct bulk insert -- app.registry.repo.insert_entry is one row per
    call (fine for real usage, far too slow for a 100k-row benchmark
    fixture), so this benchmark script writes rows directly, same
    philosophy as scripts/registry_benchmark.py's bulk_insert_portals_raw."""
    from app.db import db_session
    from app.registry.store import utcnow

    now = utcnow()
    with db_session() as conn:
        conn.executemany(
            """INSERT INTO company_registry
                 (company_name, provider, tenant_identifier, enabled, support_level,
                  poll_interval_minutes, created_at, updated_at)
               VALUES (?, ?, ?, 1, 'FULL', 15, ?, ?)""",
            [(f"Synthetic Co {i}", SYNTHETIC_PROVIDER, f"synthetic-{i}", now, now) for i in range(n)],
        )


def run_benchmark(size: int) -> dict:
    from app.workers.leasing import claim_poll_batch
    from app.workers.repo import heartbeat_worker, record_attempt, upsert_worker
    from app.workers.models import AttemptRecord

    t0 = time.perf_counter()
    _bulk_seed_company_registry(size)
    seed_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    claimed = claim_poll_batch(worker_id="bench-single", limit=50, lease_seconds=120)
    single_claim_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    upsert_worker("bench-worker-1", hostname="bench", pid=1, shard_index=0, shard_count=1, status="WORKING")
    for i in range(200):
        heartbeat_worker("bench-worker-1", status="WORKING", portals_processed=i, jobs_processed=i * 2, errors=0)
    heartbeat_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    for i, row in enumerate(claimed):
        record_attempt(AttemptRecord(
            attempt_id=f"bench-attempt-{i}", portal_type="company_registry", portal_id=row["id"],
            worker_id="bench-worker-1", provider=SYNTHETIC_PROVIDER, queue="poll",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
            status="SUCCEEDED", jobs_received=1, jobs_new=1,
        ))
    attempt_record_seconds = (time.perf_counter() - t0) if claimed else 0.0

    # Multi-worker contention: 8 threads racing to drain whatever is left
    # due, verifying zero duplicate claims and measuring wall time.
    results: dict[str, list[int]] = {}
    lock = threading.Lock()

    def drain(worker_id: str) -> None:
        local = []
        while True:
            batch = claim_poll_batch(worker_id=worker_id, limit=200, lease_seconds=120)
            if not batch:
                break
            local.extend(r["id"] for r in batch)
        with lock:
            results[worker_id] = local

    t0 = time.perf_counter()
    threads = [threading.Thread(target=drain, args=(f"bench-worker-contend-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    contention_seconds = time.perf_counter() - t0

    all_claimed_ids = [i for v in results.values() for i in v]
    duplicate_claims = len(all_claimed_ids) - len(set(all_claimed_ids))

    t0 = time.perf_counter()
    from app.db import db_session

    with db_session() as conn:
        retryable = conn.execute(
            "SELECT COUNT(*) AS c FROM poll_attempts WHERE status = 'RETRYABLE_FAILURE'"
        ).fetchone()["c"]
    retry_queue_query_seconds = time.perf_counter() - t0

    return {
        "size": size,
        "seed_seconds": round(seed_seconds, 4),
        "single_claim_50_seconds": round(single_claim_seconds, 4),
        "single_claim_returned": len(claimed),
        "worker_heartbeat_200_updates_seconds": round(heartbeat_seconds, 4),
        "attempt_record_seconds": round(attempt_record_seconds, 4),
        "attempt_record_count": len(claimed),
        "eight_worker_contention_drain_seconds": round(contention_seconds, 4),
        "eight_worker_total_claimed": len(all_claimed_ids),
        "eight_worker_duplicate_claims": duplicate_claims,
        "retry_queue_query_seconds": round(retry_queue_query_seconds, 4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000,50000")
    parser.add_argument("--include-100k", action="store_true")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if args.include_100k:
        sizes.append(100_000)

    print("Synthetic Phase 5 worker-fleet scale benchmark -- DB-only, isolated temp SQLite file per run.")
    print("This measures leasing/bookkeeping query performance ONLY, not real network-polling capacity.\n")

    for size in sizes:
        db_path = _build_temp_db()
        print(f"--- size={size} (temp db: {db_path}) ---")
        result = run_benchmark(size)
        for k, v in result.items():
            print(f"  {k}: {v}")
        assert result["eight_worker_duplicate_claims"] == 0, "BENCHMARK INVARIANT VIOLATED: duplicate claims detected"
        print()


if __name__ == "__main__":
    main()
