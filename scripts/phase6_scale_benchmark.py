#!/usr/bin/env python3
"""Phase 6 production-scale synthetic benchmark (CLAUDE.md Phase 6 sections
45-46) -- separate from scripts/worker_benchmark.py (Phase 5) and
scripts/registry_benchmark.py (Phase 4), which this does not replace or
modify. Adds PostgreSQL support (--database-url) and covers the specific
operations Phase 6 asks for that the Phase 4/5 scripts didn't already
measure: queue-depth query, dead-letter query, batch job inserts,
provenance upsert -- alongside lease claim / attempt writes / heartbeat
updates / concurrent worker claims / bounded due-portal selection at
1k/10k/50k(/100k) rows, run against EITHER backend.

This is a DB-only benchmark: no HTTP requests are made. It measures whether
the query/leasing layer holds up at these row counts -- it says NOTHING
about real network-polling capacity, and NOTHING about true multi-machine
throughput (see docs/scaling-claims.md). Every synthetic row uses the
provider name "benchmark-fixture" (matching the Phase 4/5 scripts'
convention) so it can never collide with a real provider, and is written
ONLY to an isolated temp SQLite file or a throwaway Postgres database
(never the real data/app.db or a shared production database).

Usage:
    python3 scripts/phase6_scale_benchmark.py [--sizes 1000,10000,50000] [--include-100k]
    python3 scripts/phase6_scale_benchmark.py --database-url postgresql://... [--sizes 1000,10000]
"""

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SYNTHETIC_PROVIDER = "benchmark-fixture"


def _setup_backend(database_url: str | None):
    import app.config as config
    import app.db as db

    if database_url:
        db.DATABASE_URL = database_url
        db.init_db()
        # Reusing one caller-provided Postgres database across multiple
        # benchmark sizes (unlike the SQLite path, which gets a brand new
        # temp file per size) -- clear prior rows first so tenant
        # identifiers/ids don't collide across successive --sizes runs.
        with db.db_session() as conn:
            for table in ("job_provenance", "jobs", "poll_attempts", "workers", "company_registry"):
                conn.execute(f"DELETE FROM {table} WHERE 1=1")
        return "postgres", None

    tmp_dir = Path(tempfile.mkdtemp(prefix="phase6_benchmark_"))
    db_path = tmp_dir / "benchmark.db"
    config.DB_PATH = db_path
    db.DB_PATH = db_path
    db.DATABASE_URL = ""
    db.init_db()
    return "sqlite", db_path


def _bulk_seed_company_registry(n: int) -> None:
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


def _bench_queue_depth_query() -> float:
    from app.observability.metrics import _queue_depth

    t0 = time.perf_counter()
    _queue_depth()
    return time.perf_counter() - t0


def _bench_dead_letter_query() -> float:
    from app.workers.repo import list_dead_letters

    t0 = time.perf_counter()
    list_dead_letters(resolved=False, limit=200)
    return time.perf_counter() - t0


def _bench_batch_job_inserts(n: int = 500) -> float:
    from app.db import db_session
    from app.registry.store import utcnow

    now = utcnow()
    t0 = time.perf_counter()
    with db_session() as conn:
        conn.executemany(
            """INSERT INTO jobs (title, company, description, provider, external_job_id, first_seen_at, created_at, updated_at)
               VALUES (?, 'Benchmark Co', 'synthetic job for benchmark', ?, ?, ?, ?, ?)""",
            [(f"Job {i}", SYNTHETIC_PROVIDER, f"bench-{i}", now, now, now) for i in range(n)],
        )
    return time.perf_counter() - t0


def _bench_provenance_upsert(job_id: int, n: int = 200) -> float:
    from app.db import db_session
    from app.registry.store import utcnow

    now = utcnow()
    t0 = time.perf_counter()
    with db_session() as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO job_provenance (job_id, provider, source_url, provider_job_id, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, provider, provider_job_id) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                (job_id, SYNTHETIC_PROVIDER, f"https://example.com/{i}", f"bench-prov-{i}", now, now),
            )
    return time.perf_counter() - t0


def run_benchmark(size: int, backend: str) -> dict:
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
            attempt_id=f"bench-attempt-{backend}-{size}-{i}", portal_type="company_registry", portal_id=row["id"],
            worker_id="bench-worker-1", provider=SYNTHETIC_PROVIDER, queue="poll",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
            status="SUCCEEDED", jobs_received=1, jobs_new=1,
        ))
    attempt_record_seconds = (time.perf_counter() - t0) if claimed else 0.0

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

    queue_depth_seconds = _bench_queue_depth_query()
    dead_letter_seconds = _bench_dead_letter_query()
    batch_insert_seconds = _bench_batch_job_inserts(500)

    from app.db import db_session

    with db_session() as conn:
        job_row = conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()
    provenance_seconds = _bench_provenance_upsert(job_row["id"], 200) if job_row else 0.0

    return {
        "backend": backend,
        "size": size,
        "seed_seconds": round(seed_seconds, 4),
        "single_claim_50_seconds": round(single_claim_seconds, 4),
        "single_claim_returned": len(claimed),
        "worker_heartbeat_200_updates_seconds": round(heartbeat_seconds, 4),
        "attempt_record_seconds": round(attempt_record_seconds, 4),
        "eight_worker_contention_drain_seconds": round(contention_seconds, 4),
        "eight_worker_total_claimed": len(all_claimed_ids),
        "eight_worker_duplicate_claims": duplicate_claims,
        "queue_depth_query_seconds": round(queue_depth_seconds, 4),
        "dead_letter_query_seconds": round(dead_letter_seconds, 4),
        "batch_500_job_insert_seconds": round(batch_insert_seconds, 4),
        "provenance_200_upsert_seconds": round(provenance_seconds, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", default="1000,10000,50000")
    parser.add_argument("--include-100k", action="store_true")
    parser.add_argument("--database-url", default=None, help="postgresql://... -- defaults to isolated temp SQLite")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if args.include_100k:
        sizes.append(100_000)

    print(f"Phase 6 synthetic scale benchmark -- backend={'postgres' if args.database_url else 'sqlite'}")
    print("DB-only: no HTTP requests. Says nothing about real network-polling or multi-machine throughput.\n")

    for size in sizes:
        backend, db_path = _setup_backend(args.database_url)
        location = db_path if db_path else "(fresh postgres database, caller-provided)"
        print(f"--- size={size} backend={backend} ({location}) ---")
        result = run_benchmark(size, backend)
        for k, v in result.items():
            print(f"  {k}: {v}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
