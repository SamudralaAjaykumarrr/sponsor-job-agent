#!/usr/bin/env python3
"""Phase 15 release-candidate benchmark (CLAUDE.md Phase 15 sections 44, 65).

Reuses existing benchmark infrastructure rather than duplicating it:
  - scripts/registry_benchmark.py::run_benchmark()          -- registry/due queries
  - scripts/resume_optimizer_benchmark.py::run_benchmark()  -- JD analysis, evidence
                                                                 matching, resume generation
                                                                 (ATS parse validation is
                                                                 already inside optimize_resume(),
                                                                 timed separately below too)

Adds NEW measurements this phase's acceptance specifically asked for that no prior
benchmark script covered:
  - standalone ATS parse validation timing (isolated from resume generation)
  - unified-dashboard query performance at synthetic job-table scale (list_jobs,
    compute_pipeline_summary, the now-batched variant/quality/execution lookups, and a
    full HTTP GET / round trip via FastAPI's TestClient for an honest end-to-end number)
  - application queue claim performance (app.applications.queue.claim_execution_batch),
    mirroring scripts/worker_benchmark.py's leasing-benchmark style (including an
    8-worker contention check for zero duplicate claims) for the discovery queue's
    application-layer counterpart

Everything runs against an ISOLATED TEMP SQLite database (never data/app.db) and a temp
output directory (never the real output/) -- every synthetic row uses provider name
"benchmark-fixture" (matching every prior benchmark script's convention), which can never
collide with a real provider.

This benchmark measures ENGINEERING PERFORMANCE ONLY: query/render/service latency and
throughput on a single machine. It proves nothing about network-polling capacity (no HTTP
requests are made to any real provider), nothing about interview/application/hiring
outcomes, and nothing about multi-machine/distributed behavior (that is covered instead by
the separate, already-existing multi-worker contention tests and
tests/test_postgres_leasing.py / tests/test_multi_machine_simulation.py).

Usage:
    python3 scripts/phase15_release_benchmark.py [--sizes 1000,10000,50000] [--include-100k]
"""

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registry_benchmark  # noqa: E402
import resume_optimizer_benchmark  # noqa: E402

SYNTHETIC_PROVIDER = "benchmark-fixture"


# ---------------------------------------------------------------------------
# Unified dashboard query benchmark
# ---------------------------------------------------------------------------

def _build_dashboard_temp_env():
    """Isolated temp DB/output/candidate dirs -- same pattern every prior
    benchmark script uses. Deliberately its own setup (not
    resume_optimizer_benchmark._build_temp_env(), which also writes a
    synthetic profile and redirects the optimizer's OUTPUT_DIR -- this
    benchmark never generates real resumes, so that extra setup would be
    dead weight here)."""
    import app.config as config
    import app.db as db

    tmp_dir = Path(tempfile.mkdtemp(prefix="phase15_dashboard_benchmark_"))
    config.DB_PATH = tmp_dir / "benchmark.db"
    db.DB_PATH = tmp_dir / "benchmark.db"

    import app.jobs_repo as jobs_repo
    jobs_repo.db_session = db.db_session

    db.init_db()
    return tmp_dir


def _bulk_seed_dashboard_jobs(n: int) -> list[int]:
    """Direct bulk insert via app.jobs_repo's own column list/coercion
    helper (reused, not duplicated) -- one INSERT per row through
    app.jobs_repo.insert_job() would mean N separate transactions, far too
    slow at 50k-100k rows (same reasoning scripts/registry_benchmark.py and
    scripts/worker_benchmark.py already documented for their own bulk
    seeding). Realistic variety (employment type / sponsorship status /
    priority tier / freshness) so filtered queries exercise real index
    selectivity, not one uniform value."""
    from app.db import db_session
    from app.jobs_repo import _COLUMNS, _coerce_sql_value
    from app.models import (
        ApplicationMode, ApplicationState, EmploymentType, FreshnessTier, Job,
        PriorityTier, SponsorshipStatus, WorkArrangement, utcnow,
    )

    employment_cycle = [EmploymentType.FULL_TIME, EmploymentType.FULL_TIME, EmploymentType.CONTRACT, EmploymentType.UNKNOWN]
    sponsorship_cycle = [SponsorshipStatus.CONFIRMED_SPONSOR, SponsorshipStatus.LIKELY_SPONSOR,
                          SponsorshipStatus.UNKNOWN, SponsorshipStatus.NO_SPONSORSHIP]
    arrangement_cycle = [WorkArrangement.REMOTE, WorkArrangement.HYBRID, WorkArrangement.ONSITE]
    priority_cycle = [PriorityTier.P1_REMOTE_CONFIRMED, PriorityTier.P2_REMOTE_LIKELY,
                       PriorityTier.P5_ONSITE_CONFIRMED, PriorityTier.NOT_ELIGIBLE]
    now = utcnow()

    ids = []
    batch_rows = []
    batch_size = 5000
    placeholders = ", ".join("?" for _ in _COLUMNS)
    cols_sql = ", ".join(_COLUMNS)

    with db_session() as conn:
        for i in range(n):
            job = Job(
                title=f"Benchmark Software Engineer {i}", company=f"BenchmarkFixtureCo{i}",
                location="Remote (US)", description="Synthetic benchmark job posting. " * 5,
                provider=SYNTHETIC_PROVIDER, external_job_id=f"bench-{i}",
                employment_type=employment_cycle[i % len(employment_cycle)].value,
                work_arrangement=arrangement_cycle[i % len(arrangement_cycle)],
                sponsorship_status=sponsorship_cycle[i % len(sponsorship_cycle)],
                freshness_tier=FreshnessTier.MODERATE,
                priority_tier=priority_cycle[i % len(priority_cycle)],
                priority_score=float(i % 150),
                application_state=ApplicationState.ANALYZED,
                mode=ApplicationMode.ASSIST,
                first_seen_at=now, last_seen_at=now, created_at=now, updated_at=now,
            )
            values = [_coerce_sql_value(getattr(job, col)) for col in _COLUMNS]
            batch_rows.append(values)
            if len(batch_rows) >= batch_size:
                conn.executemany(f"INSERT INTO jobs ({cols_sql}) VALUES ({placeholders})", batch_rows)
                batch_rows = []
        if batch_rows:
            conn.executemany(f"INSERT INTO jobs ({cols_sql}) VALUES ({placeholders})", batch_rows)

        rows = conn.execute("SELECT id FROM jobs WHERE provider = ? ORDER BY id ASC", (SYNTHETIC_PROVIDER,)).fetchall()
        ids = [r["id"] for r in rows]
    return ids


def _seed_active_executions(job_ids: list[int], count: int) -> None:
    """A realistic-sized subset of jobs have an active application
    execution -- via the sanctioned app.applications.repo.create_execution()
    (not a raw insert here; this subset is small enough that per-row
    overhead is fine, and reusing the real function avoids hand-duplicating
    application_executions' column defaults)."""
    from app.applications.models import ExecutionMode
    from app.applications.repo import create_execution

    for jid in job_ids[:count]:
        create_execution(jid, provider="mock_ats", mode=ExecutionMode.ASSIST.value)


def run_dashboard_benchmark(size: int) -> dict:
    from app.applications import repo as applications_repo
    from app.jobs_repo import list_jobs
    from app.pipeline_dashboard import compute_pipeline_summary, is_actionable
    from app.resume_optimizer import repo as resume_optimizer_repo

    t0 = time.perf_counter()
    job_ids = _bulk_seed_dashboard_jobs(size)
    seed_seconds = time.perf_counter() - t0

    executions_seeded = min(2000, max(1, size // 20))
    t0 = time.perf_counter()
    _seed_active_executions(job_ids, executions_seeded)
    seed_executions_seconds = time.perf_counter() - t0

    # Unbounded "all jobs" query -- exactly what the dashboard's summary
    # cards use today (app.main.dashboard()).
    t0 = time.perf_counter()
    all_jobs = list_jobs({})
    list_all_jobs_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    summary = compute_pipeline_summary(all_jobs)
    summary_compute_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    filtered_jobs = list_jobs({"work_arrangement": "REMOTE"})
    list_filtered_jobs_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    actionable = [j for j in filtered_jobs if is_actionable(j)]
    actionable_filter_seconds = time.perf_counter() - t0

    actionable_ids = [j.id for j in actionable if j.id is not None]

    t0 = time.perf_counter()
    quality_by_job = resume_optimizer_repo.get_quality_reports_for_jobs(actionable_ids)
    quality_lookup_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    variant_by_job = resume_optimizer_repo.get_current_variants_for_jobs(actionable_ids)
    variant_lookup_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    execution_by_job = applications_repo.get_active_executions_for_jobs(actionable_ids)
    execution_lookup_seconds = time.perf_counter() - t0

    # Full end-to-end HTTP round trip: proves the dashboard route stays
    # functionally correct (200 OK, real HTML body) at this scale, not just
    # that the underlying queries are individually fast.
    from fastapi.testclient import TestClient

    import app.main as main_module

    client = TestClient(main_module.app)
    t0 = time.perf_counter()
    response = client.get("/")
    full_request_seconds = time.perf_counter() - t0

    return {
        "size": size,
        "jobs_inserted": len(job_ids),
        "executions_seeded": executions_seeded,
        "seed_jobs_seconds": round(seed_seconds, 4),
        "seed_executions_seconds": round(seed_executions_seconds, 4),
        "list_all_jobs_unbounded_seconds": round(list_all_jobs_seconds, 4),
        "list_all_jobs_rows": len(all_jobs),
        "summary_compute_seconds": round(summary_compute_seconds, 4),
        "summary_jobs_discovered": summary.get("jobs_discovered"),
        "list_filtered_jobs_seconds": round(list_filtered_jobs_seconds, 4),
        "list_filtered_jobs_rows": len(filtered_jobs),
        "actionable_filter_seconds": round(actionable_filter_seconds, 4),
        "actionable_rows": len(actionable),
        "quality_lookup_seconds": round(quality_lookup_seconds, 4),
        "variant_lookup_seconds": round(variant_lookup_seconds, 4),
        "execution_lookup_seconds_batched": round(execution_lookup_seconds, 4),
        "full_http_get_dashboard_seconds": round(full_request_seconds, 4),
        "full_http_get_dashboard_status": response.status_code,
        "full_http_get_dashboard_body_bytes": len(response.content),
    }


# ---------------------------------------------------------------------------
# Application queue claim benchmark
# ---------------------------------------------------------------------------

def _bulk_seed_application_executions(n: int) -> None:
    """Raw bulk insert (mirroring scripts/worker_benchmark.py's
    _bulk_seed_company_registry) -- application_executions has no enforced
    FK to jobs (see CLAUDE.md Phase 6's "only two real FK constraints"
    note), so this queue-claim-only benchmark doesn't need real job rows,
    matching worker_benchmark.py's equivalent choice not to seed real
    provider/company data for its own leasing benchmark."""
    from app.db import db_session
    from app.jobs_repo import utcnow

    now = utcnow()
    with db_session() as conn:
        conn.executemany(
            """INSERT INTO application_executions
                 (execution_id, job_id, provider, mode, status, active, started_at, created_at, updated_at)
               VALUES (?, ?, ?, 'ASSIST', 'QUEUED', 1, ?, ?, ?)""",
            [(f"bench-exec-{i}", i, SYNTHETIC_PROVIDER, now, now, now) for i in range(n)],
        )


def run_application_queue_benchmark(size: int) -> dict:
    from app.applications.queue import claim_execution_batch

    t0 = time.perf_counter()
    _bulk_seed_application_executions(size)
    seed_seconds = time.perf_counter() - t0

    t0 = time.perf_counter()
    claimed = claim_execution_batch(worker_id="bench-single", limit=50, lease_seconds=300)
    single_claim_seconds = time.perf_counter() - t0

    results: dict[str, list[str]] = {}
    lock = threading.Lock()

    def drain(worker_id: str) -> None:
        local = []
        while True:
            batch = claim_execution_batch(worker_id=worker_id, limit=200, lease_seconds=300)
            if not batch:
                break
            local.extend(r["execution_id"] for r in batch)
        with lock:
            results[worker_id] = local

    t0 = time.perf_counter()
    threads = [threading.Thread(target=drain, args=(f"bench-worker-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    contention_seconds = time.perf_counter() - t0

    all_claimed = [e for v in results.values() for e in v]
    duplicate_claims = len(all_claimed) - len(set(all_claimed))

    return {
        "size": size,
        "seed_seconds": round(seed_seconds, 4),
        "single_claim_50_seconds": round(single_claim_seconds, 4),
        "single_claim_returned": len(claimed),
        "eight_worker_contention_drain_seconds": round(contention_seconds, 4),
        "eight_worker_total_claimed": len(all_claimed),
        "eight_worker_duplicate_claims": duplicate_claims,
    }


# ---------------------------------------------------------------------------
# Standalone ATS parse validation timing
# ---------------------------------------------------------------------------

def run_ats_parse_benchmark(n: int) -> dict:
    from app.resume.docx_writer import write_docx
    from app.resume.generator import EducationBlock, ExperienceBlock, ProjectBlock, ResumeContent
    from app.resume.pdf_writer import write_pdf
    from app.resume.txt_writer import write_txt
    from app.resume_optimizer import ats_parse

    resume = ResumeContent(
        full_name="Benchmark Candidate", email="benchmark.candidate@example.invalid",
        phone="555-000-0000", location="Austin, TX", linkedin_url="", github_url="", portfolio_url="",
        summary="Software engineer with verified backend experience.",
        skills_ordered=["python", "fastapi", "postgresql", "docker"],
        experience=[ExperienceBlock(company="BenchmarkFixtureCo", title="Backend Software Engineer",
                                     start_date="2022-06", end_date="Present", location="Remote",
                                     bullets=["Built and maintained REST APIs in Python using FastAPI."])],
        projects=[ProjectBlock(name="Benchmark CLI", description="A CLI tool.", bullets=["Implemented in Python."], url="")],
        education=[EducationBlock(school="Benchmark State University", degree="B.S.",
                                   field_of_study="Computer Science", graduation_date="2022-05")],
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="phase15_ats_parse_benchmark_"))
    docx_path = write_docx(resume, tmp_dir / "resume.docx")
    pdf_path = write_pdf(resume, tmp_dir / "resume.pdf")
    txt_path = write_txt(resume, tmp_dir / "resume.txt")

    durations = []
    for _ in range(n):
        t0 = time.perf_counter()
        ats_parse.validate_all(docx_path, pdf_path, txt_path, resume)
        durations.append(time.perf_counter() - t0)

    durations.sort()
    return {
        "runs": n,
        "mean_ms": round(1000 * sum(durations) / n, 3),
        "p50_ms": round(1000 * durations[n // 2], 3),
        "max_ms": round(1000 * durations[-1], 3),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000,50000")
    parser.add_argument("--include-100k", action="store_true")
    parser.add_argument("--resume-jobs", type=int, default=25, help="jobs for the JD-analysis/evidence/generation benchmark")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if args.include_100k:
        sizes.append(100_000)

    print("=" * 78)
    print("Phase 15 release-candidate benchmark")
    print("Every dataset is synthetic, in an isolated temp SQLite file -- never data/app.db.")
    print("Measures ENGINEERING PERFORMANCE ONLY. No claim about interview/hiring outcomes.")
    print("=" * 78)

    print("\n--- 1. Registry / due queries (reusing scripts/registry_benchmark.py) ---")
    for size in sizes:
        registry_benchmark._build_temp_db()
        result = registry_benchmark.run_benchmark(size)
        print(f"  size={size}: {result}")

    print("\n--- 2. JD analysis / evidence matching / resume generation "
          "(reusing scripts/resume_optimizer_benchmark.py) ---")
    tmp_dir = resume_optimizer_benchmark._build_temp_env()
    print(f"  temp env: {tmp_dir}")
    resume_results = resume_optimizer_benchmark.run_benchmark(args.resume_jobs)
    for stage, stats in resume_results.items():
        print(f"  {stage:16s} {stats}")

    print("\n--- 3. Standalone ATS parse validation (DOCX+PDF+TXT together, per call) ---")
    ats_result = run_ats_parse_benchmark(200)
    print(f"  {ats_result}")

    print("\n--- 4. Unified dashboard query (new this phase) ---")
    for size in sizes:
        _build_dashboard_temp_env()
        result = run_dashboard_benchmark(size)
        for k, v in result.items():
            print(f"  {k}: {v}")
        print()

    print("--- 5. Application queue claim (new this phase; mirrors scripts/worker_benchmark.py) ---")
    for size in sizes:
        _build_dashboard_temp_env()
        result = run_application_queue_benchmark(size)
        for k, v in result.items():
            print(f"  {k}: {v}")
        assert result["eight_worker_duplicate_claims"] == 0, "BENCHMARK INVARIANT VIOLATED: duplicate claims detected"
        print()

    print("=" * 78)
    print("This benchmark proves: these specific queries/operations complete in the times")
    print("shown, on this machine, against synthetic data of this shape, on this date.")
    print("It does NOT prove: real network-polling throughput, real multi-machine behavior")
    print("(see tests/test_postgres_leasing.py / tests/test_multi_machine_simulation.py for")
    print("that), or anything about interview/application/hiring outcomes for any candidate.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
