"""One-click-application-experience-v1 (CLAUDE.md section J): calm, in-app
notifications for meaningful events only. This module is a thin, durable
read/unread log with a dedupe key -- it owns no business logic, no
lifecycle state, and no gating decision. It is only ever called from a
handful of already-existing, narrow choke points that already decided
something meaningful happened (a genuine blocker was raised, a receipt was
recorded, a rate limit blocked a submit, the orchestrator's own crash/lease
handling) -- never a new place that re-derives whether something is
"meaningful".

Every call is best-effort: a notification failing to write must never
interrupt the pipeline/orchestrator/executor code path that triggered it,
matching this project's existing convention for app.agent.run_state.
log_activity."""

from datetime import datetime, timezone
from typing import Optional

from app.db import db_session

KIND_NEEDS_YOU = "NEEDS_YOU"
KIND_APPLIED = "APPLIED"
KIND_STATUS_UNKNOWN = "STATUS_UNKNOWN"
KIND_AGENT_STOPPED = "AGENT_STOPPED"
KIND_DAILY_LIMIT = "DAILY_LIMIT"
KIND_HEALTH_ISSUE = "HEALTH_ISSUE"

_ALL_KINDS = frozenset({
    KIND_NEEDS_YOU, KIND_APPLIED, KIND_STATUS_UNKNOWN, KIND_AGENT_STOPPED, KIND_DAILY_LIMIT, KIND_HEALTH_ISSUE,
})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def notify(
    kind: str, title: str, message: str = "", *, dedupe_key: str = "",
    job_id: Optional[int] = None, execution_id: Optional[str] = None,
) -> Optional[dict]:
    """Best-effort insert, deduped against any already-UNREAD row sharing
    the same `dedupe_key` (never against read/dismissed ones -- a
    genuinely-recurring condition the user already acknowledged should be
    able to notify again). A blank dedupe_key never dedupes (every call
    produces its own row) -- callers with a genuine identity to dedupe on
    (an execution id, a job id + day) must always pass one."""
    if kind not in _ALL_KINDS:
        kind = KIND_HEALTH_ISSUE
    try:
        with db_session() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT id FROM notifications WHERE dedupe_key = ? AND read_at IS NULL LIMIT 1",
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    return None
            now = utcnow()
            cur = conn.execute(
                """INSERT INTO notifications (kind, title, message, job_id, execution_id, dedupe_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (kind, title[:200], (message or "")[:1000], job_id, execution_id, dedupe_key, now),
            )
            row = conn.execute("SELECT * FROM notifications WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row) if row else None
    except Exception:  # noqa: BLE001 -- a notification must never break the caller's real work
        return None


def list_notifications(*, unread_only: bool = False, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM notifications"
    if unread_only:
        query += " WHERE read_at IS NULL"
    query += " ORDER BY id DESC LIMIT ?"
    with db_session() as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]


def unread_count() -> int:
    with db_session() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM notifications WHERE read_at IS NULL").fetchone()["c"]


def mark_read(notification_id: int) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (utcnow(), notification_id),
        )


def mark_all_read() -> int:
    with db_session() as conn:
        cur = conn.execute("UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (utcnow(),))
        return cur.rowcount
