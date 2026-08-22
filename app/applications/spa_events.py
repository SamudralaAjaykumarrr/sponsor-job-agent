"""Append-only structured event log for SPA/dynamic-flow observations
(CLAUDE.md Phase 12 sections 70-71). `app.applications.metrics.
collect_phase12()` is the only reader most callers need -- this module is
just the write path, kept tiny and dependency-free (no Playwright import)
so it can be called from both `app.applications.browser_runtime` (the one
module that actually drives a page) and `app.applications.browser_assist`
(orchestration) without either importing the other for this purpose.

Never a column for a candidate field VALUE -- only ids (session/execution/
job/provider/tenant), a stage/event/result label, a short free-text detail
(never a raw page snapshot), and a duration."""

from datetime import datetime, timezone

from app.db import db_session

# CLAUDE.md Phase 12 section 70's exact metric-shaped event names, plus a
# couple of structural ones (workday_observation_recorded,
# capability_live_revalidation) sections 41/54 need a durable record of.
EVENT_APPLY_CONTROL_DETECTED = "apply_control_detected"
EVENT_APPLY_CONTROL_UNKNOWN = "apply_control_unknown"
EVENT_TRUSTED_REDIRECT = "trusted_ats_redirect"
EVENT_BLOCKED_REDIRECT = "blocked_redirect"
EVENT_SPA_ROUTE_DETECTED = "spa_route_detected"
EVENT_DYNAMIC_FORM_DETECTED = "dynamic_form_detected"
EVENT_DYNAMIC_FORM_TIMEOUT = "dynamic_form_timeout"
EVENT_IFRAME_FORM_DETECTED = "iframe_form_detected"
EVENT_IFRAME_UNEXPECTED_HOST = "iframe_unexpected_host"
EVENT_SHADOW_FORM_DETECTED = "shadow_form_detected"
EVENT_WORKDAY_OBSERVATION_RECORDED = "workday_observation_recorded"
EVENT_WORKDAY_VARIABLE_OBSERVATION = "workday_variable_observation"
EVENT_CAPABILITY_LIVE_REVALIDATION = "capability_live_revalidation"
EVENT_STAGE_TRANSITION_INVALID = "stage_transition_invalid"
EVENT_JOB_IDENTITY_MISMATCH = "job_identity_mismatch"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(
    event: str, *, session_id: str = "", execution_id: str = "", job_id: int = None, provider: str = "",
    tenant: str = "", stage: str = "", result: str = "", detail: str = "", duration_ms: int = None,
) -> None:
    """Best-effort append -- never raises into the caller's discovery/fill
    pass (an observability write must never break a real browser session),
    matching every other 'best-effort, never raises' helper in this
    project's browser-assist layer."""
    try:
        with db_session() as conn:
            conn.execute(
                """INSERT INTO browser_spa_events
                   (session_id, execution_id, job_id, provider, tenant, stage, event, result, detail,
                    duration_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, execution_id, job_id, provider, tenant, stage, event, result, detail[:500],
                 duration_ms, utcnow()),
            )
    except Exception:  # noqa: BLE001 -- observability must never break the caller
        pass


def list_events(event: str = "", *, session_id: str = "", limit: int = 200) -> list[dict]:
    query = "SELECT * FROM browser_spa_events WHERE 1=1"
    params: list = []
    if event:
        query += " AND event = ?"
        params.append(event)
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def count(event: str) -> int:
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM browser_spa_events WHERE event = ?", (event,),
        ).fetchone()["c"]
