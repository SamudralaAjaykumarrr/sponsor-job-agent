"""Application executor metrics (CLAUDE.md Phase 8 section 59). Every value
is a live DB query at collection time, same principle as
app.observability.metrics. No PII -- counts only."""

from app.db import db_session

_READY_STATES = ("SUBMISSION_READY",)
_QUEUED_STATES = ("QUEUED", "STARTED")
_PREPARED_STATES = ("FORM_DISCOVERED", "FORM_MAPPED", "FORM_FILLED", "VALIDATION_REQUIRED")
_NEEDS_ACTION_STATES = ("NEEDS_USER_ACTION", "VALIDATION_REQUIRED")
_SUBMITTED_STATES = ("SUBMITTED", "SUBMISSION_CONFIRMED", "APPLIED")
_CONFIRMED_STATES = ("SUBMISSION_CONFIRMED", "APPLIED")
_FAILED_STATES = ("SUBMISSION_FAILED", "RETRYABLE_SUBMISSION_FAILURE", "PERMANENT_SUBMISSION_FAILURE")


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
