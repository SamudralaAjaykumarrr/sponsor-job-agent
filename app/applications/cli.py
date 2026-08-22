"""Application executor operational CLI (CLAUDE.md Phase 8 section 57).

    python -m app.applications.cli prepare JOB_ID [--mode ASSIST|AUTO_PERMITTED]
    python -m app.applications.cli validate JOB_ID
    python -m app.applications.cli queue JOB_ID [--mode ASSIST|AUTO_PERMITTED]
    python -m app.applications.cli status
    python -m app.applications.cli reconcile EXECUTION_ID --resolution R [--confirmation-id ID]
    python -m app.applications.cli doctor

Every command initializes the real app database first -- migrations are
additive/idempotent, safe to run every time."""

import argparse
import sys

from app.db import init_db


def _cmd_prepare(args: argparse.Namespace) -> int:
    """"prepare" == queue + run once synchronously, for a single job. Useful
    for manual/CLI operation without standing up a worker loop."""
    from app.applications.executor import process_execution, queue_application

    result = queue_application(args.job_id, mode=args.mode)
    print(f"queue: queued={result.queued} execution_id={result.execution_id} reason={result.reason}")
    if not result.queued or result.execution_id is None:
        return 1
    execution = process_execution(result.execution_id)
    print(f"status: {execution['status']}")
    if execution.get("requires_user_action"):
        print(f"  requires_user_action: {execution.get('user_action_reason')}")
    if execution.get("confirmation_id"):
        print(f"  confirmation_id: {execution['confirmation_id']}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from app.applications.eligibility import evaluate_executor_eligibility
    from app.jobs_repo import get_job

    job = get_job(args.job_id)
    if job is None:
        print(f"job {args.job_id} not found")
        return 1
    result = evaluate_executor_eligibility(job)
    print(f"job {args.job_id}: enters_queue={result.enters_queue} auto_submit_eligible={result.auto_submit_eligible} "
          f"employment_type={result.employment_type.value}")
    for r in result.reasons:
        print(f"  - {r}")
    return 0 if result.enters_queue else 1


def _cmd_queue(args: argparse.Namespace) -> int:
    from app.applications.executor import queue_application

    result = queue_application(args.job_id, mode=args.mode)
    print(f"queued={result.queued} execution_id={result.execution_id} reason={result.reason}")
    return 0 if result.queued else 1


def _cmd_status(_: argparse.Namespace) -> int:
    from app.applications import metrics

    m = metrics.collect()
    for k, v in m.items():
        print(f"  {k}: {v}")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from app.applications.reconcile import reconcile_execution

    result = reconcile_execution(
        args.execution_id, args.resolution, confirmation_id=args.confirmation_id or "", note=args.note or "",
    )
    print(f"ok={result.ok} detail={result.detail}")
    return 0 if result.ok else 1


def _cmd_doctor(_: argparse.Namespace) -> int:
    from app.applications.doctor import run_doctor

    report = run_doctor()
    print(f"Application doctor: {report.serious_count} serious issue(s), {report.warning_count} warning(s)")
    for issue in report.issues:
        print(f"  [{issue.severity.upper()}] {issue.check}: {issue.detail}")
    return 1 if report.serious_count else 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from app.applications.worker import main as worker_main

    argv = ["run"]
    if args.once:
        argv.append("--once")
    if args.workers != 1:
        argv.extend(["--workers", str(args.workers)])
    if args.drain:
        argv.append("--drain")
    return worker_main(argv)


def _cmd_drain(args: argparse.Namespace) -> int:
    from app.applications.worker_admin import request_drain, resume_from_drain

    if args.resume:
        ok = resume_from_drain(args.worker_id)
    else:
        ok = request_drain(args.worker_id)
    print(f"{'resumed' if args.resume else 'drain requested'}: {ok}")
    return 0 if ok else 1


def _cmd_scheduler(args: argparse.Namespace) -> int:
    from app.applications.scheduler import run_cycle

    result = run_cycle(limit=args.limit)
    print(f"scheduler cycle: {result.as_dict()}")
    return 0


def _cmd_reconcile_worker(args: argparse.Namespace) -> int:
    from app.applications.reconcile_worker import run_pass

    result = run_pass(limit=args.limit)
    print(f"reconciliation pass: {result.as_dict()}")
    return 0


def _cmd_budget(_: argparse.Namespace) -> int:
    from app.applications.budget import collect

    for k, v in collect().as_dict().items():
        print(f"  {k}: {v}")
    return 0


def _cmd_capability_matrix(_: argparse.Namespace) -> int:
    from app.applications.capability_matrix import render_text

    print(render_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.applications.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="queue + synchronously run one job's application executor once")
    p_prepare.add_argument("job_id", type=int)
    p_prepare.add_argument("--mode", default="ASSIST", choices=["ASSIST", "AUTO_PERMITTED"])
    p_prepare.set_defaults(func=_cmd_prepare)

    p_validate = sub.add_parser("validate", help="print the executor eligibility gate result for a job")
    p_validate.add_argument("job_id", type=int)
    p_validate.set_defaults(func=_cmd_validate)

    p_queue = sub.add_parser("queue", help="queue a job for execution (does not run it)")
    p_queue.add_argument("job_id", type=int)
    p_queue.add_argument("--mode", default="ASSIST", choices=["ASSIST", "AUTO_PERMITTED"])
    p_queue.set_defaults(func=_cmd_queue)

    p_status = sub.add_parser("status", help="print application executor metrics")
    p_status.set_defaults(func=_cmd_status)

    p_reconcile = sub.add_parser("reconcile", help="reconcile a SUBMISSION_STATUS_UNKNOWN execution")
    p_reconcile.add_argument("execution_id")
    p_reconcile.add_argument("--resolution", required=True,
                              choices=["confirmed_applied", "confirmed_not_submitted", "manual_applied"])
    p_reconcile.add_argument("--confirmation-id", default="")
    p_reconcile.add_argument("--note", default="")
    p_reconcile.set_defaults(func=_cmd_reconcile)

    p_doctor = sub.add_parser("doctor", help="run the application executor integrity checker")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_worker = sub.add_parser("worker", help="run the Phase 9 application-executor worker daemon")
    p_worker.add_argument("--once", action="store_true")
    p_worker.add_argument("--workers", type=int, default=1)
    p_worker.add_argument("--drain", action="store_true")
    p_worker.set_defaults(func=_cmd_worker)

    p_drain = sub.add_parser("drain", help="request (or resume from) drain mode for a running application worker")
    p_drain.add_argument("worker_id")
    p_drain.add_argument("--resume", action="store_true")
    p_drain.set_defaults(func=_cmd_drain)

    p_scheduler = sub.add_parser("scheduler", help="run one application-scheduler cycle (auto-prepare)")
    p_scheduler.add_argument("--limit", type=int, default=None)
    p_scheduler.set_defaults(func=_cmd_scheduler)

    p_reconcile_worker = sub.add_parser("reconcile-worker",
                                         help="run one automated reconciliation evidence pass")
    p_reconcile_worker.add_argument("--limit", type=int, default=50)
    p_reconcile_worker.set_defaults(func=_cmd_reconcile_worker)

    p_budget = sub.add_parser("budget", help="print today's application budget accounting")
    p_budget.set_defaults(func=_cmd_budget)

    p_matrix = sub.add_parser("capability-matrix", help="print the truthful provider capability matrix")
    p_matrix.set_defaults(func=_cmd_capability_matrix)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
