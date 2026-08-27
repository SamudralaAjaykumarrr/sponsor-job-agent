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

import logging
from dataclasses import dataclass, field

from app import apply_settings, config
from app.applications import rate_limit
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications.executor import ExecutorDisabledError, queue_application
from app.applications.models import ExecutionMode
from app.applications.repo import get_active_execution_for_job
from app.db import db_session
from app.jobs_repo import get_job
from app.models import ApplicationState

logger = logging.getLogger("applications.scheduler")


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


def _alignment_meets_threshold(job_id: int) -> bool:
    from app.resume_optimizer.repo import get_quality_report_for_job

    report_row = get_quality_report_for_job(job_id)
    if report_row is None:
        return True
    score = report_row["report"].get("internal_alignment_score")
    if score is None:
        return True
    return score >= config.MIN_ALIGNMENT_FOR_AUTO_PREPARE


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
    preferences = apply_settings.get_settings()

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

        # Apply/Automation Settings V1 section 6: narrows WHICH already-
        # eligible jobs get auto-prepared -- never a substitute for, or
        # weakening of, the FULL_TIME/sponsorship hard gates already
        # enforced by eligibility.enters_queue above. Default (empty)
        # preferences match every job, unchanged from before this setting
        # existed.
        prefs_ok, _prefs_reason = apply_settings.job_matches_preferences(job, preferences)
        if not prefs_ok:
            result.skipped_not_eligible += 1
            continue

        if not _alignment_meets_threshold(job_id):
            # CLAUDE.md one-click-agent section 13: alignment must pass a
            # repository-defined threshold before automatic preparation. A
            # job with no quality report yet (resume optimization hasn't run
            # for it) is never blocked here -- absence of a score is not
            # evidence of a bad one, matching this project's existing
            # "never reject for missing info" pattern (compensation/salary).
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
            # Global condition -- every remaining candidate would raise the
            # exact same error, so stopping the whole cycle here is correct,
            # not a per-job-isolation violation.
            result.errors.append(str(exc))
            break
        except Exception as exc:  # noqa: BLE001 -- one job's unexpected failure (e.g. a transient
            # DB hiccup, a bug in duplicate/rate-limit lookups) must never abort
            # auto-prepare for every OTHER candidate in this cycle -- matches
            # this project's existing per-job isolation principle
            # (app.agent.orchestrator._run_resume_stage's per-job try/except,
            # app.applications.worker's per-execution isolation). A real gap:
            # previously only ExecutorDisabledError was caught here.
            logger.exception("auto-prepare: queue_application failed unexpectedly for job_id=%s", job_id)
            result.errors.append(f"job {job_id}: {exc}")
            continue
        if queue_result.queued:
            result.queued += 1
        else:
            result.skipped_not_eligible += 1

    return result
