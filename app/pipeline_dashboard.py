"""Aggregation for the unified one-page dashboard (CLAUDE.md Phase 14
sections 44-55, 79). Deliberately separate from app.resume_optimizer (which
knows nothing about applications/browser sessions) and from
app.applications.repo (which knows nothing about resume diagnostics) --
this module is the cross-cutting read layer the dashboard route uses, built
from small, indexed, already-existing queries (CLAUDE.md section 55: never
a full-table scan or per-job recomputation on every page load)."""

from datetime import datetime, timezone

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

        # --- one-click-agent dashboard cards (section 23) --------------------
        one_page_ready = conn.execute(
            "SELECT COUNT(*) AS c FROM resume_variants WHERE current = 1 AND status = 'READY' AND page_count = 1"
        ).fetchone()["c"]
        applying = conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions WHERE active = 1 AND status IN ('SUBMITTING', 'SUBMITTED')"
        ).fetchone()["c"]
        today_prefix = datetime.now(timezone.utc).date().isoformat()
        applied_today = conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions WHERE status = 'APPLIED' AND finished_at LIKE ?",
            (f"{today_prefix}%",),
        ).fetchone()["c"]
        skipped = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state LIKE 'SKIPPED%'"
        ).fetchone()["c"]

    return {
        "jobs_discovered": total_jobs,
        "full_time_eligible": full_time_eligible,
        "sponsor_confirmed": sponsor_confirmed,
        "high_alignment_jobs": high_alignment,
        "strong_matches": high_alignment,
        "resume_ready": resume_ready,
        "one_page_ready": one_page_ready,
        "application_ready": application_ready,
        "applying": applying,
        "needs_user_action": needs_action_execs + needs_action_sessions,
        "ready_for_final_submit": ready_for_final_submit,
        "applied": applied,
        "applied_today": applied_today,
        "skipped": skipped,
        "failed_attention": failed_attention + claim_failed,
    }


def build_needs_action_queue(limit: int = 25) -> list[dict]:
    """CLAUDE.md one-click-agent section 20: centralized 'Needs Your Action'
    queue -- each item shows company/role/reason/current stage/what the
    agent already completed/the exact action required, and links to the job
    detail page's existing Continue-equivalent controls (retry preparation,
    reconcile, resume browser session). Sourced entirely from
    already-indexed columns, one bounded query per source -- never a
    per-job N+1 scan."""
    items: list[dict] = []
    with db_session() as conn:
        exec_rows = conn.execute(
            """SELECT j.id AS job_id, j.company, j.title, e.execution_id, e.status, e.user_action_reason
               FROM application_executions e JOIN jobs j ON j.id = e.job_id
               WHERE e.active = 1 AND e.requires_user_action = 1
               ORDER BY e.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        for r in exec_rows:
            items.append({
                "job_id": r["job_id"], "company": r["company"], "role": r["title"],
                "stage": r["status"], "reason": r["user_action_reason"] or "action required",
                "completed": "Application prepared through form discovery/fill/validation.",
                "action": "Review and continue on the job detail page.",
                "kind": "execution",
            })

        session_rows = conn.execute(
            """SELECT j.id AS job_id, j.company, j.title, s.session_id, s.status, s.user_action_reason
               FROM browser_assist_sessions s JOIN jobs j ON j.id = s.job_id
               WHERE s.active = 1 AND s.needs_user_action = 1
               ORDER BY s.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        for r in session_rows:
            items.append({
                "job_id": r["job_id"], "company": r["company"], "role": r["title"],
                "stage": r["status"], "reason": r["user_action_reason"] or "action required",
                "completed": "Browser-assist session navigated to the application form.",
                "action": "Resolve the blocker in-browser, then Continue.",
                "kind": "browser_session",
            })

        review_rows = conn.execute(
            """SELECT id AS job_id, company, title FROM jobs
               WHERE sponsorship_status = 'LIKELY_SPONSOR' AND application_state = 'REVIEW_REQUIRED'
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        for r in review_rows:
            items.append({
                "job_id": r["job_id"], "company": r["company"], "role": r["title"],
                "stage": "LIKELY_SPONSOR", "reason": "Historical sponsorship signal only -- verify before applying.",
                "completed": "Resume and application package generated for review.",
                "action": "Confirm sponsorship, then apply manually or mark Ready.",
                "kind": "review_required",
            })

        overflow_rows = conn.execute(
            """SELECT rv.job_id AS job_id, j.company, j.title FROM resume_variants rv
               JOIN jobs j ON j.id = rv.job_id
               WHERE rv.current = 1 AND rv.status = 'REVIEW_REQUIRED'
               ORDER BY rv.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        for r in overflow_rows:
            items.append({
                "job_id": r["job_id"], "company": r["company"], "role": r["title"],
                "stage": "RESUME_REVIEW_REQUIRED",
                "reason": "One page could not be safely achieved within the compression bounds.",
                "completed": "Tailored resume content generated; claim check and ATS parse passed.",
                "action": "Trim the candidate profile or manually review the resume.",
                "kind": "resume_overflow",
            })

    return items[:limit]


def build_recent_activity(limit: int = 20) -> list[dict]:
    """CLAUDE.md one-click-agent section 24: no PII values -- company/title
    are already-public job-posting metadata (same standard this project's
    job_identity_verifications table already uses), never JD text, resume
    content, or candidate profile fields."""
    with db_session() as conn:
        state_rows = conn.execute(
            """SELECT h.changed_at AS ts, j.company, j.title, h.to_state AS detail, 'state' AS kind
               FROM application_state_history h JOIN jobs j ON j.id = h.job_id
               ORDER BY h.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        audit_rows = conn.execute(
            """SELECT a.created_at AS ts, j.company, j.title, a.event_type AS detail, 'execution' AS kind
               FROM application_audit_log a JOIN jobs j ON j.id = a.job_id
               ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    combined = [dict(r) for r in state_rows] + [dict(r) for r in audit_rows]
    combined.sort(key=lambda r: r["ts"] or "", reverse=True)
    return [
        {"ts": r["ts"], "company": r["company"], "role": r["title"], "text": _activity_text(r)}
        for r in combined[:limit]
    ]


_STATE_ACTIVITY_TEXT = {
    "SKIPPED": "Skipped -- not a target role.",
    "SKIPPED_NO_SPONSORSHIP": "Skipped -- no sponsorship.",
    "SKIPPED_SENIORITY": "Skipped -- seniority mismatch.",
    "SKIPPED_COMPENSATION": "Skipped -- compensation below threshold.",
    "SKIPPED_POOR_MATCH": "Skipped -- weak technical match.",
    "READY_TO_APPLY": "Ready to apply -- confirmed sponsor.",
    "REVIEW_REQUIRED": "Review required -- likely sponsor.",
    "CLAIM_VALIDATION_FAILED": "Paused -- resume claim check failed.",
    "APPLIED": "Application confirmed.",
    "NEEDS_USER_ACTION": "Paused -- needs your action.",
    "EXECUTION_QUEUED": "Application queued for preparation.",
}

_AUDIT_ACTIVITY_TEXT = {
    "prepared": "Application preparation started.",
    "form_discovered": "Application form discovered.",
    "form_discovery_unsupported": "Provider form discovery unsupported -- ASSIST only.",
    "form_mapped": "Application fields mapped.",
    "filled": "Application draft filled.",
    "user_action_required": "Paused -- needs your action.",
    "validated": "Application validated.",
    "submit_attempted": "Submitting application.",
    "confirmed": "Application confirmed.",
    "failed": "Application attempt failed.",
}


def _activity_text(row: dict) -> str:
    if row["kind"] == "state":
        return _STATE_ACTIVITY_TEXT.get(row["detail"], f"State changed to {row['detail']}.")
    return _AUDIT_ACTIVITY_TEXT.get(row["detail"], row["detail"] or "Activity recorded.")


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
