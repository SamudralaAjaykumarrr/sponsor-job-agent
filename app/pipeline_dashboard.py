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


def _count_fresh(jobs: list[Job]) -> int:
    """'Fresh jobs' dashboard card: freshness_tier already computed and
    stored per job by app.freshness.tracker at discovery/ingest time (never
    recomputed here) -- MAXIMUM/VERY_HIGH/HIGH/MODERATE (<=24h, CLAUDE.md's
    own freshness tiers); LOWER means older-than-24h or unknown timestamp,
    so it is never counted as fresh."""
    return sum(1 for j in jobs if j.freshness_tier.value != "LOWER")


def compute_pipeline_summary(all_jobs: list[Job]) -> dict:
    """One grouped query per table -- never N+1, never a per-job DB query.
    `all_jobs` is the full (unfiltered) job list, already fetched once by
    the dashboard route via app.jobs_repo.list_jobs -- reused here instead
    of a second query."""
    with db_session() as conn:
        total_jobs = len(all_jobs)
        full_time_eligible = _count_full_time(all_jobs)
        fresh_jobs = _count_fresh(all_jobs)

        sponsor_confirmed = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE sponsorship_status = 'CONFIRMED_SPONSOR' AND is_test_fixture = 0"
        ).fetchone()["c"]

        high_alignment = conn.execute(
            """SELECT COUNT(*) AS c FROM resume_quality_reports qr
               JOIN resume_variants rv ON rv.variant_id = qr.variant_id
               JOIN jobs j ON j.id = rv.job_id
               WHERE rv.current = 1 AND qr.alignment_label = 'STRONG' AND j.is_test_fixture = 0"""
        ).fetchone()["c"]

        resume_ready = conn.execute(
            """SELECT COUNT(*) AS c FROM resume_variants rv JOIN jobs j ON j.id = rv.job_id
               WHERE rv.current = 1 AND rv.status = 'READY' AND j.is_test_fixture = 0"""
        ).fetchone()["c"]

        application_ready = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state = 'READY_TO_APPLY' AND is_test_fixture = 0"
        ).fetchone()["c"]

        applied = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state IN ('APPLIED', 'INTERVIEW') AND is_test_fixture = 0"
        ).fetchone()["c"]

        needs_user_action = _count_needs_action(conn)

        ready_for_final_submit = conn.execute(
            """SELECT COUNT(*) AS c FROM browser_assist_sessions s JOIN jobs j ON j.id = s.job_id
               WHERE s.active = 1 AND s.status IN (?, ?) AND j.is_test_fixture = 0""",
            (BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value, BrowserSessionStatus.AWAITING_USER_SUBMIT.value),
        ).fetchone()["c"]

        failed_attention = conn.execute(
            """SELECT COUNT(*) AS c FROM application_executions e JOIN jobs j ON j.id = e.job_id
               WHERE e.active = 1 AND e.status IN (?, ?, ?) AND j.is_test_fixture = 0""",
            (ExecutionStatus.SUBMISSION_FAILED.value, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value,
             ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value),
        ).fetchone()["c"]
        claim_failed = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state = 'CLAIM_VALIDATION_FAILED' AND is_test_fixture = 0"
        ).fetchone()["c"]

        # --- one-click-agent dashboard cards (section 23) --------------------
        one_page_ready = conn.execute(
            """SELECT COUNT(*) AS c FROM resume_variants rv JOIN jobs j ON j.id = rv.job_id
               WHERE rv.current = 1 AND rv.status = 'READY' AND rv.page_count = 1 AND j.is_test_fixture = 0"""
        ).fetchone()["c"]
        applying = conn.execute(
            """SELECT COUNT(*) AS c FROM application_executions e JOIN jobs j ON j.id = e.job_id
               WHERE e.active = 1 AND e.status IN ('SUBMITTING', 'SUBMITTED') AND j.is_test_fixture = 0"""
        ).fetchone()["c"]
        today_prefix = datetime.now(timezone.utc).date().isoformat()
        applied_today = conn.execute(
            """SELECT COUNT(*) AS c FROM application_executions e JOIN jobs j ON j.id = e.job_id
               WHERE e.status = 'APPLIED' AND e.finished_at LIKE ? AND j.is_test_fixture = 0""",
            (f"{today_prefix}%",),
        ).fetchone()["c"]
        skipped = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state LIKE 'SKIPPED%' AND is_test_fixture = 0"
        ).fetchone()["c"]

    return {
        "jobs_discovered": total_jobs,
        "fresh_jobs": fresh_jobs,
        "full_time_eligible": full_time_eligible,
        "sponsor_confirmed": sponsor_confirmed,
        "high_alignment_jobs": high_alignment,
        "strong_matches": high_alignment,
        "resume_ready": resume_ready,
        "one_page_ready": one_page_ready,
        "application_ready": application_ready,
        "applying": applying,
        "needs_user_action": needs_user_action,
        "ready_for_final_submit": ready_for_final_submit,
        "applied": applied,
        "applied_today": applied_today,
        "skipped": skipped,
        "failed_attention": failed_attention + claim_failed,
    }


# CLAUDE.md production-v2 dashboard defect 2: "Needs your action" card showed
# 0 while the section below it showed 5, because the summary card counted
# only 2 of the 4 sources that make up the actual queue. This module is now
# the ONE authoritative definition -- _NEEDS_ACTION_QUERIES is the single
# source of truth both compute_pipeline_summary()'s count and
# build_needs_action_queue()'s list are built from, so they can never
# disagree again (CLAUDE.md section 32). Every query excludes test-fixture
# jobs by default, matching every other real-mode dashboard query.
_NEEDS_ACTION_QUERIES: list[dict] = [
    {
        "kind": "execution",
        "sql": """SELECT j.id AS job_id, j.company, j.title, e.status, e.user_action_reason
                  FROM application_executions e JOIN jobs j ON j.id = e.job_id
                  WHERE e.active = 1 AND e.requires_user_action = 1 AND j.is_test_fixture = 0
                  ORDER BY e.updated_at DESC""",
        "completed": "Application prepared through form discovery/fill/validation.",
        "action": "Review and continue on the job detail page.",
    },
    {
        "kind": "browser_session",
        "sql": """SELECT j.id AS job_id, j.company, j.title, s.status, s.user_action_reason
                  FROM browser_assist_sessions s JOIN jobs j ON j.id = s.job_id
                  WHERE s.active = 1 AND s.needs_user_action = 1 AND j.is_test_fixture = 0
                  ORDER BY s.updated_at DESC""",
        "completed": "Browser-assist session navigated to the application form.",
        "action": "Resolve the blocker in-browser, then Continue.",
    },
    {
        "kind": "review_required",
        "sql": """SELECT id AS job_id, company, title, 'LIKELY_SPONSOR' AS status,
                         'Historical sponsorship signal only -- verify before applying.' AS user_action_reason
                  FROM jobs
                  WHERE sponsorship_status = 'LIKELY_SPONSOR' AND application_state = 'REVIEW_REQUIRED'
                        AND is_test_fixture = 0
                  ORDER BY updated_at DESC""",
        "completed": "Resume and application package generated for review.",
        "action": "Confirm sponsorship, then apply manually or mark Ready.",
    },
    {
        "kind": "resume_overflow",
        "sql": """SELECT rv.job_id AS job_id, j.company, j.title, 'RESUME_REVIEW_REQUIRED' AS status,
                         'One page could not be safely achieved within the compression bounds.' AS user_action_reason
                  FROM resume_variants rv JOIN jobs j ON j.id = rv.job_id
                  WHERE rv.current = 1 AND rv.status = 'REVIEW_REQUIRED' AND j.is_test_fixture = 0
                  ORDER BY rv.updated_at DESC""",
        "completed": "Tailored resume content generated; claim check and ATS parse passed.",
        "action": "Trim the candidate profile or manually review the resume.",
    },
]


def count_needs_action() -> int:
    """The authoritative total -- used for the dashboard card, the API, and
    metrics. Always the true count (never truncated by a display limit)."""
    with db_session() as conn:
        return _count_needs_action(conn)


def _count_needs_action(conn) -> int:
    total = 0
    for source in _NEEDS_ACTION_QUERIES:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM ({source['sql']}) t").fetchone()
        total += row["c"]
    return total


def build_needs_action_queue(limit: int = 25) -> list[dict]:
    """CLAUDE.md one-click-agent section 20: centralized 'Needs Your Action'
    queue -- each item shows company/role/reason/current stage/what the
    agent already completed/the exact action required, and links to the job
    detail page's existing Continue-equivalent controls (retry preparation,
    reconcile, resume browser session). Built from the SAME _NEEDS_ACTION_QUERIES
    source list count_needs_action() uses, so the displayed list and the
    summary card can never disagree (CLAUDE.md section 32)."""
    items: list[dict] = []
    with db_session() as conn:
        for source in _NEEDS_ACTION_QUERIES:
            for r in conn.execute(source["sql"]).fetchall():
                items.append({
                    "job_id": r["job_id"], "company": r["company"], "role": r["title"],
                    "stage": r["status"], "reason": r["user_action_reason"] or "action required",
                    "completed": source["completed"], "action": source["action"], "kind": source["kind"],
                })
    return items[:limit]


def build_recent_activity(limit: int = 20) -> list[dict]:
    """CLAUDE.md one-click-agent section 24 / production-v2 dashboard defect
    7: no PII values -- company/title are already-public job-posting
    metadata (same standard this project's job_identity_verifications table
    already uses), never JD text, resume content, or candidate profile
    fields. Merges job-level state/audit rows with the orchestrator's own
    agent-level lifecycle events (app.agent.run_state.list_recent_activity)
    so the feed reflects real current-cycle activity ('Discovery cycle
    started', 'Found N jobs', ...) instead of looking stale whenever a cycle
    hasn't yet produced a job-level event."""
    from app.agent import run_state as agent_run_state

    with db_session() as conn:
        state_rows = conn.execute(
            """SELECT h.changed_at AS ts, j.company, j.title, h.to_state AS detail, 'state' AS kind
               FROM application_state_history h JOIN jobs j ON j.id = h.job_id
               WHERE j.is_test_fixture = 0
               ORDER BY h.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        audit_rows = conn.execute(
            """SELECT a.created_at AS ts, j.company, j.title, a.event_type AS detail, 'execution' AS kind
               FROM application_audit_log a JOIN jobs j ON j.id = a.job_id
               WHERE j.is_test_fixture = 0
               ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    agent_rows = [
        {"ts": r["ts"], "company": None, "title": None, "detail": r["event"], "kind": "agent", "extra": r["detail"]}
        for r in agent_run_state.list_recent_activity(limit=limit)
    ]

    combined = [dict(r) for r in state_rows] + [dict(r) for r in audit_rows] + agent_rows
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

_AGENT_ACTIVITY_TEXT = {
    "agent_started": "Agent started.",
    "agent_stopped": "Agent stopped.",
    "cycle_started": "Discovery cycle started.",
    "discovery_completed": "Discovery: {extra}",
    "resumes_generated": "Resumes: {extra}",
    "applications_prepared": "Applications prepared: {extra}",
    "applications_applied": "Applications applied: {extra}",
    "needs_user_action": "Needs user action: {extra}",
    "cycle_finished": "Cycle finished ({extra}).",
    "error": "Error: {extra}",
    "recovered": "Recovered: {extra}",
}


def _activity_text(row: dict) -> str:
    if row["kind"] == "state":
        return _STATE_ACTIVITY_TEXT.get(row["detail"], f"State changed to {row['detail']}.")
    if row["kind"] == "agent":
        template = _AGENT_ACTIVITY_TEXT.get(row["detail"], (row["detail"] or "Agent activity") + ": {extra}")
        return template.format(extra=row.get("extra") or "")
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


def build_job_rows(
    filters: dict, *, full_time_only: bool = True, resume_status: str = "",
    needs_action_only: bool = False, row_cap: int | None = None, search: str = "",
) -> dict:
    """Shared row-building logic behind both the unified Dashboard (top
    matches) and the dedicated Jobs page (full searchable/filterable list) --
    CLAUDE.md's 'use current backend routes/services rather than creating
    duplicate business logic' rule extended to the premium UI's own two
    job-listing surfaces. Pulled out of the (until now) inline dashboard()
    route body unchanged -- same filters dict shape (app.jobs_repo.list_jobs),
    same is_actionable() gate, same resume_status/needs_action_only
    post-filters, same priority-sorted row cap."""
    from app.applications import repo as applications_repo
    from app.jobs_repo import list_jobs
    from app.resume_optimizer import repo as resume_optimizer_repo

    jobs = list_jobs(filters)
    if full_time_only:
        jobs = [j for j in jobs if is_actionable(j)]
    if search:
        needle = search.strip().lower()
        jobs = [
            j for j in jobs
            if needle in (j.title or "").lower() or needle in (j.company or "").lower()
            or needle in (j.location or "").lower()
        ]

    job_ids = [j.id for j in jobs if j.id is not None]
    quality_by_job = resume_optimizer_repo.get_quality_reports_for_jobs(job_ids)
    variant_by_job = resume_optimizer_repo.get_current_variants_for_jobs(job_ids)
    active_execution_by_job = applications_repo.get_active_executions_for_jobs(job_ids)

    def resume_status_of(jid: int) -> str:
        variant = variant_by_job.get(jid)
        return variant["status"] if variant else "NOT_GENERATED"

    if resume_status:
        jobs = [j for j in jobs if resume_status_of(j.id) == resume_status]
    if needs_action_only:
        jobs = [j for j in jobs if (active_execution_by_job.get(j.id) or {}).get("requires_user_action")]

    total_matching = len(jobs)
    if row_cap is not None and total_matching > row_cap:
        jobs = jobs[:row_cap]

    pipeline_rows = [
        {
            "job": j,
            "quality_report": (quality_by_job.get(j.id) or {}).get("report"),
            "resume_status": resume_status_of(j.id),
            "page_count": (variant_by_job.get(j.id) or {}).get("page_count"),
            "execution": active_execution_by_job.get(j.id),
            "employment_classified": classify_employment_type(j.employment_type, j.title, j.description).value,
        }
        for j in jobs
    ]
    return {"jobs": jobs, "pipeline_rows": pipeline_rows, "total_matching": total_matching}


# --- Tracker board (Applied / Assessment / Interview / Offer / Rejected /
# Withdrawn) -------------------------------------------------------------

TRACKER_LANES: list[tuple[str, str]] = [
    ("APPLIED", "Applied"),
    ("ASSESSMENT", "Assessment"),
    ("INTERVIEW", "Interview"),
    ("OFFER", "Offer"),
    ("REJECTED", "Rejected"),
    ("WITHDRAWN", "Withdrawn"),
]


def build_tracker_board(lane_limit: int = 50) -> list[dict]:
    """One bounded, indexed query per lane (application_state is indexed --
    see app.jobs_repo) -- never a full-table scan, matching this project's
    existing 'every list/query is bounded' convention. Test fixtures are
    excluded, matching every other real-mode dashboard query."""
    with db_session() as conn:
        lanes = []
        for state_value, label in TRACKER_LANES:
            rows = conn.execute(
                """SELECT id, company, title, location, work_arrangement, sponsorship_status,
                          updated_at, first_seen_at
                   FROM jobs WHERE application_state = ? AND is_test_fixture = 0
                   ORDER BY updated_at DESC LIMIT ?""",
                (state_value, lane_limit),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE application_state = ? AND is_test_fixture = 0",
                (state_value,),
            ).fetchone()["c"]
            lanes.append({
                "state": state_value, "label": label, "total": total,
                "jobs": [dict(r) for r in rows],
            })
        return lanes


def count_skipped_jobs() -> int:
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state LIKE 'SKIPPED%' AND is_test_fixture = 0"
        ).fetchone()["c"]


def list_skipped_job_rows(*, company: str = "", provider: str = "", work_arrangement: str = "",
                           sponsorship_status: str = "", limit: int = 200) -> list[dict]:
    """Premium UI Applications page's "Skipped" tab: skipped jobs never get
    an application_executions row (the pipeline hard-skips them before
    reaching the executor), so this reads app.jobs_repo's own table
    directly and reshapes rows into the same shape
    app.applications.repo.list_executions_with_jobs() already returns --
    letting the Applications template render both with one identical table,
    never a second parallel template."""
    with db_session() as conn:
        clauses = ["application_state LIKE 'SKIPPED%'", "is_test_fixture = 0"]
        params: list = []
        if company:
            clauses.append("company LIKE ?")
            params.append(f"%{company}%")
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if work_arrangement:
            clauses.append("work_arrangement = ?")
            params.append(work_arrangement)
        if sponsorship_status:
            clauses.append("sponsorship_status = ?")
            params.append(sponsorship_status)
        query = (
            "SELECT id, title, company, provider, application_state, sponsorship_status, "
            "updated_at, notes FROM jobs WHERE " + " AND ".join(clauses) +
            " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "job_id": r["id"], "job_title": r["title"], "job_company": r["company"],
            "provider": r["provider"], "status": r["application_state"],
            "mode": "", "job_sponsorship_status": r["sponsorship_status"],
            "started_at": r["updated_at"], "user_action_reason": r["notes"] or "",
        }
        for r in rows
    ]
