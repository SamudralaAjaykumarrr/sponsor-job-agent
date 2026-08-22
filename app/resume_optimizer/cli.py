"""CLAUDE.md Phase 14 section 65:

  python -m app.resume_optimizer.cli analyze JOB_ID
  python -m app.resume_optimizer.cli generate JOB_ID
  python -m app.resume_optimizer.cli report JOB_ID
  python -m app.resume_optimizer.cli doctor

Mirrors app.applications.cli / app.registry.cli's argparse-based structure.
"""

import argparse
import json
import sys


def cmd_analyze(job_id: int) -> int:
    from app.jobs_repo import get_job
    from app.resume_optimizer.fingerprint import compute_jd_fingerprint
    from app.resume_optimizer.jd_analysis import analyze_jd
    from app.resume_optimizer import repo

    job = get_job(job_id)
    if job is None:
        print(f"job {job_id} not found", file=sys.stderr)
        return 1
    fingerprint = compute_jd_fingerprint(job.title, job.company, job.description)
    analysis = analyze_jd(job.title, job.description)
    repo.save_jd_analysis(job_id, fingerprint, analysis)
    print(json.dumps({
        "job_id": job_id, "jd_fingerprint": fingerprint,
        "required_years": analysis.required_years, "domain_signals": analysis.domain_signals,
        "responsibilities": analysis.responsibilities, "education_requirements": analysis.education_requirements,
        "certification_requirements": analysis.certification_requirements,
        "requirement_count": len(analysis.requirements),
    }, indent=2))
    return 0


def cmd_generate(job_id: int, force: bool) -> int:
    from app.resume_optimizer.optimizer import optimize_resume

    result = optimize_resume(job_id, force=force)
    print(json.dumps({"variant_id": result.variant_id, "status": result.status, "created": result.created, "reason": result.reason}, indent=2))
    return 0 if result.status in ("READY",) or not result.created else 1


def cmd_report(job_id: int) -> int:
    from app.resume_optimizer import repo

    report = repo.get_quality_report_for_job(job_id)
    if report is None:
        print(f"no quality report for job {job_id} -- run 'generate' first", file=sys.stderr)
        return 1
    print(json.dumps(report["report"], indent=2))
    return 0


def cmd_doctor() -> int:
    from app.resume_optimizer.doctor import run_doctor

    report = run_doctor()
    print(json.dumps(report.as_dict(), indent=2))
    return 1 if report.serious_count > 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.resume_optimizer.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("job_id", type=int)

    p_generate = sub.add_parser("generate")
    p_generate.add_argument("job_id", type=int)
    p_generate.add_argument("--force", action="store_true")

    p_report = sub.add_parser("report")
    p_report.add_argument("job_id", type=int)

    sub.add_parser("doctor")

    args = parser.parse_args(argv)

    from app.db import init_db

    init_db()

    if args.command == "analyze":
        return cmd_analyze(args.job_id)
    if args.command == "generate":
        return cmd_generate(args.job_id, args.force)
    if args.command == "report":
        return cmd_report(args.job_id)
    if args.command == "doctor":
        return cmd_doctor()
    return 1


if __name__ == "__main__":
    sys.exit(main())
