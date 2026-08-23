"""Read-only doctor for the one-click agent orchestrator (CLAUDE.md
one-click-agent section 47). Same contract as every other subsystem doctor
in this project (app.registry.doctor / app.sponsorship.doctor /
app.applications.doctor / app.resume_optimizer.doctor): reports problems,
never auto-repairs."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import config


@dataclass
class Issue:
    severity: str
    check: str
    detail: str


@dataclass
class DoctorReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def serious_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "serious")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def as_dict(self) -> dict:
        return {
            "serious_count": self.serious_count, "warning_count": self.warning_count,
            "issues": [{"severity": i.severity, "check": i.check, "detail": i.detail} for i in self.issues],
        }


def _check_running_but_loop_absent(report: DoctorReport) -> None:
    """CLAUDE.md one-click-agent section 47: agent_run_state.actual_state
    says RUNNING but the orchestrator's own background task has died --
    never true under normal operation (a crashed cycle is caught and logged
    inside the loop itself, see AgentOrchestrator._loop), so this catches a
    genuine regression rather than an expected state."""
    from app.agent.orchestrator import orchestrator
    from app.agent.run_state import get_run_state

    run = get_run_state()
    if run["actual_state"] != "RUNNING":
        return
    task = orchestrator._task
    if task is None or task.done():
        report.issues.append(Issue("serious", "agent_running_but_loop_absent",
                                    "agent_run_state.actual_state is RUNNING but the orchestrator's background "
                                    "loop task is not alive."))


def _check_running_without_valid_lease(report: DoctorReport) -> None:
    """autonomous-core-v3 hardening: actual_state RUNNING must always be
    backed by a live (unexpired, owned) single-orchestrator-guarantee lease
    -- see app.agent.run_state's lease section and
    AGENT_ORCHESTRATOR_LEASE_SECONDS. Should never trigger under normal
    operation (the loop only ever sets RUNNING after acquiring the lease,
    and renews it every cycle) -- catches a genuine regression, e.g. a future
    change that sets actual_state without going through the lease-guarded
    path in app.agent.orchestrator._loop."""
    from app.agent.run_state import get_run_state

    run = get_run_state()
    if run["actual_state"] != "RUNNING":
        return
    lease_expires_at = run.get("lease_expires_at")
    instance_id = run.get("instance_id") or ""
    if not instance_id or not lease_expires_at:
        report.issues.append(Issue("serious", "agent_running_without_lease",
                                    "agent_run_state.actual_state is RUNNING but no instance currently holds "
                                    "the single-orchestrator-guarantee lease."))
        return
    try:
        expires = datetime.fromisoformat(lease_expires_at)
    except ValueError:
        return
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        report.issues.append(Issue("serious", "agent_running_with_expired_lease",
                                    f"agent_run_state.actual_state is RUNNING but instance {instance_id}'s "
                                    f"orchestrator lease expired at {lease_expires_at} and was never renewed."))


def _check_stopped_but_workers_leaking(conn, report: DoctorReport) -> None:
    """A STOPPED agent must never leave an application worker actively
    WORKING/STARTING with a fresh heartbeat -- app.agent.orchestrator.stop()
    always lets the in-flight ApplicationWorker.run() cycle finish (which
    itself heartbeats STOPPED) before flipping actual_state to STOPPED."""
    from app.agent.run_state import get_run_state
    from app.workers.models import WorkerStatus

    run = get_run_state()
    if run["actual_state"] != "STOPPED":
        return
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=max(60, config.APPLICATION_WORKER_HEARTBEAT_SECONDS * 4))
    ).isoformat()
    rows = conn.execute(
        "SELECT worker_id, status, last_heartbeat_at FROM workers "
        "WHERE status IN (?, ?) AND last_heartbeat_at >= ? AND capabilities LIKE '%APPLICATION%'",
        (WorkerStatus.WORKING.value, WorkerStatus.STARTING.value, cutoff),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "agent_stopped_but_worker_leaking",
                                    f"agent is STOPPED but application worker {r['worker_id']} last heartbeated "
                                    f"{r['last_heartbeat_at']} with status {r['status']} -- expected idle/stopped."))


_STARTUP_GRACE_SECONDS = 30


def _check_running_but_no_cycle_ever(report: DoctorReport) -> None:
    """CLAUDE.md production-v2 dashboard defect 1: RUNNING with
    last_cycle_started_at still null more than _STARTUP_GRACE_SECONDS after
    started_at is exactly the reported real defect ('Agent Status = RUNNING
    but Last cycle = never') -- a brief STARTING window is expected and
    excluded, a genuinely stuck-before-first-cycle loop is not."""
    from app.agent.run_state import get_run_state

    run = get_run_state()
    if run["actual_state"] != "RUNNING" or not run.get("started_at"):
        return
    if run.get("last_cycle_started_at"):
        return
    try:
        started = datetime.fromisoformat(run["started_at"])
    except ValueError:
        return
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - started).total_seconds()
    if age > _STARTUP_GRACE_SECONDS:
        report.issues.append(Issue(
            "serious", "agent_running_but_no_cycle_ever",
            f"agent has been RUNNING for {age:.0f}s but last_cycle_started_at is still null -- "
            "the first cycle should begin within seconds of START.",
        ))


def _check_stale_heartbeat(report: DoctorReport) -> None:
    """CLAUDE.md production-v2 section 5: RUNNING + stale/missing heartbeat
    must be detectable -- surfaced as a warning here (dashboard also shows
    it), never auto-restarted (this project cannot safely force-cancel a
    hung synchronous stage mid-network-call)."""
    from app.agent.run_state import get_run_state, heartbeat_age_seconds

    run = get_run_state()
    if run["actual_state"] != "RUNNING":
        return
    age = heartbeat_age_seconds()
    if age is not None and age > config.AGENT_HEARTBEAT_STALE_SECONDS:
        report.issues.append(Issue(
            "warning", "agent_heartbeat_stale",
            f"agent is RUNNING but heartbeat_at is {age:.0f}s old (threshold "
            f"{config.AGENT_HEARTBEAT_STALE_SECONDS}s) -- possible stuck cycle stage.",
        ))


def _check_needs_action_count_matches_queue(report: DoctorReport) -> None:
    """CLAUDE.md production-v2 dashboard defect 2: the summary card and the
    'Needs Your Action' list must never disagree -- both now derive from
    app.pipeline_dashboard's single _NEEDS_ACTION_QUERIES source, so this
    check is a live regression guard, not a fix in itself."""
    from app.pipeline_dashboard import build_needs_action_queue, count_needs_action

    total = count_needs_action()
    queue_len = len(build_needs_action_queue(limit=max(total, 25)))
    if total != queue_len:
        report.issues.append(Issue(
            "serious", "needs_action_count_mismatch",
            f"count_needs_action()={total} but build_needs_action_queue() returned {queue_len} items "
            "for the same (raised) limit -- these must always agree.",
        ))


def _check_test_fixture_not_in_real_dashboard(conn, report: DoctorReport) -> None:
    """CLAUDE.md production-v2 dashboard defect 6: a job marked
    is_test_fixture must never appear in a default (non-opt-in) real-mode
    query result -- this checks the actual list_jobs() default behavior
    rather than merely inspecting the flag's existence."""
    from app.jobs_repo import list_jobs

    test_rows = conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE is_test_fixture = 1").fetchone()["c"]
    if test_rows == 0:
        return
    default_jobs = list_jobs({})
    if any(j.is_test_fixture for j in default_jobs):
        report.issues.append(Issue(
            "serious", "test_fixture_in_real_dashboard",
            "list_jobs({}) (the default, real-mode query) returned at least one is_test_fixture=1 row.",
        ))


def _check_legacy_scheduler_and_orchestrator_both_running(report: DoctorReport) -> None:
    """CLAUDE.md production-v2 'CURRENT REAL DASHBOARD DEFECTS' item 3: the
    one-click orchestrator and the legacy discovery-only scheduler
    (app.agent.state/app.agent.scheduler) are deliberately decoupled
    (orchestrator no longer touches agent_state.set_enabled) so starting one
    never silently starts the other -- but a user can still explicitly turn
    the legacy toggle ON via /agent/toggle while the orchestrator is also
    RUNNING, which means two independent run_discovery_cycle() loops running
    concurrently. Not unsafe (discovery is dedup-safe), but wasteful and
    exactly the 'two competing agent controls' confusion this build fixes --
    flagged so it's visible, never silently allowed to look normal."""
    from app.agent import state as agent_state
    from app.agent.run_state import get_run_state

    run = get_run_state()
    if run["actual_state"] == "RUNNING" and agent_state.is_enabled():
        report.issues.append(Issue(
            "warning", "duplicate_discovery_loops",
            "Both the one-click agent (RUNNING) and the legacy discovery-only scheduler toggle "
            "are ON at the same time -- two independent discovery cycles are running. Turn the "
            "legacy toggle OFF; the one-click agent already covers discovery.",
        ))


def _check_auto_prepare_without_safety_gates(report: DoctorReport) -> None:
    """Static assertion (source inspection), mirroring app.applications.
    doctor's own '_check_no_browser_auto_submit_capability' pattern: the
    orchestrator's auto-prepare stage (app.applications.scheduler.run_cycle)
    must always re-derive full eligibility before queuing -- never trust a
    cached state as a substitute for the real gate."""
    import inspect

    from app.applications import scheduler as applications_scheduler

    src = inspect.getsource(applications_scheduler.run_cycle)
    if "evaluate_executor_eligibility" not in src:
        report.issues.append(Issue("serious", "auto_prepare_without_safety_gates",
                                    "app.applications.scheduler.run_cycle() no longer calls "
                                    "evaluate_executor_eligibility() -- auto-prepare must never queue a job "
                                    "without re-deriving the full eligibility gate."))


def run_doctor() -> DoctorReport:
    from app.db import db_session

    report = DoctorReport()
    _check_running_but_loop_absent(report)
    _check_running_but_no_cycle_ever(report)
    _check_running_without_valid_lease(report)
    _check_stale_heartbeat(report)
    _check_needs_action_count_matches_queue(report)
    _check_legacy_scheduler_and_orchestrator_both_running(report)
    _check_auto_prepare_without_safety_gates(report)
    with db_session() as conn:
        _check_stopped_but_workers_leaking(conn, report)
        _check_test_fixture_not_in_real_dashboard(conn, report)
    return report
