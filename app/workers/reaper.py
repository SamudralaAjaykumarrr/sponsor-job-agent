"""Orphan worker reaper (CLAUDE.md Phase 6 section 20).

If a worker's heartbeat goes stale (crash, killed -9, network partition from
a shared Postgres), it is marked OFFLINE and its work becomes reclaimable.
Deliberately NOT the mechanism that actually frees a stuck lease -- that is
still, exactly as in Phase 5, the lease's own `lease_expires_at`/
`verify_lease_expires_at` passing (app/workers/leasing.py). This module only
updates the `workers` row's status for dashboard/operator visibility ("this
worker looks dead") and is always driven by a configurable threshold, so
ordinary heartbeat jitter (a slow GC pause, a busy cycle that runs a little
past WORKER_HEARTBEAT_SECONDS) never gets misclassified as a crash -- the
threshold is deliberately several heartbeat intervals, not one."""

from datetime import datetime, timezone

from app.db import db_session
from app.workers import repo as workers_repo
from app.workers.models import WorkerStatus


def reap_orphans(*, stale_after_seconds: int) -> list[str]:
    """Marks every worker whose last heartbeat is older than
    `stale_after_seconds` (and isn't already STOPPED/OFFLINE) as OFFLINE.
    Returns the worker_ids just marked. Never touches leases directly --
    those recover purely through their own expiry, independent of this
    function ever running at all."""
    stale = workers_repo.list_stale_workers(older_than_seconds=stale_after_seconds)
    reaped = []
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        for w in stale:
            if w["status"] == WorkerStatus.OFFLINE.value:
                continue
            conn.execute(
                "UPDATE workers SET status = ?, updated_at = ? WHERE worker_id = ? AND last_heartbeat_at = ?",
                (WorkerStatus.OFFLINE.value, now, w["worker_id"], w["last_heartbeat_at"]),
            )
            reaped.append(w["worker_id"])
    return reaped
