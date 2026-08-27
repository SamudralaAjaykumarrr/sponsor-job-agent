"""Application execution queue (CLAUDE.md Phase 8 sections 38, 61). Same
atomic `UPDATE ... WHERE (unleased OR lease-expired)` claim pattern as
app.workers.leasing (correctness comes from the database's own single-writer
serialization / MVCC, not application-level locking) -- kept as a separate,
smaller mechanism here rather than importing app.workers.leasing directly,
since it claims a different table with different columns and this queue must
never be claimable by a DISCOVERY-only worker (see
app.applications.capabilities)."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.applications.models import ExecutionStatus
from app.db import db_session

# CLAUDE.md Phase 9 section 4 (crash recovery): a worker that crashes AFTER
# claiming a QUEUED execution advances its status almost immediately
# (executor.process_execution's first write is STARTED) -- if only QUEUED
# rows were ever reclaimable, a crash at any point past that first write
# would strand the row FOREVER even once its lease expires, since it would
# never again match this WHERE clause. Every status a worker can leave an
# execution in mid-pipeline (i.e. everything that is not a terminal status
# and not a status that means "paused for a human/reconciliation" --
# NEEDS_USER_ACTION/VALIDATION_REQUIRED/SUBMISSION_READY/
# SUBMISSION_STATUS_UNKNOWN) is included here so lease expiry alone is
# sufficient to recover it, exactly like Phase 5's poll/verification queues.
# SUBMITTING/SUBMITTED are deliberately included: a reclaim landing on one of
# those is made safe by executor.process_execution()'s own resume guard,
# which converts it straight to SUBMISSION_STATUS_UNKNOWN rather than ever
# calling provider.submit() a second time -- never a blind retry.
#
# RETRYABLE_SUBMISSION_FAILURE (autonomous-ux-reliability-v1) is included so
# a bounded, backoff-cooled submit retry (see app.applications.executor's
# _RETRYABLE_SUBMIT_ERROR_TYPES handling) can be reclaimed and reprocessed
# from scratch -- never SUBMISSION_STATUS_UNKNOWN, which stays permanently
# unclaimable here and can only be resolved by explicit reconciliation.
_ACTIVE_CLAIMABLE_STATUSES = (
    ExecutionStatus.QUEUED.value,
    ExecutionStatus.STARTED.value,
    ExecutionStatus.FORM_DISCOVERED.value,
    ExecutionStatus.FORM_MAPPED.value,
    ExecutionStatus.FORM_FILLED.value,
    ExecutionStatus.SUBMITTING.value,
    ExecutionStatus.SUBMITTED.value,
    ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value,
)


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def utcnow() -> str:
    return utcnow_dt().isoformat()


def _iso_plus(seconds: int) -> str:
    return (utcnow_dt() + timedelta(seconds=seconds)).isoformat()


def new_attempt_id() -> str:
    return uuid.uuid4().hex


def claim_execution_batch(*, worker_id: str, limit: int, lease_seconds: int,
                           statuses: tuple[str, ...] = _ACTIVE_CLAIMABLE_STATUSES) -> list[dict]:
    now = utcnow()
    claimed: list[dict] = []
    placeholders = ", ".join("?" for _ in statuses)
    with db_session() as conn:
        candidates = conn.execute(
            f"""SELECT id, execution_id FROM application_executions
                WHERE active = 1 AND status IN ({placeholders})
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY started_at ASC
                LIMIT ?""",
            [*statuses, now, limit * 4],
        ).fetchall()

        for row in candidates:
            attempt_id = new_attempt_id()
            expires = _iso_plus(lease_seconds)
            cur = conn.execute(
                """UPDATE application_executions
                   SET lease_owner = ?, lease_attempt_id = ?, lease_acquired_at = ?, lease_expires_at = ?
                   WHERE execution_id = ? AND active = 1
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (worker_id, attempt_id, now, expires, row["execution_id"], now),
            )
            if cur.rowcount == 1:
                full = conn.execute(
                    "SELECT * FROM application_executions WHERE execution_id = ?", (row["execution_id"],)
                ).fetchone()
                claimed.append(dict(full))
            if len(claimed) >= limit:
                break
    return claimed


def release_execution_lease(execution_id: str, *, expected_attempt_id: Optional[str] = None) -> None:
    with db_session() as conn:
        if expected_attempt_id is not None:
            conn.execute(
                """UPDATE application_executions SET lease_owner=NULL, lease_attempt_id=NULL,
                     lease_acquired_at=NULL, lease_expires_at=NULL
                   WHERE execution_id = ? AND lease_attempt_id = ?""",
                (execution_id, expected_attempt_id),
            )
        else:
            conn.execute(
                """UPDATE application_executions SET lease_owner=NULL, lease_attempt_id=NULL,
                     lease_acquired_at=NULL, lease_expires_at=NULL WHERE execution_id = ?""",
                (execution_id,),
            )


def extend_execution_lease(execution_id: str, attempt_id: str, *, lease_seconds: int) -> bool:
    """CLAUDE.md Phase 8 section 40 parallel to Phase 5 section 29: a claimed
    item that is skipped without being attempted (e.g. rate-limited) gets a
    cooldown extension, never a bare release -- avoids a busy-spin of
    claim/cancel/reclaim across concurrent executor workers."""
    expires = _iso_plus(lease_seconds)
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE application_executions SET lease_expires_at = ? WHERE execution_id = ? AND lease_attempt_id = ?",
            (expires, execution_id, attempt_id),
        )
        return cur.rowcount == 1


def count_active_leases() -> int:
    now = utcnow()
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions WHERE lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (now,),
        ).fetchone()["c"]
