"""Per-attempt history for the Phase 9 application executor worker fleet
(CLAUDE.md Phase 9 section 6). Mirrors the shape/philosophy of
app.workers.repo's poll_attempts helpers, but for `application_attempts` --
kept as a separate table/module (never merged with poll_attempts) since the
two describe entirely different pipelines (discovery polling vs. candidate
application submission) with different fields and different consumers.

Never stores secrets, passwords, tokens, or candidate answer VALUES -- only
ids/stage/result/timestamps/bounded safe error text."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session

_MAX_ATTEMPTS_PER_EXECUTION = 50


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_attempt_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ApplicationAttemptRecord:
    attempt_id: str
    execution_id: str
    job_id: int
    worker_id: str
    provider: str = ""
    started_at: str = field(default_factory=utcnow)
    finished_at: Optional[str] = None
    stage: str = ""
    result: str = ""
    retryable: bool = False
    submission_request_started_at: Optional[str] = None
    submission_request_finished_at: Optional[str] = None
    confirmation_observed: bool = False
    error_type: str = ""
    safe_error_message: str = ""
    correlation_id: str = ""


def record_attempt(attempt: ApplicationAttemptRecord) -> int:
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO application_attempts
                 (attempt_id, execution_id, job_id, worker_id, provider, started_at, finished_at,
                  stage, result, retryable, submission_request_started_at, submission_request_finished_at,
                  confirmation_observed, error_type, safe_error_message, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(attempt_id) DO UPDATE SET
                 finished_at=excluded.finished_at, stage=excluded.stage, result=excluded.result,
                 retryable=excluded.retryable,
                 submission_request_started_at=excluded.submission_request_started_at,
                 submission_request_finished_at=excluded.submission_request_finished_at,
                 confirmation_observed=excluded.confirmation_observed,
                 error_type=excluded.error_type, safe_error_message=excluded.safe_error_message""",
            (
                attempt.attempt_id, attempt.execution_id, attempt.job_id, attempt.worker_id, attempt.provider,
                attempt.started_at, attempt.finished_at, attempt.stage, attempt.result, int(attempt.retryable),
                attempt.submission_request_started_at, attempt.submission_request_finished_at,
                int(attempt.confirmation_observed), attempt.error_type[:200], attempt.safe_error_message[:500],
                attempt.correlation_id,
            ),
        )
        # Bound history per execution so this never grows unbounded.
        conn.execute(
            """DELETE FROM application_attempts WHERE execution_id = ? AND id NOT IN (
                 SELECT id FROM application_attempts WHERE execution_id = ?
                 ORDER BY started_at DESC LIMIT ?
               )""",
            (attempt.execution_id, attempt.execution_id, _MAX_ATTEMPTS_PER_EXECUTION),
        )
        return cur.lastrowid


def list_attempts_for_execution(execution_id: str, limit: int = 50) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_attempts WHERE execution_id = ? ORDER BY started_at DESC LIMIT ?",
            (execution_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_recent_attempts(limit: int = 100, worker_id: str = "", result: str = "") -> list[dict]:
    clauses, params = [], []
    if worker_id:
        clauses.append("worker_id = ?")
        params.append(worker_id)
    if result:
        clauses.append("result = ?")
        params.append(result)
    query = "SELECT * FROM application_attempts"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def count_attempts_since(since_iso: str, *, result: str = "") -> int:
    query = "SELECT COUNT(*) AS c FROM application_attempts WHERE started_at >= ?"
    params: list = [since_iso]
    if result:
        query += " AND result = ?"
        params.append(result)
    with db_session() as conn:
        return conn.execute(query, params).fetchone()["c"]
