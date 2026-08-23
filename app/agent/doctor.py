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
    _check_auto_prepare_without_safety_gates(report)
    with db_session() as conn:
        _check_stopped_but_workers_leaking(conn, report)
    return report
