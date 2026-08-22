"""Aggregation for the unified one-page dashboard (CLAUDE.md Phase 14
sections 44-55, 79). Deliberately separate from app.resume_optimizer (which
knows nothing about applications/browser sessions) and from
app.applications.repo (which knows nothing about resume diagnostics) --
this module is the cross-cutting read layer the dashboard route uses, built
from small, indexed, already-existing queries (CLAUDE.md section 55: never
a full-table scan or per-job recomputation on every page load)."""

from app.applications.browser_session import BrowserSessionStatus
from app.applications.models import ExecutionStatus
from app.db import db_session
from app.matching.employment_type import classify_employment_type
from app.models import EmploymentType, Job


def _count_full_time(jobs: list[Job]) -> int:
    """CLAUDE.md sections 53/84: 'full-time eligible' means the STRICT
    positive classifier (app.matching.employment_type.classify_employment_type,
    the same one the application executor's hard gate uses -- CLAUDE.md
    Phase 8 section 1), not the raw provider string column. UNKNOWN is never
    counted as full-time here either. Computed over the already-fetched jobs
    list (no second full-table scan) -- this project is a local, single-user
    job agent, not a planet-scale service (CLAUDE.md Phase 6's own scaling-
    honesty rule), so classifying an already-in-memory page's worth of jobs
    in Python is the right tradeoff over a redundant indexed column that
    would need to be kept in sync with two different classifiers."""
    return sum(
        1 for j in jobs
        if classify_employment_type(j.employment_type, j.title, j.description) == EmploymentType.FULL_TIME
    )


def compute_pipeline_summary(all_jobs: list[Job]) -> dict:
    """One grouped query per table -- never N+1, never a per-job DB query.
    `all_jobs` is the full (unfiltered) job list, already fetched once by
    the dashboard route via app.jobs_repo.list_jobs -- reused here instead
    of a second query."""
    with db_session() as conn:
        total_jobs = len(all_jobs)
        full_time_eligible = _count_full_time(all_jobs)

        sponsor_confirmed = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE sponsorship_status = 'CONFIRMED_SPONSOR'"
        ).fetchone()["c"]

        high_alignment = conn.execute(
            """SELECT COUNT(*) AS c FROM resume_quality_reports qr
               JOIN resume_variants rv ON rv.variant_id = qr.variant_id
               WHERE rv.current = 1 AND qr.alignment_label = 'STRONG'"""
        ).fetchone()["c"]

        resume_ready = conn.execute(
            "SELECT COUNT(*) AS c FROM resume_variants WHERE current = 1 AND status = 'READY'"
        ).fetchone()["c"]

        application_ready = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state = 'READY_TO_APPLY'"
        ).fetchone()["c"]

        applied = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state IN ('APPLIED', 'INTERVIEW')"
        ).fetchone()["c"]

        needs_action_execs = conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions WHERE active = 1 AND status = ?",
            (ExecutionStatus.NEEDS_USER_ACTION.value,),
        ).fetchone()["c"]
        needs_action_sessions = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE active = 1 AND needs_user_action = 1"
        ).fetchone()["c"]

        ready_for_final_submit = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE active = 1 AND status IN (?, ?)",
            (BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value, BrowserSessionStatus.AWAITING_USER_SUBMIT.value),
        ).fetchone()["c"]

        failed_attention = conn.execute(
            """SELECT COUNT(*) AS c FROM application_executions WHERE active = 1 AND status IN (?, ?, ?)""",
            (ExecutionStatus.SUBMISSION_FAILED.value, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value,
             ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value),
        ).fetchone()["c"]
        claim_failed = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state = 'CLAIM_VALIDATION_FAILED'"
        ).fetchone()["c"]

    return {
        "jobs_discovered": total_jobs,
        "full_time_eligible": full_time_eligible,
        "sponsor_confirmed": sponsor_confirmed,
        "high_alignment_jobs": high_alignment,
        "resume_ready": resume_ready,
        "application_ready": application_ready,
        "needs_user_action": needs_action_execs + needs_action_sessions,
        "ready_for_final_submit": ready_for_final_submit,
        "applied": applied,
        "failed_attention": failed_attention + claim_failed,
    }


_NON_ACTIONABLE_EMPLOYMENT_TYPES = frozenset({
    EmploymentType.CONTRACT, EmploymentType.C2C, EmploymentType.PART_TIME, EmploymentType.INTERNSHIP,
    EmploymentType.TEMPORARY, EmploymentType.SEASONAL, EmploymentType.FREELANCE,
})


def is_actionable(job: Job) -> bool:
    """CLAUDE.md sections 53-54, 84: a job POSITIVELY classified CONTRACT/
    C2C/PART_TIME/INTERNSHIP/TEMPORARY/SEASONAL/FREELANCE never appears in
    the default actionable queue, matching the executor's own hard gate
    (CLAUDE.md Phase 8 section 1). UNKNOWN (the common case for jobs with no
    explicit employment-type signal -- most manually-ingested/legacy jobs)
    stays visible by default, same as this project's existing 'UNKNOWN is
    not a hard-skip, just not auto-progressed' pattern for sponsorship --
    only a CONFIRMED non-full-time classification hides a job here."""
    classified = classify_employment_type(job.employment_type, job.title, job.description)
    return classified not in _NON_ACTIONABLE_EMPLOYMENT_TYPES
