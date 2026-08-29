"""Sponsorship intelligence operational CLI (CLAUDE.md Phase 7 section 34).

    python -m app.sponsorship.cli import-uscis file.csv [--dataset-version V] [--resume]
    python -m app.sponsorship.cli import-dol-lca file.csv [--dataset-version V] [--resume]
    python -m app.sponsorship.cli datasets
    python -m app.sponsorship.cli stats
    python -m app.sponsorship.cli company "Company Name"
    python -m app.sponsorship.cli doctor
    python -m app.sponsorship.cli review-queue

Every command initializes the real app database (app.config.DB_PATH) via
app.db.init_db() first -- migrations are additive and idempotent, so this is
always safe to run."""

import argparse
import sys

from app.db import init_db


def _cmd_import_uscis(args: argparse.Namespace) -> int:
    from app.sponsorship.importers import import_uscis_employer_data, recompute_profiles_for_dataset

    result = import_uscis_employer_data(
        args.path, dataset_version=args.dataset_version, resume=args.resume,
    )
    _print_import_result(result)
    recompute_profiles_for_dataset(result.dataset_id)
    return 1 if result.rows_invalid and not args.allow_invalid else 0


def _cmd_import_dol_lca(args: argparse.Namespace) -> int:
    from app.sponsorship.importers import import_dol_lca_data, recompute_profiles_for_dataset

    result = import_dol_lca_data(
        args.path, dataset_version=args.dataset_version, resume=args.resume,
    )
    _print_import_result(result)
    recompute_profiles_for_dataset(result.dataset_id)
    return 1 if result.rows_invalid and not args.allow_invalid else 0


def _print_import_result(result) -> None:
    d = result.as_dict()
    print(f"dataset: {d['dataset_name']} (id={d['dataset_id']})")
    print(f"  rows_total:              {d['rows_total']}")
    print(f"  rows_created:            {d['rows_created']}")
    print(f"  rows_skipped_duplicate:  {d['rows_skipped_duplicate']}")
    print(f"  rows_invalid:            {d['rows_invalid']}")
    print(f"  companies_matched:       {d['companies_matched']}")
    print(f"  companies_ambiguous:     {d['companies_ambiguous']}")
    print(f"  companies_unmatched:     {d['companies_unmatched']}")
    for err in d["errors"][:50]:
        print(f"  ERROR: {err}")
    if len(d["errors"]) > 50:
        print(f"  ... and {len(d['errors']) - 50} more errors")


def _cmd_import_public_source(args: argparse.Namespace) -> int:
    from app.sponsorship.public_source_importer import import_h1bdata_snapshot
    from app.sponsorship.importers import recompute_profiles_for_dataset

    result = import_h1bdata_snapshot(
        args.path, args.employer, dataset_version=args.dataset_version,
    )
    d = result.as_dict()
    print(f"dataset: {d['dataset_name']} (id={d['dataset_id']}) employer_query='{d['employer_query']}'")
    print(f"  rows_total:                        {d['rows_total']}")
    print(f"  rows_created:                      {d['rows_created']}")
    print(f"  rows_skipped_duplicate:            {d['rows_skipped_duplicate']}")
    print(f"  rows_rejected_employer_mismatch:   {d['rows_rejected_employer_mismatch']}")
    if d["rejected_employer_names"]:
        print(f"    rejected names: {', '.join(d['rejected_employer_names'][:10])}")
    print(f"  company_id:                        {d['company_id']} (matched_via={d['company_match_via'] or '-'})")
    if result.company_id is not None:
        recompute_profiles_for_dataset(result.dataset_id)
    return 0


def _cmd_seed_aliases(_: argparse.Namespace) -> int:
    from app.sponsorship.aliases import seed_known_aliases

    result = seed_known_aliases()
    print(f"aliases applied: {result.applied}")
    if result.skipped_no_company:
        print(f"  skipped (no registry company): {result.skipped_no_company}")
    if result.skipped_ambiguous_company:
        print(f"  skipped (ambiguous registry company): {result.skipped_ambiguous_company}")
    return 0


def _cmd_seed_identities(_: argparse.Namespace) -> int:
    from app.sponsorship.registry_backfill import seed_missing_employer_identities

    result = seed_missing_employer_identities()
    print(f"companies created: {result.created}")
    if result.already_present:
        print(f"  already present: {result.already_present}")
    return 0


def _cmd_coverage(_: argparse.Namespace) -> int:
    from app.sponsorship.coverage import coverage_snapshot

    snap = coverage_snapshot()
    print("Sponsorship evidence coverage (real discovered employers only):")
    print(f"  employers_total:                {snap['employers_total']}")
    print(f"  employers_matched_to_evidence:  {snap['employers_matched_to_evidence']}")
    print(f"  employers_unmatched:            {snap['employers_unmatched']}")
    print(f"  employers_ambiguous:            {snap['employers_ambiguous']}")
    print(f"  identity_reviews_pending:       {snap['identity_reviews_pending']}")
    print(f"  jobs_total:                     {snap['jobs_total']}")
    print(f"  jobs_confirmed_sponsor:         {snap['jobs_confirmed_sponsor']}")
    print(f"  jobs_likely_sponsor:            {snap['jobs_likely_sponsor']}")
    print(f"  jobs_unknown:                   {snap['jobs_unknown']}")
    print(f"  jobs_no_sponsorship:            {snap['jobs_no_sponsorship']}")
    if snap["unmatched_employer_names"]:
        print(f"  unmatched: {snap['unmatched_employer_names']}")
    if snap["ambiguous_employer_names"]:
        print(f"  ambiguous: {snap['ambiguous_employer_names']}")
    return 0


def _real_job_ids(args: argparse.Namespace) -> list[int]:
    """--job-ids if given, else every real (non-fixture, non-Acme-Corp) job id."""
    if args.job_ids:
        return list(args.job_ids)
    from app.db import db_session

    with db_session() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE is_test_fixture = 0 AND company != 'Acme Corp' ORDER BY id"
        ).fetchall()
    return [r["id"] for r in rows]


def _cmd_refresh_jobs(args: argparse.Namespace) -> int:
    from app.sponsorship.refresh import refresh_job_sponsorship

    changed = 0
    for job_id in _real_job_ids(args):
        if job_id in args.exclude:
            print(f"  job={job_id}: excluded, skipped")
            continue
        outcome = refresh_job_sponsorship(job_id)
        marker = "CHANGED" if outcome.changed else "unchanged"
        print(f"  job={job_id}: {outcome.previous_status.value} -> {outcome.new_status.value} ({marker})")
        if outcome.changed:
            changed += 1
    print(f"{changed} job(s) changed status")
    return 0


def _cmd_datasets(_: argparse.Namespace) -> int:
    from app.sponsorship.datasets import list_datasets

    for d in list_datasets():
        print(f"  id={d['id']:<4} {d['dataset_name']:<30} version={d['dataset_version'] or '-':<10} "
              f"fy={d['fiscal_year'] or '-':<6} status={d['status']:<10} records={d['record_count']}")
    return 0


def _cmd_stats(_: argparse.Namespace) -> int:
    from app.sponsorship.evidence import count_companies_with_recent_h1b_history, count_evidence

    print("Sponsorship evidence snapshot:")
    print(f"  total_evidence_records:            {count_evidence()}")
    print(f"  companies_with_recent_h1b_history:  {count_companies_with_recent_h1b_history()}")
    return 0


def _cmd_company(args: argparse.Namespace) -> int:
    from app.registry.normalize import normalize_company_name
    from app.registry import store as registry_store
    from app.sponsorship.profile import get_or_compute_profile

    normalized = normalize_company_name(args.name)
    candidates = registry_store.list_companies(limit=5, search=normalized)
    matches = [c for c in candidates if c.normalized_name == normalized] or candidates
    if not matches:
        print(f"no registry company found matching '{args.name}'")
        return 1
    company = matches[0]
    profile = get_or_compute_profile(company.id)
    print(f"Company: {company.display_name} (id={company.id}, domain={company.primary_domain or '-'})")
    print("HISTORICAL EVIDENCE -- NOT A GUARANTEE FOR ANY CURRENT ROLE")
    print(f"  historical_strength:       {profile.historical_strength.value}")
    print(f"  years_with_h1b_activity:   {profile.years_with_h1b_activity}")
    print(f"  most_recent_fiscal_year:   {profile.most_recent_fiscal_year}")
    print(f"  recent_filing_count:       {profile.recent_filing_count}")
    print(f"  historical_filing_count:   {profile.historical_filing_count}")
    print(f"  continuity_years:          {profile.continuity_years}")
    print(f"  trend:                     {profile.trend}")
    print(f"  recent_states:             {', '.join(profile.recent_states) or '-'}")
    print(f"  history_score:             {profile.history_score}")
    for reason in profile.history_reasons:
        print(f"    - {reason}")
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    from app.sponsorship.doctor import run_doctor

    report = run_doctor()
    print(f"Sponsorship doctor: {report.serious_count} serious issue(s), {report.warning_count} warning(s)")
    for issue in report.issues:
        print(f"  [{issue.severity.upper()}] {issue.check}: {issue.detail}")
    return 1 if report.serious_count else 0


def _cmd_review_queue(args: argparse.Namespace) -> int:
    from app.sponsorship.review_queue import build_review_queue

    items = build_review_queue(limit=args.limit)
    for item in items:
        print(f"  job={item.job_id:<6} {item.company:<24} {item.title:<32} "
              f"match={item.technical_match_score:<5} strength={item.historical_strength:<14} "
              f"missing='{item.missing_confirmation}'")
    print(f"{len(items)} job(s) in review queue")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.sponsorship.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_uscis = sub.add_parser("import-uscis", help="import a USCIS H-1B Employer Data Hub CSV")
    p_uscis.add_argument("path")
    p_uscis.add_argument("--dataset-version", default="")
    p_uscis.add_argument("--resume", action="store_true")
    p_uscis.add_argument("--allow-invalid", action="store_true")
    p_uscis.set_defaults(func=_cmd_import_uscis)

    p_lca = sub.add_parser("import-dol-lca", help="import a DOL OFLC LCA disclosure CSV")
    p_lca.add_argument("path")
    p_lca.add_argument("--dataset-version", default="")
    p_lca.add_argument("--resume", action="store_true")
    p_lca.add_argument("--allow-invalid", action="store_true")
    p_lca.set_defaults(func=_cmd_import_dol_lca)

    p_public = sub.add_parser(
        "import-public-source",
        help="import an already-downloaded h1bdata.info employer-search HTML snapshot",
    )
    p_public.add_argument("path")
    p_public.add_argument("--employer", required=True, help="exact legal-entity name the snapshot was searched for")
    p_public.add_argument("--dataset-version", default="")
    p_public.set_defaults(func=_cmd_import_public_source)

    p_seed_aliases = sub.add_parser("seed-aliases", help="load the verified employer alias seed file")
    p_seed_aliases.set_defaults(func=_cmd_seed_aliases)

    p_seed_identities = sub.add_parser(
        "seed-identities", help="load the verified employer registry-identity seed file",
    )
    p_seed_identities.set_defaults(func=_cmd_seed_identities)

    p_coverage = sub.add_parser("coverage", help="print sponsorship-evidence coverage metrics")
    p_coverage.set_defaults(func=_cmd_coverage)

    p_refresh = sub.add_parser(
        "refresh-jobs", help="recompute sponsorship_status for jobs using current evidence (never touches application_state)",
    )
    p_refresh.add_argument("--job-ids", type=int, nargs="*", default=[], help="specific job ids (default: every real job)")
    p_refresh.add_argument("--exclude", type=int, nargs="*", default=[], help="job ids to skip entirely")
    p_refresh.set_defaults(func=_cmd_refresh_jobs)

    p_datasets = sub.add_parser("datasets", help="list imported datasets")
    p_datasets.set_defaults(func=_cmd_datasets)

    p_stats = sub.add_parser("stats", help="print evidence/company summary stats")
    p_stats.set_defaults(func=_cmd_stats)

    p_company = sub.add_parser("company", help="print a company's historical sponsorship profile")
    p_company.add_argument("name")
    p_company.set_defaults(func=_cmd_company)

    p_doctor = sub.add_parser("doctor", help="run the sponsorship integrity checker")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_review = sub.add_parser("review-queue", help="print the sponsorship review queue")
    p_review.add_argument("--limit", type=int, default=50)
    p_review.set_defaults(func=_cmd_review_queue)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
