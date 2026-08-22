#!/usr/bin/env python3
"""Deterministic resume-optimizer benchmark -- CLAUDE.md Phase 14 section 90.

Benchmarks JD parsing, evidence matching, quality-diagnostic calculation,
resume generation (DOCX/PDF/TXT), and ATS parse validation against a
synthetic candidate profile and a batch of synthetic job descriptions.
Everything runs against an ISOLATED TEMP SQLite database and a temp output
directory -- never the real data/app.db, candidate_data/, or output/ --
matching the exact "benchmark-fixture" isolation convention already
established by scripts/sponsorship_benchmark.py, scripts/registry_benchmark.py,
scripts/worker_benchmark.py, scripts/phase6_scale_benchmark.py.

This benchmark measures ENGINEERING PERFORMANCE ONLY (parse/match/generate/
validate latency and throughput). It makes no claim about, and must never be
read as predicting, interview or hiring outcomes for any real candidate.

Usage:
    python3 scripts/resume_optimizer_benchmark.py [--jobs 50]
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SYNTHETIC_COMPANY = "BenchmarkFixtureCo"
SYNTHETIC_PROVIDER = "benchmark-fixture"

_JD_TEMPLATES = [
    (
        "Backend Software Engineer",
        "Required: Python, FastAPI, PostgreSQL, REST APIs, Docker, CI/CD, AWS. "
        "Preferred: Kubernetes, Kafka, GraphQL. Bachelor's degree in Computer Science required. "
        "Responsibilities include building REST APIs, distributed systems, and CI/CD pipelines. "
        "This role offers H-1B sponsorship.",
    ),
    (
        "Platform Engineer",
        "Required: Docker, Kubernetes, Terraform, AWS, CI/CD, monitoring. Preferred: Go, Prometheus. "
        "5+ years of experience required. Responsibilities include cloud deployment and production support. "
        "Visa sponsorship available.",
    ),
    (
        "Java Backend Engineer",
        "Required: Java, Spring Boot, Kafka, Kubernetes, 7+ years experience. "
        "AWS Certified Solutions Architect required. PhD in Computer Science required. "
        "This role offers H-1B sponsorship.",
    ),
    (
        "Python Developer",
        "Required: Python, Django, PostgreSQL, unit testing, pytest, Git. Preferred: React, GraphQL. "
        "Responsibilities include debugging, code review, and testing. Sponsorship not available.",
    ),
    (
        "Data Platform Engineer",
        "Required: Python, ETL, Spark, data pipelines, SQL. Preferred: machine learning, Hadoop. "
        "Bachelor's degree required. 3+ years of experience required. H1B sponsorship offered.",
    ),
]


def _synthetic_profile():
    from app.candidate.schema import CandidateProfile

    return CandidateProfile.model_validate({
        "contact": {
            "full_name": "Benchmark Candidate", "email": "benchmark.candidate@example.invalid",
            "phone": "555-000-0000", "city": "Austin", "state": "TX",
            "linkedin_url": "", "github_url": "", "portfolio_url": "",
        },
        "employment": [{
            "company": SYNTHETIC_COMPANY, "title": "Backend Software Engineer",
            "start_date": "2022-06", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Built and maintained REST APIs in Python using FastAPI serving 2M requests/day.",
                "Designed PostgreSQL schema migrations for a multi-tenant billing system.",
                "Automated deployment pipelines with Docker and GitHub Actions CI/CD.",
                "Deployed services to AWS and monitored production systems.",
            ],
            "skills_used": ["python", "fastapi", "rest api", "postgresql", "docker", "ci/cd", "aws", "git"],
        }],
        "skills": [
            "python", "fastapi", "django", "rest api", "postgresql", "docker", "ci/cd", "git",
            "aws", "sql", "unit testing", "pytest",
        ],
        "projects": [{
            "name": "Benchmark CLI", "description": "A command-line benchmarking tool.",
            "verified_bullets": ["Implemented a SQLite-backed CLI in Python with pytest test coverage."],
            "skills_used": ["python", "sqlite", "pytest"], "url": "",
        }],
        "education": [{
            "school": "Benchmark State University", "degree": "B.S.",
            "field_of_study": "Computer Science", "graduation_date": "2022-05",
        }],
        "work_authorization": {
            "current_status": "F-1 OPT", "requires_sponsorship": True,
            "sponsorship_type_needed": "H-1B", "years_us_experience": 3,
        },
        "preferences": {
            "relocation_open": False, "preferred_locations": ["Remote"], "salary_min_usd": 110000,
            "salary_preference_notes": "", "work_arrangement_priority": ["REMOTE", "HYBRID", "ONSITE"],
        },
        "standard_answers": {
            "years_of_experience": 3, "notice_period": "2 weeks", "willing_to_relocate": False,
            "requires_sponsorship_answer": "Yes", "veteran_status": "no", "disability_status": "no",
            "race_ethnicity": "prefer not to say", "gender": "prefer not to say",
        },
    })


def _build_temp_env():
    import app.config as config
    import app.db as db

    tmp_dir = Path(tempfile.mkdtemp(prefix="resume_optimizer_benchmark_"))
    (tmp_dir / "candidate_data").mkdir()
    (tmp_dir / "output").mkdir()
    config.DB_PATH = tmp_dir / "benchmark.db"
    config.CANDIDATE_DIR = tmp_dir / "candidate_data"
    config.OUTPUT_DIR = tmp_dir / "output"
    db.DB_PATH = tmp_dir / "benchmark.db"

    import app.candidate.profile as profile_mod
    profile_mod.CANDIDATE_DIR = tmp_dir / "candidate_data"
    profile_mod.PROFILE_PATH = tmp_dir / "candidate_data" / "profile.json"

    import app.jobs_repo as jobs_repo
    jobs_repo.db_session = db.db_session

    import app.pipeline as pipeline
    pipeline.OUTPUT_DIR = tmp_dir / "output"

    import app.resume_optimizer.optimizer as optimizer_module
    optimizer_module.OUTPUT_DIR = tmp_dir / "output"

    db.init_db()
    profile_mod.save_profile(_synthetic_profile())
    return tmp_dir


def _insert_synthetic_jobs(n: int) -> list[int]:
    from app.jobs_repo import insert_job
    from app.models import ApplicationMode, Job

    ids = []
    for i in range(n):
        title, desc = _JD_TEMPLATES[i % len(_JD_TEMPLATES)]
        job = Job(
            title=title, company=f"{SYNTHETIC_COMPANY}{i}", location="Remote", description=desc,
            provider=SYNTHETIC_PROVIDER, external_job_id=f"benchmark-{i}", mode=ApplicationMode.ASSIST,
        )
        ids.append(insert_job(job))
    return ids


def run_benchmark(n_jobs: int) -> dict:
    from app.candidate.profile import load_profile
    from app.resume_optimizer.jd_analysis import analyze_jd
    from app.resume_optimizer.evidence import build_evidence_graph
    from app.resume_optimizer.matching import match_requirements
    from app.resume_optimizer.optimizer import optimize_resume
    from app.jobs_repo import get_job

    job_ids = _insert_synthetic_jobs(n_jobs)
    profile = load_profile()
    graph = build_evidence_graph(profile)  # not timed -- graph is per-profile, not per-job

    timings = {"jd_parse": [], "evidence_match": [], "full_optimize": []}

    for job_id in job_ids:
        job = get_job(job_id)

        t0 = time.perf_counter()
        analysis = analyze_jd(job.title, job.description)
        t1 = time.perf_counter()
        timings["jd_parse"].append(t1 - t0)

        t2 = time.perf_counter()
        match_requirements(analysis.requirements, graph, profile)
        t3 = time.perf_counter()
        timings["evidence_match"].append(t3 - t2)

        t4 = time.perf_counter()
        optimize_resume(job_id)
        t5 = time.perf_counter()
        timings["full_optimize"].append(t5 - t4)

    def summarize(values: list[float]) -> dict:
        values_sorted = sorted(values)
        n = len(values_sorted)
        return {
            "count": n,
            "mean_ms": round(1000 * sum(values_sorted) / n, 2) if n else 0.0,
            "p50_ms": round(1000 * values_sorted[n // 2], 2) if n else 0.0,
            "p95_ms": round(1000 * values_sorted[int(n * 0.95) if n > 1 else 0], 2) if n else 0.0,
            "max_ms": round(1000 * values_sorted[-1], 2) if n else 0.0,
        }

    return {stage: summarize(vals) for stage, vals in timings.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=50)
    args = parser.parse_args()

    tmp_dir = _build_temp_env()
    print(f"Benchmark isolated temp DB/output: {tmp_dir}")
    print(f"Synthetic provider name: {SYNTHETIC_PROVIDER!r} (never collides with a real provider)")
    print(f"Running against {args.jobs} synthetic jobs...\n")

    results = run_benchmark(args.jobs)
    for stage, stats in results.items():
        print(f"{stage:16s} n={stats['count']:4d}  mean={stats['mean_ms']:8.2f}ms  "
              f"p50={stats['p50_ms']:8.2f}ms  p95={stats['p95_ms']:8.2f}ms  max={stats['max_ms']:8.2f}ms")

    print(
        "\nNOTE: this benchmark measures engineering latency/throughput only. "
        "It makes no claim about, and must never be read as predicting, interview or hiring outcomes."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
