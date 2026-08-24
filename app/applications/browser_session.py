"""Persistent, resumable browser-assist session model (CLAUDE.md Phase 10
sections 4-8, 63). A session tracks ONE candidate's real-browser interaction
with ONE application form for ONE execution -- separate from, but linked to,
the Phase 8 `application_executions` row (browser_assist is an alternative/
complementary preparation path, not a replacement for the executor pipeline).

Never stores a password, MFA code, cookie, or raw auth token -- see the
column list in app.migrations._m025_browser_assist_sessions_table. `active`
mirrors application_executions' own pattern: 1 while the session is in any
non-terminal status, 0 once CONFIRMED/CLOSED/EXPIRED, backed by a partial
unique index so two workers/dashboard clicks can never both start a second
live session for the same job (section 63)."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from app.db import db_session


class BrowserSessionStatus(str, Enum):
    STARTING = "STARTING"
    DISCOVERING = "DISCOVERING"
    ACTIVE = "ACTIVE"
    PAUSED_LOGIN_REQUIRED = "PAUSED_LOGIN_REQUIRED"
    PAUSED_CAPTCHA = "PAUSED_CAPTCHA"
    PAUSED_MFA_REQUIRED = "PAUSED_MFA_REQUIRED"
    PAUSED_LEGAL_QUESTION = "PAUSED_LEGAL_QUESTION"
    PAUSED_UNKNOWN_FIELD = "PAUSED_UNKNOWN_FIELD"
    PAUSED_FORM_CHANGED = "PAUSED_FORM_CHANGED"
    PAUSED_PLATFORM_RESTRICTED = "PAUSED_PLATFORM_RESTRICTED"
    PAUSED_UNSUPPORTED_SUBMISSION = "PAUSED_UNSUPPORTED_SUBMISSION"
    # CLAUDE.md Phase 11 section 6: an apply-entry control was found but its
    # text didn't classify as NAVIGATION_SAFE (nor LOGIN_TRIGGER, which gets
    # its own PAUSED_LOGIN_REQUIRED) -- never clicked, always surfaced for a
    # human to look at and, if genuinely safe, click themselves.
    PAUSED_APPLY_ENTRY_UNRECOGNIZED = "PAUSED_APPLY_ENTRY_UNRECOGNIZED"
    # CLAUDE.md Phase 12 sections 14, 36-38: three new, distinct pause
    # reasons for SPA/dynamic-flow hardening -- never folded into the
    # existing PLATFORM_POLICY_RESTRICTED/APPLY_ENTRY_UNRECOGNIZED buckets,
    # so the dashboard/doctor can tell them apart.
    PAUSED_IFRAME_UNEXPECTED_HOST = "PAUSED_IFRAME_UNEXPECTED_HOST"
    PAUSED_AMBIGUOUS_APPLY_CONTROL = "PAUSED_AMBIGUOUS_APPLY_CONTROL"
    PAUSED_JOB_IDENTITY_MISMATCH = "PAUSED_JOB_IDENTITY_MISMATCH"
    # CLAUDE.md Phase 13 acceptance correction: distinct from a CONFIRMED
    # contradiction (PAUSED_JOB_IDENTITY_MISMATCH above) -- this is "we could
    # not establish enough confidence to proceed unattended" (a
    # JobIdentityVerdict of PROBABLE/AMBIGUOUS/INSUFFICIENT at the pre-
    # upload/pre-final-submit gate). Only a VERIFIED verdict skips this pause.
    PAUSED_JOB_IDENTITY_UNVERIFIED = "PAUSED_JOB_IDENTITY_UNVERIFIED"
    READY_FOR_FINAL_SUBMIT = "READY_FOR_FINAL_SUBMIT"
    AWAITING_USER_SUBMIT = "AWAITING_USER_SUBMIT"
    SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"
    # CLAUDE.md Phase 11 section 36: "you already applied" evidence is
    # deliberately never folded into CONFIRMED -- it means a submission
    # (possibly this one, possibly an earlier one) already exists somewhere,
    # which is a distinct fact from "this attempt just succeeded" and always
    # needs a human to reconcile which is true.
    DUPLICATE_APPLICATION_DETECTED = "DUPLICATE_APPLICATION_DETECTED"
    CONFIRMED = "CONFIRMED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    CRASHED_RECOVERABLE = "CRASHED_RECOVERABLE"


# Once a session reaches one of these, `active` flips to 0 -- exactly the
# same terminal/non-terminal split application_executions uses.
TERMINAL_SESSION_STATUSES = frozenset({
    BrowserSessionStatus.CONFIRMED, BrowserSessionStatus.CLOSED, BrowserSessionStatus.EXPIRED,
})

# Every PAUSED_* status is "waiting on the user", by construction.
PAUSED_STATUSES = frozenset({
    BrowserSessionStatus.PAUSED_LOGIN_REQUIRED, BrowserSessionStatus.PAUSED_CAPTCHA,
    BrowserSessionStatus.PAUSED_MFA_REQUIRED, BrowserSessionStatus.PAUSED_LEGAL_QUESTION,
    BrowserSessionStatus.PAUSED_UNKNOWN_FIELD, BrowserSessionStatus.PAUSED_FORM_CHANGED,
    BrowserSessionStatus.PAUSED_PLATFORM_RESTRICTED, BrowserSessionStatus.PAUSED_UNSUPPORTED_SUBMISSION,
    BrowserSessionStatus.PAUSED_APPLY_ENTRY_UNRECOGNIZED, BrowserSessionStatus.PAUSED_IFRAME_UNEXPECTED_HOST,
    BrowserSessionStatus.PAUSED_AMBIGUOUS_APPLY_CONTROL, BrowserSessionStatus.PAUSED_JOB_IDENTITY_MISMATCH,
    BrowserSessionStatus.PAUSED_JOB_IDENTITY_UNVERIFIED,
})


class BrowserPauseReason(str, Enum):
    """CLAUDE.md Phase 10 section 7's exact list."""
    CAPTCHA_PRESENT = "CAPTCHA_PRESENT"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    LEGAL_ATTESTATION = "LEGAL_ATTESTATION"
    UNKNOWN_REQUIRED_FIELD = "UNKNOWN_REQUIRED_FIELD"
    PLATFORM_POLICY_RESTRICTED = "PLATFORM_POLICY_RESTRICTED"
    FORM_CHANGED = "FORM_CHANGED"
    UNSUPPORTED_SUBMISSION = "UNSUPPORTED_SUBMISSION"
    APPLY_ENTRY_UNRECOGNIZED = "APPLY_ENTRY_UNRECOGNIZED"
    # CLAUDE.md Phase 12 sections 14, 36-38.
    IFRAME_UNEXPECTED_HOST = "IFRAME_UNEXPECTED_HOST"
    AMBIGUOUS_APPLY_CONTROL = "AMBIGUOUS_APPLY_CONTROL"
    JOB_IDENTITY_MISMATCH = "JOB_IDENTITY_MISMATCH"
    # CLAUDE.md Phase 13 acceptance correction (sections 4, 9-10): a
    # PROBABLE/AMBIGUOUS/INSUFFICIENT JobIdentityVerdict at the pre-upload/
    # pre-final-submit gate -- distinct from a confirmed JOB_IDENTITY_MISMATCH.
    JOB_IDENTITY_UNVERIFIED = "JOB_IDENTITY_UNVERIFIED"


# Maps a pause reason to the session status it produces -- kept as one
# explicit table so the mapping is never duplicated/drifted between callers.
REASON_TO_STATUS: dict[BrowserPauseReason, BrowserSessionStatus] = {
    BrowserPauseReason.CAPTCHA_PRESENT: BrowserSessionStatus.PAUSED_CAPTCHA,
    BrowserPauseReason.LOGIN_REQUIRED: BrowserSessionStatus.PAUSED_LOGIN_REQUIRED,
    BrowserPauseReason.MFA_REQUIRED: BrowserSessionStatus.PAUSED_MFA_REQUIRED,
    BrowserPauseReason.LEGAL_ATTESTATION: BrowserSessionStatus.PAUSED_LEGAL_QUESTION,
    BrowserPauseReason.UNKNOWN_REQUIRED_FIELD: BrowserSessionStatus.PAUSED_UNKNOWN_FIELD,
    BrowserPauseReason.PLATFORM_POLICY_RESTRICTED: BrowserSessionStatus.PAUSED_PLATFORM_RESTRICTED,
    BrowserPauseReason.FORM_CHANGED: BrowserSessionStatus.PAUSED_FORM_CHANGED,
    BrowserPauseReason.UNSUPPORTED_SUBMISSION: BrowserSessionStatus.PAUSED_UNSUPPORTED_SUBMISSION,
    BrowserPauseReason.APPLY_ENTRY_UNRECOGNIZED: BrowserSessionStatus.PAUSED_APPLY_ENTRY_UNRECOGNIZED,
    BrowserPauseReason.IFRAME_UNEXPECTED_HOST: BrowserSessionStatus.PAUSED_IFRAME_UNEXPECTED_HOST,
    BrowserPauseReason.AMBIGUOUS_APPLY_CONTROL: BrowserSessionStatus.PAUSED_AMBIGUOUS_APPLY_CONTROL,
    BrowserPauseReason.JOB_IDENTITY_MISMATCH: BrowserSessionStatus.PAUSED_JOB_IDENTITY_MISMATCH,
    BrowserPauseReason.JOB_IDENTITY_UNVERIFIED: BrowserSessionStatus.PAUSED_JOB_IDENTITY_UNVERIFIED,
}


class DuplicateSessionError(Exception):
    """Raised when a job already has an active browser-assist session --
    mirrors app.applications.repo.DuplicateExecutionError. The partial unique
    index on browser_assist_sessions(job_id) WHERE active=1 is the actual
    atomic guard; this exception is just how Python observes it firing."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def new_session_id() -> str:
    return f"bsess_{uuid.uuid4().hex}"


def _is_unique_violation(exc: BaseException) -> bool:
    name = type(exc).__name__
    return "IntegrityError" in name or "UniqueViolation" in name


def create_session(*, execution_id: str, job_id: int, provider: str, application_url: str) -> dict:
    session_id = new_session_id()
    now = utcnow()
    try:
        with db_session() as conn:
            conn.execute(
                """INSERT INTO browser_assist_sessions
                   (session_id, execution_id, job_id, provider, application_url, status, active,
                    current_step, created_at, updated_at, last_activity_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)""",
                (session_id, execution_id, job_id, provider, application_url,
                 BrowserSessionStatus.STARTING.value, now, now, now),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise DuplicateSessionError(f"job {job_id} already has an active browser-assist session") from exc
        raise
    return get_session(session_id)


def get_session(session_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM browser_assist_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None


def get_active_session_for_job(job_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM browser_assist_sessions WHERE job_id = ? AND active = 1", (job_id,)
        ).fetchone()
        return dict(row) if row else None


def get_active_sessions_for_jobs(job_ids: list[int]) -> dict[int, dict]:
    """Batched version of get_active_session_for_job for dashboard/CTA list
    rendering -- one query for N jobs, never N queries, matching
    app.applications.repo.get_active_executions_for_jobs's existing pattern
    (application-action-experience-v1: the Apply CTA needs a job's active
    browser-assist session, when one exists, without an N+1 query per row)."""
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    with db_session() as conn:
        rows = conn.execute(
            f"SELECT * FROM browser_assist_sessions WHERE job_id IN ({placeholders}) AND active = 1", job_ids
        ).fetchall()
        return {r["job_id"]: dict(r) for r in rows}


def list_sessions(*, status: Optional[str] = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM browser_assist_sessions"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def update_session(session_id: str, **fields) -> Optional[dict]:
    fields["updated_at"] = utcnow()
    status = fields.get("status")
    if status is not None:
        status_enum = BrowserSessionStatus(status) if not isinstance(status, BrowserSessionStatus) else status
        fields["status"] = status_enum.value
        if status_enum in TERMINAL_SESSION_STATUSES:
            fields["active"] = 0
            fields.setdefault("closed_at", utcnow())
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db_session() as conn:
        conn.execute(
            f"UPDATE browser_assist_sessions SET {set_clause} WHERE session_id = ?",
            [*fields.values(), session_id],
        )
    row = get_session(session_id)
    if row is not None and status is not None:
        _sync_blocker(row, fields.get("user_action_reason") or "")
    return row


def _sync_blocker(session: dict, page_text: str) -> None:
    """Application-lifecycle-exception-resume-v1: the ONE chokepoint every
    session status write funnels through, so app.applications.blockers stays
    in sync without touching any of browser_assist.py's ~16 call sites.
    Best-effort -- must never break a real session update."""
    from app.applications import blockers

    try:
        status_value = session["status"]
        code = blockers.from_browser_session_status(status_value, page_text)
        if code is not None:
            blockers.raise_blocker(
                session["execution_id"], session["job_id"], code, provider=session.get("provider") or "",
                detail=page_text[:2000], attempt_id=session.get("lease_attempt_id") or "",
                resume_checkpoint={"session_id": session["session_id"], "stage": session.get("stage") or ""},
                source="browser_session.update_session",
            )
        elif blockers.is_browser_status_unblocked(status_value):
            blockers.resolve_blocker(session["execution_id"], resolution_note=f"browser session reached {status_value}")
    except Exception:  # noqa: BLE001 -- observability sync must never break a real session update
        pass


def touch_activity(session_id: str) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE browser_assist_sessions SET last_activity_at = ?, updated_at = ? WHERE session_id = ?",
            (utcnow(), utcnow(), session_id),
        )


# --- Distributed ownership (CLAUDE.md Phase 10 section 63) ------------------
# Same atomic UPDATE ... WHERE (unleased OR lease-expired) claim pattern as
# app.applications.queue/app.workers.leasing -- correctness comes from the
# database's own single-writer serialization, never application-level
# locking.

def claim_session(session_id: str, *, worker_id: str, lease_seconds: int) -> Optional[dict]:
    """Atomic `UPDATE ... WHERE (unleased OR lease-expired OR already-mine)`
    -- the third condition (CLAUDE.md Phase 11 section 26) makes this safely
    RE-ENTRANT for the current owner (a single orchestration call in
    app.applications.browser_assist that internally delegates to another
    browser_assist function, e.g. mark_user_action_complete ->
    resume_session, must be able to re-claim/renew its own lease without
    that being treated as a conflict) while staying exactly as exclusive as
    Phase 10's original version for any OTHER worker_id."""
    now = utcnow()
    expires = (_utcnow_dt() + timedelta(seconds=lease_seconds)).isoformat()
    attempt_id = uuid.uuid4().hex
    with db_session() as conn:
        cur = conn.execute(
            """UPDATE browser_assist_sessions
               SET lease_owner = ?, lease_attempt_id = ?, lease_acquired_at = ?, lease_expires_at = ?,
                   worker_id = ?
               WHERE session_id = ? AND active = 1
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ? OR lease_owner = ?)""",
            (worker_id, attempt_id, now, expires, worker_id, session_id, now, worker_id),
        )
        if cur.rowcount != 1:
            return None
    return get_session(session_id)


def renew_session_lease(session_id: str, *, worker_id: str, lease_seconds: int) -> Optional[dict]:
    """CLAUDE.md Phase 11 section 26: a worker actively driving a live
    session extends its own lease periodically rather than losing ownership
    mid-interaction to a lease-expiry race. Only the CURRENT owner may renew
    -- same atomic `UPDATE ... WHERE lease_owner = ?` pattern as
    app.workers.queue's own lease-extension calls, never a
    read-then-write."""
    expires = (_utcnow_dt() + timedelta(seconds=lease_seconds)).isoformat()
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE browser_assist_sessions SET lease_expires_at = ? "
            "WHERE session_id = ? AND active = 1 AND lease_owner = ?",
            (expires, session_id, worker_id),
        )
        if cur.rowcount != 1:
            return None
    return get_session(session_id)


def release_session_lease(session_id: str) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE browser_assist_sessions SET lease_owner=NULL, lease_attempt_id=NULL, "
            "lease_acquired_at=NULL, lease_expires_at=NULL WHERE session_id = ?",
            (session_id,),
        )


def close_session(session_id: str, *, reason: str = "closed") -> Optional[dict]:
    return update_session(session_id, status=BrowserSessionStatus.CLOSED.value, user_action_reason=reason)


def expire_stale_sessions(*, timeout_minutes: int) -> list[dict]:
    """CLAUDE.md Phase 10 section 50: abandoned sessions become EXPIRED after
    a configurable timeout. Never auto-submits or deletes evidence -- only
    flips status/active so a fresh attempt can start cleanly; the row and its
    audit trail remain."""
    cutoff = (_utcnow_dt() - timedelta(minutes=timeout_minutes)).isoformat()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM browser_assist_sessions WHERE active = 1 AND last_activity_at < ?", (cutoff,),
        ).fetchall()
        expired = [dict(r) for r in rows]
    for row in expired:
        update_session(row["session_id"], status=BrowserSessionStatus.EXPIRED.value,
                        user_action_reason="expired: no activity for over "
                                            f"{timeout_minutes} minutes")
    return expired


@dataclass
class SessionSummary:
    """Convenience view for dashboard/CLI bucket counts (CLAUDE.md Phase 10
    sections 46, 48)."""
    active_sessions: int = 0
    paused_login: int = 0
    paused_captcha: int = 0
    paused_legal: int = 0
    paused_unknown_field: int = 0
    paused_form_changed: int = 0
    ready_for_submit: int = 0
    confirmation_unknown: int = 0
    confirmed: int = 0

    def as_dict(self) -> dict:
        return {
            "active_sessions": self.active_sessions, "paused_login": self.paused_login,
            "paused_captcha": self.paused_captcha, "paused_legal": self.paused_legal,
            "paused_unknown_field": self.paused_unknown_field, "paused_form_changed": self.paused_form_changed,
            "ready_for_submit": self.ready_for_submit, "confirmation_unknown": self.confirmation_unknown,
            "confirmed": self.confirmed,
        }


def summarize() -> SessionSummary:
    summary = SessionSummary()
    with db_session() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS c FROM browser_assist_sessions WHERE active = 1 "
                            "GROUP BY status").fetchall()
        counts = {r["status"]: r["c"] for r in rows}
        confirmed_row = conn.execute(
            "SELECT COUNT(*) AS c FROM browser_assist_sessions WHERE status = ?",
            (BrowserSessionStatus.CONFIRMED.value,),
        ).fetchone()
    summary.paused_login = counts.get(BrowserSessionStatus.PAUSED_LOGIN_REQUIRED.value, 0)
    summary.paused_captcha = counts.get(BrowserSessionStatus.PAUSED_CAPTCHA.value, 0)
    summary.paused_legal = counts.get(BrowserSessionStatus.PAUSED_LEGAL_QUESTION.value, 0)
    summary.paused_unknown_field = counts.get(BrowserSessionStatus.PAUSED_UNKNOWN_FIELD.value, 0)
    summary.paused_form_changed = counts.get(BrowserSessionStatus.PAUSED_FORM_CHANGED.value, 0)
    summary.ready_for_submit = counts.get(BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value, 0)
    summary.confirmation_unknown = counts.get(BrowserSessionStatus.SUBMISSION_STATUS_UNKNOWN.value, 0)
    summary.confirmed = confirmed_row["c"] if confirmed_row else 0
    summary.active_sessions = sum(counts.values())
    return summary
