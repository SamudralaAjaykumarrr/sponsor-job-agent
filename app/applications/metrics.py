"""Application executor metrics (CLAUDE.md Phase 8 section 59, extended by
Phase 9 section 49). Every value is a live DB query at collection time, same
principle as app.observability.metrics. No PII -- counts only."""

from datetime import datetime, timedelta, timezone

from app.applications.worker_capabilities import WorkerCapability, has_capability
from app.db import db_session

_READY_STATES = ("SUBMISSION_READY",)
_QUEUED_STATES = ("QUEUED", "STARTED")
_PREPARED_STATES = ("FORM_DISCOVERED", "FORM_MAPPED", "FORM_FILLED", "VALIDATION_REQUIRED")
_NEEDS_ACTION_STATES = ("NEEDS_USER_ACTION", "VALIDATION_REQUIRED")
_SUBMITTED_STATES = ("SUBMITTED", "SUBMISSION_CONFIRMED", "APPLIED")
_CONFIRMED_STATES = ("SUBMISSION_CONFIRMED", "APPLIED")
_FAILED_STATES = ("SUBMISSION_FAILED", "RETRYABLE_SUBMISSION_FAILURE", "PERMANENT_SUBMISSION_FAILURE",
                   "JOB_NO_LONGER_ACTIVE")


def _count(conn, statuses: tuple[str, ...]) -> int:
    placeholders = ", ".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM application_executions WHERE status IN ({placeholders})", statuses,
    ).fetchone()["c"]


def collect() -> dict:
    with db_session() as conn:
        by_provider_rows = conn.execute(
            "SELECT provider, COUNT(*) AS c FROM application_executions GROUP BY provider"
        ).fetchall()
        return {
            "applications_ready": _count(conn, _READY_STATES),
            "applications_queued": _count(conn, _QUEUED_STATES),
            "applications_prepared": _count(conn, _PREPARED_STATES),
            "applications_needs_user_action": _count(conn, _NEEDS_ACTION_STATES),
            "applications_submitted": _count(conn, _SUBMITTED_STATES),
            "applications_confirmed": _count(conn, _CONFIRMED_STATES),
            "applications_failed": _count(conn, _FAILED_STATES),
            "applications_duplicate_blocked": _count(conn, ("DUPLICATE_APPLICATION_BLOCKED",)),
            "application_form_drift": conn.execute(
                "SELECT COUNT(*) AS c FROM application_audit_log WHERE detail = 'FORM_SCHEMA_CHANGED'"
            ).fetchone()["c"],
            "submission_unknown": _count(conn, ("SUBMISSION_STATUS_UNKNOWN",)),
            "applications_by_provider": {r["provider"] or "unknown": r["c"] for r in by_provider_rows},
        }


def collect_worker_fleet() -> dict:
    """CLAUDE.md Phase 9 section 49: application-worker-fleet-shaped
    observability, kept separate from collect() above (which is purely
    execution-status shaped) since these describe the WORKER/QUEUE side of
    the pipeline rather than individual application outcomes."""
    from app.applications import circuit as app_circuit
    from app.workers import repo as workers_repo

    now = datetime.now(timezone.utc)
    since_1h = (now - timedelta(hours=1)).isoformat()

    application_workers = [
        w for w in workers_repo.list_workers(limit=1000)
        if has_capability(w.get("capabilities") or "[]", WorkerCapability.APPLICATION_SUBMIT)
        or has_capability(w.get("capabilities") or "[]", WorkerCapability.APPLICATION_PREPARE)
    ]
    online_statuses = {"STARTING", "IDLE", "WORKING", "DEGRADED", "DRAINING"}
    workers_online = sum(1 for w in application_workers if w["status"] in online_statuses)
    workers_draining = sum(1 for w in application_workers if w["status"] == "DRAINING")

    with db_session() as conn:
        queue_depth = conn.execute(
            """SELECT COUNT(*) AS c FROM application_executions
               WHERE active = 1 AND status = 'QUEUED'
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
            (now.isoformat(),),
        ).fetchone()["c"]
        leases_active = conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions "
            "WHERE lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (now.isoformat(),),
        ).fetchone()["c"]
        attempts_total_1h = conn.execute(
            "SELECT COUNT(*) AS c FROM application_attempts WHERE started_at >= ?", (since_1h,),
        ).fetchone()["c"]
        attempts_failed_1h = conn.execute(
            "SELECT COUNT(*) AS c FROM application_attempts WHERE started_at >= ? "
            "AND result IN ('PERMANENT_SUBMISSION_FAILURE', 'SUBMISSION_STATUS_UNKNOWN', 'WORKER_EXCEPTION')",
            (since_1h,),
        ).fetchone()["c"]
        submissions_total_1h = conn.execute(
            "SELECT COUNT(*) AS c FROM application_audit_log WHERE event_type = 'submit_attempted' "
            "AND created_at >= ?", (since_1h,),
        ).fetchone()["c"]
        confirmations_total_1h = conn.execute(
            "SELECT COUNT(*) AS c FROM application_audit_log WHERE event_type = 'confirmed' AND created_at >= ?",
            (since_1h,),
        ).fetchone()["c"]
        duplicate_blocked_1h = conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions WHERE status = 'DUPLICATE_APPLICATION_BLOCKED' "
            "AND updated_at >= ?", (since_1h,),
        ).fetchone()["c"]

    return {
        "application_workers_online": workers_online,
        "application_workers_draining": workers_draining,
        "application_queue_depth": queue_depth,
        "application_leases_active": leases_active,
        "application_attempts_total_1h": attempts_total_1h,
        "application_attempts_failed_1h": attempts_failed_1h,
        "application_submissions_total_1h": submissions_total_1h,
        "application_confirmations_total_1h": confirmations_total_1h,
        "application_duplicate_blocked_total_1h": duplicate_blocked_1h,
        "application_provider_circuit_state": app_circuit.all_states(),
    }
