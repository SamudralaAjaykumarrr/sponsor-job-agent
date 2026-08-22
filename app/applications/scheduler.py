"""Continuous application-executor scheduler (CLAUDE.md Phase 9 section 38).
When APPLICATION_AUTO_PREPARE_ENABLED, finds eligible READY_TO_APPLY jobs and
queues them for the application worker fleet -- within budget, respecting
rate limits, never bypassing app.applications.eligibility's gates (this
module never calls anything below queue_application; it has no direct access
to form/submit machinery at all).

CLAUDE.md Phase 9 section 37: AUTO_PREPARE (this module auto-queuing) is
independent of AUTO_SUBMIT_ENABLED (submit permission) -- this scheduler
runs, and queues jobs in ASSIST mode, purely under
APPLICATION_AUTO_PREPARE_ENABLED; it only ever queues in AUTO_PERMITTED mode
for a specific job when AUTO_SUBMIT_ENABLED is ALSO true AND that job's own
eligibility already clears auto_submit_eligible."""

from dataclasses import dataclass, field

from app import config
from app.applications import rate_limit
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications.executor import ExecutorDisabledError, queue_application
from app.applications.models import ExecutionMode
from app.applications.repo import get_active_execution_for_job
from app.db import db_session
from app.jobs_repo import get_job
from app.models import ApplicationState


@dataclass
class SchedulerCycleResult:
    candidates_considered: int = 0
    queued: int = 0
    skipped_active_execution: int = 0
    skipped_rate_limited: int = 0
    skipped_not_eligible: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidates_considered": self.candidates_considered, "queued": self.queued,
            "skipped_active_execution": self.skipped_active_execution,
            "skipped_rate_limited": self.skipped_rate_limited,
            "skipped_not_eligible": self.skipped_not_eligible, "errors": self.errors,
        }


def _candidate_job_ids(limit: int) -> list[int]:
    """CLAUDE.md Phase 9 section 9: queue ordering favors fresh job, strong
    match, CONFIRMED_SPONSOR, FULL_TIME, Remote > Hybrid > Onsite -- exactly
    what Phase 1-2's existing priority_tier/priority_score scoring already
    encodes (P1_REMOTE_CONFIRMED highest ... P6_ONSITE_LIKELY lowest), so
    this reuses that scoring rather than inventing a second ranking."""
    with db_session() as conn:
        rows = conn.execute(
            """SELECT id FROM jobs
               WHERE application_state = ?
               ORDER BY priority_score DESC, first_seen_at DESC
               LIMIT ?""",
            (ApplicationState.READY_TO_APPLY.value, limit),
        ).fetchall()
        return [r["id"] for r in rows]


def run_cycle(*, limit: int | None = None) -> SchedulerCycleResult:
    result = SchedulerCycleResult()
    if not config.APPLICATION_AUTO_PREPARE_ENABLED:
        return result
    if not config.APPLICATION_EXECUTOR_ENABLED:
        return result

    max_queue = limit if limit is not None else config.APPLICATION_SCHEDULER_MAX_QUEUE_PER_CYCLE
    # Overfetch candidates since some will already have an active execution
    # or be blocked by rate limits -- bounded, never unbounded.
    candidate_ids = _candidate_job_ids(max_queue * 5)
    result.candidates_considered = len(candidate_ids)

    for job_id in candidate_ids:
        if result.queued >= max_queue:
            break
        if get_active_execution_for_job(job_id) is not None:
            result.skipped_active_execution += 1
            continue

        job = get_job(job_id)
        if job is None:
            continue

        eligibility = evaluate_executor_eligibility(job)
        if not eligibility.enters_queue:
            result.skipped_not_eligible += 1
            continue

        rl = rate_limit.check_rate_limits(job.company)
        if not rl.allowed:
            result.skipped_rate_limited += 1
            continue

        mode = (
            ExecutionMode.AUTO_PERMITTED
            if (config.AUTO_SUBMIT_ENABLED and eligibility.auto_submit_eligible)
            else ExecutionMode.ASSIST
        )
        try:
            queue_result = queue_application(job_id, mode=mode.value)
        except ExecutorDisabledError as exc:
            result.errors.append(str(exc))
            break
        if queue_result.queued:
            result.queued += 1
        else:
            result.skipped_not_eligible += 1

    return result
