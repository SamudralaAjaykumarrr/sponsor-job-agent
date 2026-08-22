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


def _cmd_browser_capability_matrix(_: argparse.Namespace) -> int:
    from app.applications.browser_capability_matrix import render_text

    print(render_text())
    return 0


def _cmd_workday_tenants(_: argparse.Namespace) -> int:
    from app.applications.workday_tenant import render_tenant_matrix

    print(render_tenant_matrix())
    return 0


def _cmd_workday_stability(_: argparse.Namespace) -> int:
    """CLAUDE.md Phase 12 sections 54, 68: 'consistent X/Y, variable X/Y'
    per tenant -- never a single collapsed 'Workday supported' claim."""
    from app.applications.workday_tenant import stability_report

    report = stability_report()
    if not report:
        print("No repeated Workday attempts recorded yet.")
        return 0
    for s in report:
        print(f"  {s.tenant}/{s.site}: {s.stability.value}  "
              f"consistent={s.consistent_count}/{s.attempt_count}  variable={s.variable_count}/{s.attempt_count}")
    return 0


def _cmd_capability_evidence(args: argparse.Namespace) -> int:
    from app.applications import capability_evidence

    rows = capability_evidence.list_evidence(provider=args.provider or None)
    if not rows:
        print("No capability evidence recorded yet.")
        return 0
    for row in rows:
        stale = capability_evidence.is_stale(row)
        age = capability_evidence.evidence_age_days(row["observed_at"])
        marker = " [STALE]" if stale else ""
        print(f"  {row['provider']:<16} {row['capability']:<20} {row['verification_type']:<12} "
              f"age={age:.1f}d{marker} observed_at={row['observed_at']}")
    return 0


def _print_session(session: dict) -> None:
    if not session:
        print("  (no session)")
        return
    print(f"  session_id={session.get('session_id')} status={session.get('status')} "
          f"job_id={session.get('job_id')} provider={session.get('provider')}")
    if session.get("user_action_reason"):
        print(f"    user_action_reason: {session['user_action_reason']}")


def _cmd_browser_start(args: argparse.Namespace) -> int:
    from app.applications.browser_assist import start_session

    result = start_session(args.execution_id)
    print(f"created={result.get('created')} reason={result.get('reason', '')}")
    _print_session(result.get("session") or {})
    return 0 if result.get("created") else 1


def _cmd_browser_resume(args: argparse.Namespace) -> int:
    from app.applications.browser_assist import resume_session

    result = resume_session(args.session_id)
    print(f"ok={result.get('ok')} detail={result.get('detail', '')}")
    _print_session(result.get("session") or {})
    return 0 if result.get("ok") else 1


def _cmd_browser_continue(args: argparse.Namespace) -> int:
    from app.applications.browser_assist import mark_user_action_complete

    result = mark_user_action_complete(args.session_id)
    print(f"ok={result.get('ok')} detail={result.get('detail', '')}")
    _print_session(result.get("session") or {})
    return 0 if result.get("ok") else 1


def _cmd_browser_close(args: argparse.Namespace) -> int:
    from app.applications.browser_assist import close_session

    session = close_session(args.session_id, reason=args.reason or "closed via CLI")
    _print_session(session or {})
    return 0


def _cmd_browser_reconcile(args: argparse.Namespace) -> int:
    from app.applications.browser_assist import attempt_user_submit_reconciliation

    result = attempt_user_submit_reconciliation(args.session_id)
    print(f"ok={result.get('ok')} detail={result.get('detail', '')}")
    _print_session(result.get("session") or {})
    return 0 if result.get("ok") else 1


def _cmd_browser_status(_: argparse.Namespace) -> int:
    from app.applications import browser_session

    summary = browser_session.summarize()
    for k, v in summary.as_dict().items():
        print(f"  {k}: {v}")
    return 0


def _cmd_browser_list(args: argparse.Namespace) -> int:
    from app.applications import browser_session

    for row in browser_session.list_sessions(status=args.status, limit=args.limit):
        print(f"  {row['session_id']}  job={row['job_id']}  status={row['status']}  "
              f"provider={row['provider']}  updated_at={row['updated_at']}")
    return 0


def _cmd_provider_health(_: argparse.Namespace) -> int:
    from app.applications import provider_health

    print(provider_health.render_health_report())
    return 0


def _cmd_canary(args: argparse.Namespace) -> int:
    from app.applications import canary

    try:
        result = canary.run_and_record_canary(args.url, provider=args.provider or "")
    except canary.CanaryUnavailable as exc:
        print(f"canary unavailable: {exc}")
        return 1
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0 if result.get("ok") else 1


def _cmd_job_identity(args: argparse.Namespace) -> int:
    from app.applications import job_identity

    for row in job_identity.list_verifications(job_id=args.job_id, limit=args.limit):
        print(f"  job={row['job_id']} stage={row['stage']} result={row['result']} "
              f"matched={row['signals_matched']} mismatched={row['signals_mismatched']} "
              f"verified_at={row['verified_at']}")
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

    p_bstart = sub.add_parser("browser-start", help="open a visible browser-assist session for an execution")
    p_bstart.add_argument("execution_id")
    p_bstart.set_defaults(func=_cmd_browser_start)

    p_bresume = sub.add_parser("browser-resume", help="resume a browser-assist session")
    p_bresume.add_argument("session_id")
    p_bresume.set_defaults(func=_cmd_browser_resume)

    p_bcontinue = sub.add_parser("browser-continue",
                                  help="mark a PAUSED_* browser-assist session's user action complete and continue")
    p_bcontinue.add_argument("session_id")
    p_bcontinue.set_defaults(func=_cmd_browser_continue)

    p_bclose = sub.add_parser("browser-close", help="close a browser-assist session")
    p_bclose.add_argument("session_id")
    p_bclose.add_argument("--reason", default="")
    p_bclose.set_defaults(func=_cmd_browser_close)

    p_breconcile = sub.add_parser("browser-reconcile",
                                   help="check the current page for confirmation evidence after a manual submit")
    p_breconcile.add_argument("session_id")
    p_breconcile.set_defaults(func=_cmd_browser_reconcile)

    p_bstatus = sub.add_parser("browser-status", help="print browser-assist session bucket counts")
    p_bstatus.set_defaults(func=_cmd_browser_status)

    p_blist = sub.add_parser("browser-list", help="list browser-assist sessions")
    p_blist.add_argument("--status", default=None)
    p_blist.add_argument("--limit", type=int, default=50)
    p_blist.set_defaults(func=_cmd_browser_list)

    p_bmatrix = sub.add_parser("browser-capability-matrix",
                                help="print the truthful browser-assist capability matrix")
    p_bmatrix.set_defaults(func=_cmd_browser_capability_matrix)

    p_workday = sub.add_parser("workday-tenants", help="print the per-tenant/site Workday observation matrix")
    p_workday.set_defaults(func=_cmd_workday_tenants)

    p_workday_stability = sub.add_parser(
        "workday-stability", help="print per-tenant Workday stability (STABLE/VARIABLE/UNVERIFIED/STALE) from "
                                   "repeated attempts",
    )
    p_workday_stability.set_defaults(func=_cmd_workday_stability)

    p_evidence = sub.add_parser("capability-evidence", help="print dated capability evidence, flagging stale rows")
    p_evidence.add_argument("--provider", default="")
    p_evidence.set_defaults(func=_cmd_capability_evidence)

    p_provider_health = sub.add_parser("provider-health",
                                        help="print application/browser-assist provider health per (provider, "
                                             "tenant, site)")
    p_provider_health.set_defaults(func=_cmd_provider_health)

    p_canary = sub.add_parser("canary", help="run one safe, read-only application-flow canary against a public URL "
                                              "(never fills PII, never uploads, never submits)")
    p_canary.add_argument("url")
    p_canary.add_argument("--provider", default="")
    p_canary.set_defaults(func=_cmd_canary)

    p_job_identity = sub.add_parser("job-identity", help="print job-identity verification evidence")
    p_job_identity.add_argument("--job-id", type=int, default=None, dest="job_id")
    p_job_identity.add_argument("--limit", type=int, default=50)
    p_job_identity.set_defaults(func=_cmd_job_identity)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
