"""CRUD for the Phase 5 execution tables: workers, poll_attempts,
dead_letters, provider_circuit_state. Kept separate from app.registry.repo/
store (Phase 3/4 domain tables) -- this module is purely about *execution*
bookkeeping. All list functions are bounded (LIMIT), per CLAUDE.md's
high-volume-operations rules carried forward from Phase 4."""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.workers.models import AttemptRecord, AttemptStatus, WorkerStatus

_MAX_ATTEMPTS_PER_PORTAL = 100


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_attempt_id() -> str:
    return uuid.uuid4().hex


# --- Workers -----------------------------------------------------------

def upsert_worker(
    worker_id: str, *, hostname: str, pid: int, shard_index: int, shard_count: int, status: str,
) -> None:
    now = utcnow()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO workers (worker_id, hostname, pid, shard_index, shard_count,
                                     started_at, last_heartbeat_at, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(worker_id) DO UPDATE SET
                 hostname=excluded.hostname, pid=excluded.pid, shard_index=excluded.shard_index,
                 shard_count=excluded.shard_count, status=excluded.status,
                 last_heartbeat_at=excluded.last_heartbeat_at, updated_at=excluded.updated_at""",
            (worker_id, hostname, pid, shard_index, shard_count, now, now, status, now),
        )


def heartbeat_worker(
    worker_id: str, *, status: str, current_portal_type: str = "", current_portal_id: Optional[int] = None,
    portals_processed: Optional[int] = None, jobs_processed: Optional[int] = None, errors: Optional[int] = None,
) -> None:
    now = utcnow()
    fields = {"last_heartbeat_at": now, "status": status, "updated_at": now,
              "current_portal_type": current_portal_type, "current_portal_id": current_portal_id}
    if portals_processed is not None:
        fields["portals_processed"] = portals_processed
    if jobs_processed is not None:
        fields["jobs_processed"] = jobs_processed
    if errors is not None:
        fields["errors"] = errors
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db_session() as conn:
        conn.execute(f"UPDATE workers SET {set_clause} WHERE worker_id = ?", [*fields.values(), worker_id])


def get_worker(worker_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return dict(row) if row else None


def list_workers(limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM workers ORDER BY last_heartbeat_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def list_stale_workers(older_than_seconds: int, limit: int = 200) -> list[dict]:
    """Workers whose heartbeat is older than the threshold and not already
    marked STOPPED -- used to flag likely-dead workers in the dashboard."""
    cutoff = (datetime.now(timezone.utc).timestamp()) - older_than_seconds
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM workers WHERE status != 'STOPPED' ORDER BY last_heartbeat_at ASC LIMIT ?", (limit,)
        ).fetchall()
    stale = []
    for r in rows:
        d = dict(r)
        try:
            hb = datetime.fromisoformat(d["last_heartbeat_at"]).timestamp()
        except (ValueError, TypeError):
            continue
        if hb < cutoff:
            stale.append(d)
    return stale


# --- Attempts ------------------------------------------------------------

def record_attempt(attempt: AttemptRecord) -> int:
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO poll_attempts
                 (attempt_id, portal_type, portal_id, worker_id, provider, queue, started_at,
                  finished_at, status, jobs_received, jobs_new, jobs_duplicate, jobs_filtered,
                  latency_ms, error_type, detail, retryable, next_retry_at, cycle_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(attempt_id) DO UPDATE SET
                 finished_at=excluded.finished_at, status=excluded.status,
                 jobs_received=excluded.jobs_received, jobs_new=excluded.jobs_new,
                 jobs_duplicate=excluded.jobs_duplicate, jobs_filtered=excluded.jobs_filtered,
                 latency_ms=excluded.latency_ms, error_type=excluded.error_type,
                 detail=excluded.detail, retryable=excluded.retryable,
                 next_retry_at=excluded.next_retry_at""",
            (
                attempt.attempt_id, attempt.portal_type, attempt.portal_id, attempt.worker_id,
                attempt.provider, attempt.queue, attempt.started_at, attempt.finished_at, attempt.status,
                attempt.jobs_received, attempt.jobs_new, attempt.jobs_duplicate, attempt.jobs_filtered,
                attempt.latency_ms, attempt.error_type, attempt.detail, int(attempt.retryable),
                attempt.next_retry_at, attempt.cycle_id,
            ),
        )
        # Bound history per portal so this never grows unbounded at fleet scale.
        conn.execute(
            """DELETE FROM poll_attempts WHERE portal_type = ? AND portal_id = ? AND id NOT IN (
                 SELECT id FROM poll_attempts WHERE portal_type = ? AND portal_id = ?
                 ORDER BY started_at DESC LIMIT ?
               )""",
            (attempt.portal_type, attempt.portal_id, attempt.portal_type, attempt.portal_id, _MAX_ATTEMPTS_PER_PORTAL),
        )
        return cur.lastrowid


def get_attempt(attempt_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM poll_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return dict(row) if row else None


def list_attempts_for_portal(portal_type: str, portal_id: int, limit: int = 50) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM poll_attempts WHERE portal_type = ? AND portal_id = ? ORDER BY started_at DESC LIMIT ?",
            (portal_type, portal_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_recent_attempts(limit: int = 100, status: str = "", worker_id: str = "") -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if worker_id:
        clauses.append("worker_id = ?")
        params.append(worker_id)
    query = "SELECT * FROM poll_attempts"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def count_attempts_since(since_iso: str, status: str = "") -> int:
    query = "SELECT COUNT(*) AS c FROM poll_attempts WHERE started_at >= ?"
    params: list = [since_iso]
    if status:
        query += " AND status = ?"
        params.append(status)
    with db_session() as conn:
        return conn.execute(query, params).fetchone()["c"]


def sum_jobs_since(since_iso: str) -> dict:
    with db_session() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(jobs_received),0) AS received, COALESCE(SUM(jobs_new),0) AS new_jobs "
            "FROM poll_attempts WHERE started_at >= ? AND queue = 'poll'",
            (since_iso,),
        ).fetchone()
        return {"jobs_received": row["received"], "jobs_new": row["new_jobs"]}


def distinct_polled_portal_ids_since(since_iso: str, portal_type: str = "company_registry") -> set[int]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT DISTINCT portal_id FROM poll_attempts WHERE started_at >= ? AND portal_type = ? AND status = ?",
            (since_iso, portal_type, AttemptStatus.SUCCEEDED.value),
        ).fetchall()
        return {r["portal_id"] for r in rows}


# --- Dead letters ----------------------------------------------------------

def upsert_dead_letter(
    *, portal_type: str, portal_id: int, provider: str, reason: str, attempt_count: int,
    last_error: str, last_attempt_id: str,
) -> None:
    now = utcnow()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO dead_letters
                 (portal_type, portal_id, provider, reason, attempt_count, last_error, last_attempt_id,
                  created_at, updated_at, resolved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(portal_type, portal_id) WHERE resolved = 0 DO UPDATE SET
                 reason=excluded.reason, attempt_count=excluded.attempt_count,
                 last_error=excluded.last_error, last_attempt_id=excluded.last_attempt_id,
                 updated_at=excluded.updated_at""",
            (portal_type, portal_id, provider, reason, attempt_count, last_error, last_attempt_id, now, now),
        )


def list_dead_letters(resolved: bool = False, limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM dead_letters WHERE resolved = ? ORDER BY updated_at DESC LIMIT ?",
            (int(resolved), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_open_dead_letter(portal_type: str, portal_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM dead_letters WHERE portal_type = ? AND portal_id = ? AND resolved = 0",
            (portal_type, portal_id),
        ).fetchone()
        return dict(row) if row else None


def resolve_dead_letter(dead_letter_id: int) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE dead_letters SET resolved = 1, updated_at = ? WHERE id = ?", (utcnow(), dead_letter_id)
        )


def count_dead_letters(resolved: bool = False) -> int:
    with db_session() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM dead_letters WHERE resolved = ?", (int(resolved),)).fetchone()["c"]
