"""Persistence for the Phase 8 application executor: application_executions,
application_answer_snapshots, application_audit_log, plus the coarse
jobs.application_state mirror. See docs/phase8-application-executor.md
"Two-layer state model"."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.applications.models import SENSITIVE_CATEGORIES, ApplicationField, ExecutionStatus, TERMINAL_STATUSES
from app.db import db_session
from app.jobs_repo import record_state_change
from app.jobs_repo import update_job as _update_job_row
from app.models import ApplicationState


class DuplicateExecutionError(Exception):
    """Raised when a second execution is attempted for a job that already
    has an active (non-terminal) execution -- CLAUDE.md Phase 8 sections
    32/61. The unique partial index on application_executions(job_id) WHERE
    active=1 is the actual atomic guard; this exception is just how a Python
    caller observes that guard firing (including under concurrent workers)."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_execution_id() -> str:
    return f"exec_{uuid.uuid4().hex}"


def _is_unique_violation(exc: BaseException) -> bool:
    name = type(exc).__name__
    return "IntegrityError" in name or "UniqueViolation" in name


# Coarse job.application_state a given fine-grained ExecutionStatus should be
# mirrored to. Anything not listed leaves the job's application_state
# untouched (e.g. FORM_DISCOVERED/FORM_MAPPED/FORM_FILLED all just mean
# "still queued/working" from the dashboard's point of view).
_JOB_STATE_MIRROR: dict[ExecutionStatus, ApplicationState] = {
    ExecutionStatus.QUEUED: ApplicationState.EXECUTION_QUEUED,
    ExecutionStatus.STARTED: ApplicationState.EXECUTION_QUEUED,
    ExecutionStatus.VALIDATION_REQUIRED: ApplicationState.NEEDS_USER_ACTION,
    ExecutionStatus.NEEDS_USER_ACTION: ApplicationState.NEEDS_USER_ACTION,
    ExecutionStatus.SUBMISSION_READY: ApplicationState.EXECUTION_QUEUED,
    ExecutionStatus.SUBMITTING: ApplicationState.SUBMITTING,
    ExecutionStatus.SUBMITTED: ApplicationState.SUBMITTING,
    ExecutionStatus.SUBMISSION_CONFIRMED: ApplicationState.APPLIED,
    ExecutionStatus.APPLIED: ApplicationState.APPLIED,
    ExecutionStatus.SUBMISSION_FAILED: ApplicationState.SUBMISSION_FAILED,
    ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE: ApplicationState.SUBMISSION_FAILED,
    ExecutionStatus.PERMANENT_SUBMISSION_FAILURE: ApplicationState.SUBMISSION_FAILED,
    ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED: ApplicationState.DUPLICATE_APPLICATION_BLOCKED,
    ExecutionStatus.WITHDRAWN: ApplicationState.WITHDRAWN,
    ExecutionStatus.SUBMISSION_STATUS_UNKNOWN: ApplicationState.SUBMISSION_STATUS_UNKNOWN,
}


def create_execution(job_id: int, *, provider: str, mode: str, correlation_id: str = "") -> str:
    """Atomically creates a new active execution row. Raises
    DuplicateExecutionError if the job already has one (the partial unique
    index is what actually serializes this across concurrent workers)."""
    execution_id = new_execution_id()
    now = utcnow()
    try:
        with db_session() as conn:
            conn.execute(
                """INSERT INTO application_executions
                   (execution_id, job_id, provider, mode, status, active, started_at,
                    attempt_count, correlation_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, 0, ?, ?, ?)""",
                (execution_id, job_id, provider, mode, ExecutionStatus.QUEUED.value, now, correlation_id, now, now),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise DuplicateExecutionError(f"job {job_id} already has an active execution") from exc
        raise
    log_event(execution_id, job_id, "prepared", detail="execution created", correlation_id=correlation_id)
    mirror_job_state(job_id, ExecutionStatus.QUEUED)
    return execution_id


def get_execution(execution_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM application_executions WHERE execution_id = ?", (execution_id,)).fetchone()
        return dict(row) if row else None


def get_active_execution_for_job(job_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM application_executions WHERE job_id = ? AND active = 1", (job_id,)
        ).fetchone()
        return dict(row) if row else None


def list_executions_for_job(job_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_executions WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_executions(*, status: Optional[str] = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM application_executions"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def update_execution(execution_id: str, job_id: int, status: ExecutionStatus, **fields) -> None:
    fields["status"] = status.value
    fields["updated_at"] = utcnow()
    if status in TERMINAL_STATUSES:
        fields["active"] = 0
        fields.setdefault("finished_at", utcnow())
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db_session() as conn:
        conn.execute(
            f"UPDATE application_executions SET {set_clause} WHERE execution_id = ?",
            [*fields.values(), execution_id],
        )
    mirror_job_state(job_id, status)


def mirror_job_state(job_id: int, status: ExecutionStatus) -> None:
    target = _JOB_STATE_MIRROR.get(status)
    if target is None:
        return
    with db_session() as conn:
        row = conn.execute("SELECT application_state FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return
    current = row["application_state"]
    if current == target.value:
        return
    _update_job_row(job_id, application_state=target)
    record_state_change(job_id, current, target.value, actor="executor")


def snapshot_answers(execution_id: str, fields: list[ApplicationField], *, source_version: str) -> int:
    """CLAUDE.md Phase 8 section 18: one snapshot row per field used for this
    execution. Sensitive-category values are minimized to a bounded
    fingerprint rather than stored verbatim (section 51's logging-privacy
    rule extended to storage)."""
    import hashlib

    now = utcnow()
    rows = []
    for f in fields:
        if f.verified_value is None:
            continue
        if f.category in SENSITIVE_CATEGORIES:
            value = hashlib.sha256(f.verified_value.encode("utf-8")).hexdigest()[:16]
        else:
            value = f.verified_value[:500]
        rows.append((
            execution_id, f.field_id, value, f.value_source, source_version,
            1 if not f.needs_user_input else 0, 1 if f.category in SENSITIVE_CATEGORIES else 0, now,
        ))
    if not rows:
        return 0
    with db_session() as conn:
        conn.executemany(
            """INSERT INTO application_answer_snapshots
               (execution_id, field_id, value, source, source_version, verified, sensitive, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


def list_answer_snapshot(execution_id: str) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_answer_snapshots WHERE execution_id = ? ORDER BY id ASC", (execution_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def log_event(execution_id: str, job_id: int, event_type: str, *, detail: str = "", correlation_id: str = "") -> None:
    """CLAUDE.md Phase 8 section 49. Never logs field VALUES -- event_type +
    a short structural detail string only."""
    with db_session() as conn:
        conn.execute(
            """INSERT INTO application_audit_log (execution_id, job_id, event_type, detail, correlation_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (execution_id, job_id, event_type, detail[:500], correlation_id, utcnow()),
        )


# CLAUDE.md Phase 8 section 42: dashboard bucket -> underlying ExecutionStatus
# values. Kept here (not in main.py) so app.applications.metrics and the
# dashboard route share one definition.
DASHBOARD_BUCKETS: dict[str, tuple[str, ...]] = {
    "ready": (ExecutionStatus.SUBMISSION_READY.value,),
    "queued": (ExecutionStatus.QUEUED.value, ExecutionStatus.STARTED.value),
    "preparing": (ExecutionStatus.FORM_DISCOVERED.value, ExecutionStatus.FORM_MAPPED.value,
                  ExecutionStatus.FORM_FILLED.value),
    "needs_action": (ExecutionStatus.NEEDS_USER_ACTION.value, ExecutionStatus.VALIDATION_REQUIRED.value,
                      ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value),
    "submitting": (ExecutionStatus.SUBMITTING.value, ExecutionStatus.SUBMITTED.value),
    "applied": (ExecutionStatus.SUBMISSION_CONFIRMED.value, ExecutionStatus.APPLIED.value),
    "failed": (ExecutionStatus.SUBMISSION_FAILED.value, ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value,
               ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value, ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED.value),
}


def list_executions_with_jobs(*, bucket: str = "", company: str = "", provider: str = "",
                               work_arrangement: str = "", sponsorship_status: str = "",
                               limit: int = 200) -> list[dict]:
    query = (
        "SELECT e.*, j.title AS job_title, j.company AS job_company, j.location AS job_location, "
        "j.work_arrangement AS job_work_arrangement, j.sponsorship_status AS job_sponsorship_status, "
        "j.employment_type AS job_employment_type "
        "FROM application_executions e JOIN jobs j ON j.id = e.job_id"
    )
    clauses, params = [], []
    if bucket and bucket in DASHBOARD_BUCKETS:
        statuses = DASHBOARD_BUCKETS[bucket]
        clauses.append(f"e.status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    if company:
        clauses.append("j.company LIKE ?")
        params.append(f"%{company}%")
    if provider:
        clauses.append("e.provider = ?")
        params.append(provider)
    if work_arrangement:
        clauses.append("j.work_arrangement = ?")
        params.append(work_arrangement)
    if sponsorship_status:
        clauses.append("j.sponsorship_status = ?")
        params.append(sponsorship_status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY e.started_at DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def list_audit_log(execution_id: Optional[str] = None, job_id: Optional[int] = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM application_audit_log"
    clauses, params = [], []
    if execution_id:
        clauses.append("execution_id = ?")
        params.append(execution_id)
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
