"""Registry operational CLI.

    python -m app.registry.cli import companies.csv [--dry-run] [--batch-size N]
    python -m app.registry.cli validate companies.csv
    python -m app.registry.cli stats
    python -m app.registry.cli export registry.jsonl [--format jsonl|json]
    python -m app.registry.cli doctor
    python -m app.registry.cli verify [--limit N] [--provider NAME]
    python -m app.registry.cli acquire seed.csv [--source-name NAME] [--no-verify]
    python -m app.registry.cli batches
    python -m app.registry.cli resume BATCH_ID [--no-verify]

Every command initializes the real app database (app.config.DB_PATH) via
app.db.init_db() first -- migrations are additive and idempotent, so this is
always safe to run."""

import argparse
import sys

from app.db import init_db


def _cmd_import(args: argparse.Namespace) -> int:
    from app.registry.importers import import_file

    summary = import_file(args.path, source_name=args.source_name, dry_run=args.dry_run, batch_size=args.batch_size)
    _print_import_summary(summary)
    return 1 if summary.rows_invalid and not args.allow_invalid else 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from app.registry.importers import import_file

    summary = import_file(args.path, source_name=args.path, dry_run=True, batch_size=args.batch_size)
    _print_import_summary(summary)
    return 1 if summary.rows_invalid else 0


def _print_import_summary(summary) -> None:
    d = summary.as_dict()
    print(f"source: {d['source_name']}{' (dry-run)' if d['dry_run'] else ''}")
    print(f"  rows_total:       {d['rows_total']}")
    print(f"  rows_created:     {d['rows_created']}")
    print(f"  rows_updated:     {d['rows_updated']}")
    print(f"  rows_skipped:     {d['rows_skipped']}")
    print(f"  rows_invalid:     {d['rows_invalid']}")
    print(f"  companies_created:{d['companies_created']}")
    for err in d["errors"][:50]:
        print(f"  ERROR: {err}")
    if len(d["errors"]) > 50:
        print(f"  ... and {len(d['errors']) - 50} more errors")


def _cmd_stats(_: argparse.Namespace) -> int:
    from app.registry.analytics import provider_breakdown, snapshot

    snap = snapshot()
    print("Registry snapshot:")
    for k, v in snap.items():
        print(f"  {k.capitalize()}: {v}")
    print("\nBy provider:")
    for row in provider_breakdown():
        print(f"  {row['provider']:<16} companies={row['companies']:<5} active={row['active_portals']:<5} "
              f"healthy={row['healthy_portals']:<5} jobs_seen={row['jobs_seen'] or 0:<6} error_rate={row['error_rate']}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from app.registry.export import export_json, export_jsonl

    if args.format == "json":
        n = export_json(args.path)
    else:
        n = export_jsonl(args.path)
    print(f"exported {n} portal record(s) to {args.path} ({args.format})")
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    from app.registry.doctor import run_doctor

    report = run_doctor()
    print(f"Registry doctor: {report.serious_count} serious issue(s), {report.warning_count} warning(s)")
    for issue in report.issues:
        print(f"  [{issue.severity.upper()}] {issue.check}: {issue.detail}")
    return 1 if report.serious_count else 0


def _cmd_acquire(args: argparse.Namespace) -> int:
    from app.registry.acquisition import run_acquisition_batch

    result = run_acquisition_batch(
        args.path, source_name=args.source_name, source_type=args.source_type,
        verify_new_candidates=not args.no_verify,
    )
    _print_batch_result(result)
    return 0 if result.status == "COMPLETED" else 1


def _cmd_batches(_: argparse.Namespace) -> int:
    from app.registry.acquisition import list_batches

    for b in list_batches():
        print(f"  id={b['id']:<4} status={b['status']:<10} source={b['source_name']:<24} "
              f"processed={b['records_processed']}/{b['records_total']} "
              f"companies={b['companies_created']} candidates={b['portal_candidates']} "
              f"verified={b['verified']} active={b['active']} quarantined={b['quarantined']} failed={b['failed']}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    from app.registry.acquisition import get_batch, run_acquisition_batch

    existing = get_batch(args.batch_id)
    if existing is None:
        print(f"no such batch id={args.batch_id}")
        return 1
    result = run_acquisition_batch(existing["path"], resume_batch_id=args.batch_id, verify_new_candidates=not args.no_verify)
    _print_batch_result(result)
    return 0 if result.status == "COMPLETED" else 1


def _print_batch_result(result) -> None:
    print(f"batch {result.batch_id}: {result.status}")
    print(f"  records_processed: {result.records_processed}/{result.records_total}")
    print(f"  companies_created: {result.companies_created}")
    print(f"  portal_candidates: {result.portal_candidates}")
    print(f"  verified:          {result.verified}")
    print(f"  active:            {result.active}")
    print(f"  quarantined:       {result.quarantined}")
    print(f"  failed:            {result.failed}")
    for err in result.errors[:50]:
        print(f"  ERROR: {err}")


def _cmd_verify(args: argparse.Namespace) -> int:
    from app.registry import lifecycle, store, sync
    from app.registry.verification import verify_portal

    portals = store.list_due_for_verification(limit=args.limit)
    if args.provider:
        portals = [p for p in portals if p.provider == args.provider]

    verified = failed = skipped = 0
    for portal in portals:
        company = store.get_company(portal.company_id)
        outcome = verify_portal(portal, company_display_name=company.display_name if company else "")
        lifecycle.apply_verification_outcome(portal.id, outcome)
        sync.sync_portal_to_operational_registry(portal.id)
        print(f"  portal {portal.id} ({portal.provider}/{portal.tenant_identifier}): {outcome.result.value} -- {outcome.detail}")
        if outcome.result.value == "VERIFIED":
            verified += 1
        elif outcome.result.value == "FAILED":
            failed += 1
        else:
            skipped += 1
    print(f"verify: {len(portals)} portal(s) checked -- verified={verified} failed={failed} other={skipped}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.registry.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="bulk import companies/portals from CSV/JSON/JSONL")
    p_import.add_argument("path")
    p_import.add_argument("--dry-run", action="store_true")
    p_import.add_argument("--batch-size", type=int, default=500)
    p_import.add_argument("--source-name", default=None)
    p_import.add_argument("--allow-invalid", action="store_true", help="exit 0 even if some rows were invalid")
    p_import.set_defaults(func=_cmd_import)

    p_validate = sub.add_parser("validate", help="dry-run import: report validation errors only, write nothing")
    p_validate.add_argument("path")
    p_validate.add_argument("--batch-size", type=int, default=500)
    p_validate.set_defaults(func=_cmd_validate)

    p_stats = sub.add_parser("stats", help="print real DB-derived registry snapshot + per-provider breakdown")
    p_stats.set_defaults(func=_cmd_stats)

    p_export = sub.add_parser("export", help="export the registry (no candidate data) as JSONL/JSON")
    p_export.add_argument("path")
    p_export.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    p_export.set_defaults(func=_cmd_export)

    p_doctor = sub.add_parser("doctor", help="run the registry integrity checker")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_verify = sub.add_parser("verify", help="run the live verification pipeline over due CANDIDATE/DISCOVERED portals")
    p_verify.add_argument("--limit", type=int, default=50)
    p_verify.add_argument("--provider", default=None)
    p_verify.set_defaults(func=_cmd_verify)

    p_acquire = sub.add_parser("acquire", help="run a resumable acquisition batch: seed dataset -> companies -> portal candidates -> verification")
    p_acquire.add_argument("path")
    p_acquire.add_argument("--source-name", default=None)
    p_acquire.add_argument("--source-type", default="CSV")
    p_acquire.add_argument("--no-verify", action="store_true", help="skip immediate verification of newly created candidates")
    p_acquire.set_defaults(func=_cmd_acquire)

    p_batches = sub.add_parser("batches", help="list acquisition batches and their progress")
    p_batches.set_defaults(func=_cmd_batches)

    p_resume = sub.add_parser("resume", help="resume an interrupted/failed acquisition batch by id")
    p_resume.add_argument("batch_id", type=int)
    p_resume.add_argument("--no-verify", action="store_true")
    p_resume.set_defaults(func=_cmd_resume)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
