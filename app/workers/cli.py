"""Worker fleet operational CLI.

    python -m app.workers.cli run [--shard-index N] [--shard-count N] [--once]
    python -m app.workers.cli status
    python -m app.workers.cli attempts [--limit N] [--status S] [--worker ID]
    python -m app.workers.cli dead-letter [--requeue ID]

Every command initializes the real app database (app.config.DB_PATH) via
app.db.init_db() first -- migrations are additive and idempotent, so this is
always safe to run. See docs/fleet-operations.md."""

import argparse
import sys

from app.db import init_db


def _cmd_run(args: argparse.Namespace) -> int:
    from app import config
    from app.workers.runner import Worker

    if config.STRUCTURED_LOGGING_ENABLED:
        from app.observability.logging_config import configure_structured_logging

        configure_structured_logging()

    worker = Worker(shard_index=args.shard_index, shard_count=args.shard_count, single_cycle=args.once)
    worker.install_signal_handlers()
    worker.run()
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    from app.workers import repo as workers_repo
    from app.workers import leasing

    workers = workers_repo.list_workers()
    print(f"Workers ({len(workers)}):")
    for w in workers:
        print(
            f"  {w['worker_id']:<32} status={w['status']:<9} shard={w['shard_index']}/{w['shard_count']} "
            f"heartbeat={w['last_heartbeat_at']} portals={w['portals_processed']} jobs={w['jobs_processed']} "
            f"errors={w['errors']} current={w['current_portal_type'] or '-'}:{w['current_portal_id'] or '-'}"
        )
    print(f"\nActive leases: poll={leasing.count_active_poll_leases()} verification={leasing.count_active_verification_leases()}")

    from app.workers.metrics import fleet_snapshot

    snap = fleet_snapshot()
    print("\nFleet metrics:")
    for k, v in snap.items():
        print(f"  {k}: {v}")
    return 0


def _cmd_attempts(args: argparse.Namespace) -> int:
    from app.workers import repo as workers_repo

    attempts = workers_repo.list_recent_attempts(limit=args.limit, status=args.status or "", worker_id=args.worker or "")
    print(f"Recent attempts ({len(attempts)}):")
    for a in attempts:
        print(
            f"  [{a['started_at']}] {a['queue']:<12} {a['provider']:<16} portal={a['portal_type']}:{a['portal_id']} "
            f"status={a['status']:<18} jobs_new={a['jobs_new']} error={a['error_type'] or '-'} worker={a['worker_id']}"
        )
    return 0


def _cmd_dead_letter(args: argparse.Namespace) -> int:
    from app.workers import dead_letter

    if args.requeue is not None:
        ok = dead_letter.requeue(args.requeue)
        print(f"requeue dead_letter id={args.requeue}: {'OK' if ok else 'not found / already resolved'}")
        return 0 if ok else 1

    entries = dead_letter.list_dead_letters(limit=args.limit)
    print(f"Dead letters ({len(entries)}):")
    for e in entries:
        print(
            f"  id={e['id']} {e['portal_type']}:{e['portal_id']} provider={e['provider']} "
            f"attempts={e['attempt_count']} reason={e['reason']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.workers.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a worker process (claims poll + verification work until stopped)")
    p_run.add_argument("--shard-index", type=int, default=None, help="overrides REGISTRY_SHARD_INDEX for this process")
    p_run.add_argument("--shard-count", type=int, default=None, help="overrides REGISTRY_SHARD_COUNT for this process")
    p_run.add_argument("--once", action="store_true", help="run exactly one bounded cycle then exit (for scripting/tests)")
    p_run.set_defaults(func=_cmd_run)

    p_status = sub.add_parser("status", help="show worker fleet + queue + fleet metrics snapshot")
    p_status.set_defaults(func=_cmd_status)

    p_attempts = sub.add_parser("attempts", help="show recent poll/verification attempt history")
    p_attempts.add_argument("--limit", type=int, default=50)
    p_attempts.add_argument("--status", default=None)
    p_attempts.add_argument("--worker", default=None)
    p_attempts.set_defaults(func=_cmd_attempts)

    p_dl = sub.add_parser("dead-letter", help="list dead-lettered work items, or requeue one by id")
    p_dl.add_argument("--limit", type=int, default=100)
    p_dl.add_argument("--requeue", type=int, default=None, metavar="ID")
    p_dl.set_defaults(func=_cmd_dead_letter)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
