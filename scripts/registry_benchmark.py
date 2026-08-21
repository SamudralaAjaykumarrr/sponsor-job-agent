#!/usr/bin/env python3
"""Synthetic registry scale benchmark -- Phase 4 section 33.

Generates synthetic portal rows ONLY in a throwaway temp SQLite database
(never the real data/app.db) and measures bulk import, dedup lookup, due-
portal query, pagination, shard assignment, and export at 1k/10k/50k (and
optionally 100k) rows. This is a DB-only benchmark: it says nothing about
network-polling scalability, only about whether the registry's storage/query
layer holds up at these row counts on a single machine.

Usage:
    python3 scripts/registry_benchmark.py [--sizes 1000,10000,50000] [--include-100k]

Every synthetic row's tenant_identifier is prefixed "synthetic-" and its
provider is always "benchmark-fixture" -- this is never a real provider name,
so a synthetic row could never be mistaken for a real registry entry even if
someone pointed this script at the wrong DB by mistake (it doesn't -- see
below)."""

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SYNTHETIC_PROVIDER = "benchmark-fixture"


def _build_temp_db():
    """Isolated temp DB -- completely separate file from the real app.db,
    regardless of what app.config.DB_PATH currently points to."""
    import app.config as config
    import app.db as db

    tmp_dir = Path(tempfile.mkdtemp(prefix="registry_benchmark_"))
    db_path = tmp_dir / "benchmark.db"
    config.DB_PATH = db_path
    db.DB_PATH = db_path
    db.init_db()
    return db_path


def _generate_rows(n: int, start_id_hint: int):
    from app.registry.store import utcnow

    now = utcnow()
    for i in range(n):
        idx = start_id_hint + i
        yield dict(
            company_id=1, provider=SYNTHETIC_PROVIDER, tenant_identifier=f"synthetic-{idx}",
            careers_url="", jobs_url="", canonical_url=f"https://benchmark-fixture.invalid/synthetic-{idx}",
            support_level="UNSUPPORTED", discovery_status="IMPORTED", verification_status="DISCOVERED",
            identity_status="UNKNOWN", enabled=1, confidence=0, confidence_reasons="[]",
            last_verified_at=None, last_polled_at=None, next_poll_at=None, last_success_at=None,
            last_failure_at=None, consecutive_failures=0, consecutive_permanent_failures=0,
            average_job_yield=0.0, average_latency_ms=0.0, current_job_count=0, poll_interval_minutes=15,
            registry_entry_id=None, superseded_by_portal_id=None, notes="synthetic benchmark row",
            created_at=now, updated_at=now,
        )


def run_benchmark(size: int) -> dict:
    from app.registry import store
    from app.registry.export import export_jsonl
    from app.registry.models import Company
    from app.registry.sharding import shard_for_portal

    company_id = store.insert_company(Company(normalized_name="benchmark", display_name="Benchmark Co", primary_domain=""))

    t0 = time.perf_counter()
    batch_size = 5000
    inserted = 0
    rows_buffer = []
    for row in _generate_rows(size, start_id_hint=inserted):
        row["company_id"] = company_id
        rows_buffer.append(row)
        if len(rows_buffer) >= batch_size:
            inserted += store.bulk_insert_portals_raw(rows_buffer)
            rows_buffer = []
    if rows_buffer:
        inserted += store.bulk_insert_portals_raw(rows_buffer)
    bulk_import_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    for i in range(0, min(size, 500), max(1, size // 500 or 1)):
        store.get_portal_by_provider_tenant(SYNTHETIC_PROVIDER, f"synthetic-{i}")
    dedup_lookup_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    due = store.list_due_for_verification(limit=200)
    due_query_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    page = store.list_portals(limit=200)
    after_id = page[-1].id if page else 0
    page2 = store.list_portals(limit=200, after_id=after_id)
    pagination_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    all_ids = store.all_portal_ids()
    for pid in all_ids:
        shard_for_portal(pid, 4)
    shard_seconds = time.perf_counter() - t0

    export_path = Path(tempfile.gettempdir()) / f"registry_benchmark_export_{size}.jsonl"
    t0 = time.perf_counter()
    exported = export_jsonl(export_path)
    export_seconds = time.perf_counter() - t0
    export_path.unlink(missing_ok=True)

    return {
        "size": size, "inserted": inserted, "bulk_import_seconds": round(bulk_import_seconds, 4),
        "dedup_lookup_seconds_for_500_lookups": round(dedup_lookup_seconds, 4),
        "due_query_seconds": round(due_query_seconds, 4), "due_rows_returned": len(due),
        "pagination_seconds_for_2_pages": round(pagination_seconds, 4),
        "shard_assignment_seconds_all_rows": round(shard_seconds, 4),
        "export_seconds": round(export_seconds, 4), "exported_rows": exported,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000,50000")
    parser.add_argument("--include-100k", action="store_true")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if args.include_100k:
        sizes.append(100_000)

    print("Synthetic registry scale benchmark -- DB-only, isolated temp SQLite file per run.")
    print("This measures storage/query performance ONLY, not network-polling scalability.\n")

    for size in sizes:
        db_path = _build_temp_db()
        print(f"--- size={size} (temp db: {db_path}) ---")
        result = run_benchmark(size)
        for k, v in result.items():
            print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    main()
