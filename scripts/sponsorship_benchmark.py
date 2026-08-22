#!/usr/bin/env python3
"""Synthetic sponsorship-evidence scale benchmark -- CLAUDE.md Phase 7
section 40.

Generates synthetic evidence rows ONLY in a throwaway temp SQLite database
(never the real data/app.db) and measures streaming/batched import, company
identity matching, employer profile aggregation, and company/role/location
lookup at 10k/100k/500k (and optionally 1M) rows. This is a DB-only
benchmark: it says nothing about a real government dataset's actual size,
quality, or the correctness of its schema mapping -- see
tests/test_sponsorship_importers.py and the small bounded real-data
validation reported separately in docs/sponsorship-data-import.md for that.

Every synthetic row's source_record_id is prefixed "synthetic-" and its
dataset_name is always "benchmark-fixture" -- this can never collide with a
real dataset, matching the Phase 4/5/6 benchmark convention
(registry_benchmark.py, worker_benchmark.py, phase6_scale_benchmark.py).

Usage:
    python3 scripts/sponsorship_benchmark.py [--sizes 10000,100000] [--include-500k] [--include-1m]

Never add this script's sizes to the normal pytest run (CLAUDE.md section 40)."""

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SYNTHETIC_DATASET = "benchmark-fixture"
SYNTHETIC_COMPANY_COUNT = 500  # evidence rows are spread across this many companies


def _build_temp_db():
    import app.config as config
    import app.db as db

    tmp_dir = Path(tempfile.mkdtemp(prefix="sponsorship_benchmark_"))
    db_path = tmp_dir / "benchmark.db"
    config.DB_PATH = db_path
    db.DB_PATH = db_path
    db.init_db()
    return db_path


def _seed_companies(n: int) -> list[int]:
    from app.registry.models import Company
    from app.registry import store

    ids = []
    for i in range(n):
        cid = store.insert_company(Company(
            normalized_name=f"benchmarkco{i}", display_name=f"BenchmarkCo{i}",
            primary_domain=f"benchmarkco{i}.invalid",
        ))
        ids.append(cid)
    return ids


def _generate_evidence_rows(n: int, dataset_id: int, company_ids: list[int]):
    from app.sponsorship.evidence import SponsorshipEvidence

    occupations = [
        ("15-1252", "Software Developers, Applications"),
        ("15-1211", "Computer Systems Analysts"),
        ("41-4012", "Sales Representatives"),
    ]
    states = ["CA", "NY", "TX", "WA", "MA"]
    for i in range(n):
        cid = company_ids[i % len(company_ids)]
        occ_code, occ_title = occupations[i % len(occupations)]
        yield SponsorshipEvidence(
            company_id=cid, company_name_raw=f"BenchmarkCo{i % len(company_ids)}",
            source_type="DOL_LCA_DATA", dataset_id=dataset_id, source_record_id=f"synthetic-{i}",
            fiscal_year=2022 + (i % 4), occupation_code=occ_code, occupation_title=occ_title,
            worksite_state=states[i % len(states)], job_title="Software Engineer", confidence=50,
        )


def _time_it(label: str, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"  {label:<45} {elapsed:>8.3f}s")
    return result, elapsed


def run_benchmark(n: int, company_ids: list[int]) -> dict:
    from app.sponsorship.datasets import get_or_create_dataset
    from app.sponsorship.evidence import bulk_record_evidence_idempotent, count_evidence

    print(f"\n=== N={n} ===")
    dataset = get_or_create_dataset(SYNTHETIC_DATASET, dataset_version=f"n{n}")
    dataset_id = dataset["id"]

    def _import():
        batch = []
        created_total = 0
        for ev in _generate_evidence_rows(n, dataset_id, company_ids):
            batch.append(ev)
            if len(batch) >= 5000:
                created, _ = bulk_record_evidence_idempotent(batch)
                created_total += created
                batch.clear()
        if batch:
            created, _ = bulk_record_evidence_idempotent(batch)
            created_total += created
        return created_total

    created, t_import = _time_it(f"streaming+batched import of {n} rows", _import)

    def _recompute_all_profiles():
        from app.sponsorship.profile import refresh_employer_profile

        for cid in company_ids:
            refresh_employer_profile(cid)
        return len(company_ids)

    _, t_profiles = _time_it(f"recompute profiles for {SYNTHETIC_COMPANY_COUNT} companies", _recompute_all_profiles)

    def _company_lookup():
        from app.sponsorship.profile import get_cached_profile

        for cid in company_ids[:100]:
            get_cached_profile(cid)
        return 100

    _, t_lookup = _time_it("100 cached-profile lookups", _company_lookup)

    def _decision_roundtrip():
        from app.sponsorship.decision import decide_sponsorship

        for i in range(50):
            decide_sponsorship("Backend Software Engineer", f"BenchmarkCo{i}", "Join our backend team.", "CA")
        return 50

    _, t_decision = _time_it("50 decide_sponsorship() calls (role+location similarity)", _decision_roundtrip)

    print(f"  rows created:        {created}")
    print(f"  total evidence rows: {count_evidence()}")

    return {
        "n": n, "import_seconds": t_import, "profile_seconds": t_profiles,
        "lookup_seconds": t_lookup, "decision_seconds": t_decision, "rows_created": created,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="10000,100000")
    parser.add_argument("--include-500k", action="store_true")
    parser.add_argument("--include-1m", action="store_true")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if args.include_500k:
        sizes.append(500_000)
    if args.include_1m:
        sizes.append(1_000_000)

    db_path = _build_temp_db()
    print(f"Using isolated temp DB: {db_path} (never the real data/app.db)")

    company_ids, t_seed = _time_it(f"seed {SYNTHETIC_COMPANY_COUNT} companies (once, shared across sizes)",
                                    lambda: _seed_companies(SYNTHETIC_COMPANY_COUNT))

    results = []
    for n in sorted(set(sizes)):
        results.append(run_benchmark(n, company_ids))

    print("\n=== Summary ===")
    for r in results:
        print(f"N={r['n']:<8} import={r['import_seconds']:.2f}s profiles={r['profile_seconds']:.2f}s "
              f"lookup={r['lookup_seconds']:.3f}s decision={r['decision_seconds']:.3f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
