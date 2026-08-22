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
