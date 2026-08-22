"""Operator actions for the application worker fleet (CLAUDE.md Phase 9
section 13). All explicit, human-triggered actions -- never automatic,
mirroring app.workers.repo.mark_worker_offline's own "never called
automatically" contract."""

from datetime import datetime, timezone

from app.db import db_session
from app.workers.models import WorkerStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_drain(worker_id: str) -> bool:
    """Flips a live (non-terminal) worker into DRAINING. The worker itself
    polls its own row (see app.applications.worker.ApplicationWorker.
    _is_draining) and reacts by no longer claiming new work or starting new
    submissions -- this function never touches leases directly."""
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE workers SET status = ?, updated_at = ? WHERE worker_id = ? "
            "AND status NOT IN (?, ?)",
            (WorkerStatus.DRAINING.value, utcnow(), worker_id, WorkerStatus.STOPPED.value, WorkerStatus.OFFLINE.value),
        )
        return cur.rowcount == 1


def resume_from_drain(worker_id: str) -> bool:
    """Explicit operator action to cancel a drain request before the worker
    process is actually stopped/replaced."""
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE workers SET status = ?, updated_at = ? WHERE worker_id = ? AND status = ?",
            (WorkerStatus.IDLE.value, utcnow(), worker_id, WorkerStatus.DRAINING.value),
        )
        return cur.rowcount == 1
