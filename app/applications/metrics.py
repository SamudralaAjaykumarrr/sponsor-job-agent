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


def collect_browser_assist() -> dict:
    """CLAUDE.md Phase 10 section 61. Every value is a live DB query -- never
    cached, never PII. `browser_assist_sessions_active` counts every
    non-terminal session (ACTIVE + every PAUSED_* + READY_FOR_FINAL_SUBMIT +
    AWAITING_USER_SUBMIT + SUBMISSION_STATUS_UNKNOWN), matching
    browser_assist_sessions.active=1 exactly."""
    from app.applications import browser_runtime
    from app.applications.browser_session import BrowserSessionStatus

    with db_session() as conn:
        active_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE active = 1"
        ).fetchone()["c"]
        paused_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE active = 1 AND status LIKE 'PAUSED_%'"
        ).fetchone()["c"]
        login_required = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.PAUSED_LOGIN_REQUIRED.value,),
        ).fetchone()["c"]
        captcha_required = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.PAUSED_CAPTCHA.value,),
        ).fetchone()["c"]
        ready_for_submit = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value,),
        ).fetchone()["c"]
        confirmation_unknown = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.SUBMISSION_STATUS_UNKNOWN.value,),
        ).fetchone()["c"]
        confirmed_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.CONFIRMED.value,),
        ).fetchone()["c"]
        form_drift_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.PAUSED_FORM_CHANGED.value,),
        ).fetchone()["c"]
        failures_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.PAUSED_PLATFORM_RESTRICTED.value,),
        ).fetchone()["c"]

    return {
        "browser_assist_sessions_active": active_total,
        "browser_assist_sessions_paused": paused_total,
        "browser_assist_login_required": login_required,
        "browser_assist_captcha_required": captcha_required,
        "browser_assist_ready_for_submit": ready_for_submit,
        "browser_assist_confirmation_unknown": confirmation_unknown,
        "browser_assist_confirmed": confirmed_total,
        "browser_assist_form_drift": form_drift_total,
        "browser_assist_failures": failures_total,
        "browser_assist_live_in_process": browser_runtime.active_count(),
    }


def collect_phase11() -> dict:
    """CLAUDE.md Phase 11 section 53. Same principle as every other
    function in this module: every value is a live query over PERSISTED
    state, never an in-memory incrementing counter (so it survives process
    restarts and is correct fleet-wide the moment DATABASE_URL points at
    shared Postgres) -- e.g. `apply_entry_detected_total` counts sessions
    whose `apply_entry_clicked` flag is set, not an event log of clicks."""
    from app.applications.capability_evidence import list_stale

    with db_session() as conn:
        apply_entry_detected_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE apply_entry_clicked = 1"
        ).fetchone()["c"]
        apply_entry_failed_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions "
            "WHERE entry_detection_result IN ('USER_ACTION_REQUIRED', 'UNSUPPORTED', 'REDIRECT_REQUIRED')"
        ).fetchone()["c"]
        reconstructed_total = conn.execute(
            "SELECT COALESCE(SUM(reconstructed_count), 0) AS c FROM browser_assist_sessions"
        ).fetchone()["c"]
        owner_conflicts = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions "
            "WHERE lease_owner IS NOT NULL AND worker_id != lease_owner"
        ).fetchone()["c"]
        step_progress_detected = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE step_confidence = 'EXACT'"
        ).fetchone()["c"]
        ready_for_final_submit = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = 'READY_FOR_FINAL_SUBMIT'"
        ).fetchone()["c"]
        manual_submit_confirmed = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = 'CONFIRMED'"
        ).fetchone()["c"]
        manual_submit_unknown = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = 'SUBMISSION_STATUS_UNKNOWN'"
        ).fetchone()["c"]
        real_form_validation_total = conn.execute(
            "SELECT COUNT(*) AS c FROM capability_evidence_records"
        ).fetchone()["c"]
        real_form_validation_failed = conn.execute(
            "SELECT COUNT(*) AS c FROM capability_evidence_records WHERE verification_type = 'NOT_TESTED'"
        ).fetchone()["c"]

    return {
        "apply_entry_detected_total": apply_entry_detected_total,
        "apply_entry_failed_total": apply_entry_failed_total,
        "browser_sessions_reconstructed_total": reconstructed_total,
        "browser_session_owner_conflicts": owner_conflicts,
        "real_form_validation_total": real_form_validation_total,
        "real_form_validation_failed": real_form_validation_failed,
        "step_progress_detected": step_progress_detected,
        "ready_for_final_submit": ready_for_final_submit,
        "manual_submit_confirmed": manual_submit_confirmed,
        "manual_submit_unknown": manual_submit_unknown,
        "capability_evidence_stale": len(list_stale()),
    }


def collect_phase12() -> dict:
    """CLAUDE.md Phase 12 section 70. Every value is a live query over
    PERSISTED state (`browser_spa_events`/`workday_tenant_attempts`/
    `capability_evidence_records`), same 'never an in-memory counter'
    principle as `collect_phase11()` above."""
    from app.applications import spa_events
    from app.applications.workday_tenant import WorkdayStability, stability_report

    with db_session() as conn:
        trusted_ats_redirects = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_spa_events WHERE event = ?",
            (spa_events.EVENT_TRUSTED_REDIRECT,),
        ).fetchone()["c"]
        blocked_redirects = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_spa_events WHERE event = ?",
            (spa_events.EVENT_BLOCKED_REDIRECT,),
        ).fetchone()["c"]
        spa_apply_controls_detected = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_spa_events WHERE event = ?",
            (spa_events.EVENT_APPLY_CONTROL_DETECTED,),
        ).fetchone()["c"]
        spa_apply_controls_unknown = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_spa_events WHERE event = ?",
            (spa_events.EVENT_APPLY_CONTROL_UNKNOWN,),
        ).fetchone()["c"]
        spa_routes_detected = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_spa_events WHERE event = ?",
            (spa_events.EVENT_SPA_ROUTE_DETECTED,),
        ).fetchone()["c"]
        dynamic_forms_detected = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE stage = 'APPLICATION_FORM'"
        ).fetchone()["c"]
        dynamic_form_timeouts = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_spa_events WHERE event = ?",
            (spa_events.EVENT_DYNAMIC_FORM_TIMEOUT,),
        ).fetchone()["c"]
        iframe_forms_detected = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE iframe_used = 1"
        ).fetchone()["c"]
        shadow_forms_detected = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE shadow_dom_used = 1"
        ).fetchone()["c"]
        workday_observations = conn.execute(
            "SELECT COUNT(*) AS c FROM workday_tenant_attempts"
        ).fetchone()["c"]
        capability_live_revalidations = conn.execute(
            "SELECT COUNT(*) AS c FROM capability_evidence_records WHERE repeat_count > 1"
        ).fetchone()["c"]
        smartrecruiters_form_verified = conn.execute(
            "SELECT COUNT(*) AS c FROM capability_evidence_records WHERE provider = 'smartrecruiters' "
            "AND verification_type IN ('REAL_BROWSER', 'REAL_BROWSER_REPEATED', 'LIVE_PUBLIC')"
        ).fetchone()["c"]

    workday_variable_observations = sum(
        1 for s in stability_report() if s.stability == WorkdayStability.VARIABLE
    )

    return {
        "spa_apply_controls_detected": spa_apply_controls_detected,
        "spa_apply_controls_unknown": spa_apply_controls_unknown,
        "trusted_ats_redirects": trusted_ats_redirects,
        "blocked_redirects": blocked_redirects,
        "spa_routes_detected": spa_routes_detected,
        "dynamic_forms_detected": dynamic_forms_detected,
        "dynamic_form_timeouts": dynamic_form_timeouts,
        "iframe_forms_detected": iframe_forms_detected,
        "shadow_forms_detected": shadow_forms_detected,
        "workday_observations": workday_observations,
        "workday_variable_observations": workday_variable_observations,
        "smartrecruiters_form_verified": smartrecruiters_form_verified,
        "capability_live_revalidations": capability_live_revalidations,
    }


def collect_phase13() -> dict:
    """CLAUDE.md Phase 13 section 63's exact metric-shaped names. Every value
    is a live query over PERSISTED state, same principle as every other
    collect_* function in this module."""
    from app.applications.provider_health import ProviderAssistHealth, list_health

    with db_session() as conn:
        job_identity_verified_total = conn.execute(
            "SELECT COUNT(*) AS c FROM job_identity_verifications WHERE result = 'VERIFIED'"
        ).fetchone()["c"]
        job_identity_mismatch_total = conn.execute(
            "SELECT COUNT(*) AS c FROM job_identity_verifications WHERE result = 'MISMATCH'"
        ).fetchone()["c"]
        job_identity_unverified_total = conn.execute(
            "SELECT COUNT(*) AS c FROM job_identity_verifications WHERE result IN "
            "('PROBABLE', 'AMBIGUOUS', 'INSUFFICIENT')"
        ).fetchone()["c"]
        provider_canary_runs_total = conn.execute(
            "SELECT COUNT(*) AS c FROM provider_canary_runs"
        ).fetchone()["c"]
        provider_canary_failures_total = conn.execute(
            "SELECT COUNT(*) AS c FROM provider_canary_runs WHERE ok = 0"
        ).fetchone()["c"]
        provider_schema_drift_total = conn.execute(
            "SELECT COALESCE(SUM(schema_drift_count), 0) AS c FROM application_provider_health"
        ).fetchone()["c"]
        captcha_handoffs_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = 'PAUSED_CAPTCHA'"
        ).fetchone()["c"]
        login_handoffs_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status IN "
            "('PAUSED_LOGIN_REQUIRED', 'PAUSED_MFA_REQUIRED')"
        ).fetchone()["c"]
        applications_closed_before_submit = conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions WHERE status = 'JOB_NO_LONGER_ACTIVE'"
        ).fetchone()["c"]
        session_reconstructions_total = conn.execute(
            "SELECT COALESCE(SUM(reconstructed_count), 0) AS c FROM browser_assist_sessions"
        ).fetchone()["c"]
        confirmation_strong_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE confirmation_evidence_strength = 'STRONG'"
        ).fetchone()["c"]
        confirmation_unknown_total = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = 'SUBMISSION_STATUS_UNKNOWN'"
        ).fetchone()["c"]

    health_rows = list_health()
    provider_capability_stale = sum(1 for e in health_rows if e["health"] == ProviderAssistHealth.STALE.value)

    return {
        "job_identity_verified_total": job_identity_verified_total,
        "job_identity_mismatch_total": job_identity_mismatch_total,
        "job_identity_unverified_total": job_identity_unverified_total,
        "provider_assist_health": {
            f"{e['row']['provider']}/{e['row']['tenant']}/{e['row']['site']}": e["health"] for e in health_rows
        },
        "provider_canary_runs_total": provider_canary_runs_total,
        "provider_canary_failures_total": provider_canary_failures_total,
        "provider_capability_stale": provider_capability_stale,
        "provider_schema_drift_total": provider_schema_drift_total,
        "captcha_handoffs_total": captcha_handoffs_total,
        "login_handoffs_total": login_handoffs_total,
        "applications_closed_before_submit": applications_closed_before_submit,
        "session_reconstructions_total": session_reconstructions_total,
        "confirmation_strong_total": confirmation_strong_total,
        "confirmation_unknown_total": confirmation_unknown_total,
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
