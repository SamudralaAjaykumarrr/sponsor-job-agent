"""Application-lifecycle-exception-resume-v1: the durable, first-class
blocker record. Distinct from -- and layered strictly on top of, never a
replacement for -- the existing live-derived views
(app.applications.product_state.compute_stage/app.applications.cta.
compute_apply_cta), which stay unmodified. Those answer "what should the UI
show right now"; this module answers "what has ever blocked this
application, when, and how was it resolved" as a durable, queryable history.

Only one blocker may ever be unresolved (`resolved_at IS NULL`) for a given
execution at a time -- enforced by a partial unique index
(`idx_application_blockers_execution_active`), the same atomic "one active
thing per key" pattern this project already uses for
application_executions/browser_assist_sessions/resume_variants.
raise_blocker() is idempotent by construction: on a unique-violation it
re-fetches and returns the existing active row rather than read-then-write
racing against a concurrent caller (mirrors
app.resume_optimizer.repo.claim_variant()'s exact pattern) -- this is what
makes blocker creation safe to call from multiple concurrent workers/resume
attempts without any extra locking.

This module never decides gating (submit/no-submit, resumable/terminal
execution transitions) -- it only records what already happened elsewhere
(app.applications.executor/reconcile/browser_session). Raising or resolving
a blocker never itself changes an execution's or session's status."""

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.applications.models import PolicyReason
from app.db import db_session


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_blocker_id() -> str:
    return f"blk_{uuid.uuid4().hex}"


class BlockerClass(str, Enum):
    TERMINAL = "TERMINAL"
    RESUMABLE = "RESUMABLE"


class BlockerCode(str, Enum):
    JOB_EXPIRED = "JOB_EXPIRED"
    JOB_REMOVED = "JOB_REMOVED"
    APPLICATION_CLOSED = "APPLICATION_CLOSED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_AUTH = "NEEDS_AUTH"
    NEEDS_ACCOUNT_CREATION = "NEEDS_ACCOUNT_CREATION"
    NEEDS_EMAIL_VERIFICATION = "NEEDS_EMAIL_VERIFICATION"
    NEEDS_OTP = "NEEDS_OTP"
    NEEDS_CAPTCHA = "NEEDS_CAPTCHA"
    NEEDS_LEGAL_CONFIRMATION = "NEEDS_LEGAL_CONFIRMATION"
    UNSUPPORTED_FIELD = "UNSUPPORTED_FIELD"
    PROVIDER_UNSUPPORTED = "PROVIDER_UNSUPPORTED"
    APPLICATION_ERROR = "APPLICATION_ERROR"
    SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"


TERMINAL_CODES = frozenset({BlockerCode.JOB_EXPIRED, BlockerCode.JOB_REMOVED, BlockerCode.APPLICATION_CLOSED})


def blocker_class_for(code: BlockerCode) -> BlockerClass:
    return BlockerClass.TERMINAL if code in TERMINAL_CODES else BlockerClass.RESUMABLE


@dataclass(frozen=True)
class BlockerCopy:
    human_title: str
    human_message: str
    required_action: str  # a short verb-phrase the UI maps to its own button label


# Plain-language copy per code -- the ONLY place these strings live, so every
# surface (consumer board, application detail, admin dashboard) shows
# identical wording for the same blocker. Never expose `code.value` itself
# to the user; always render human_title/human_message/required_action.
_COPY: dict[BlockerCode, BlockerCopy] = {
    BlockerCode.JOB_EXPIRED: BlockerCopy(
        "Job no longer accepting applications",
        "This employer's posting has expired. We stopped before submitting anything.",
        "FIND_SIMILAR",
    ),
    BlockerCode.JOB_REMOVED: BlockerCopy(
        "Job posting was removed",
        "This job listing no longer exists. We stopped before submitting anything.",
        "FIND_SIMILAR",
    ),
    BlockerCode.APPLICATION_CLOSED: BlockerCopy(
        "Application closed",
        "This employer has closed applications for this role. We stopped before submitting anything.",
        "FIND_SIMILAR",
    ),
    BlockerCode.NEEDS_USER_INPUT: BlockerCopy(
        "Needs your answer",
        "This application has a question we can't answer for you.",
        "ANSWER_AND_CONTINUE",
    ),
    BlockerCode.NEEDS_AUTH: BlockerCopy(
        "Sign in required",
        "This employer's application requires you to sign in before continuing.",
        "SIGN_IN_AND_CONTINUE",
    ),
    BlockerCode.NEEDS_ACCOUNT_CREATION: BlockerCopy(
        "Account creation required",
        "This employer requires a candidate account before continuing.",
        "CREATE_ACCOUNT_AND_CONTINUE",
    ),
    BlockerCode.NEEDS_EMAIL_VERIFICATION: BlockerCopy(
        "Email verification required",
        "This employer needs you to verify your email before continuing.",
        "VERIFY_AND_CONTINUE",
    ),
    BlockerCode.NEEDS_OTP: BlockerCopy(
        "Verification code required",
        "This employer needs a one-time verification code before continuing.",
        "VERIFY_AND_CONTINUE",
    ),
    BlockerCode.NEEDS_CAPTCHA: BlockerCopy(
        "CAPTCHA required",
        "This application needs you to complete a CAPTCHA before continuing.",
        "COMPLETE_CAPTCHA",
    ),
    BlockerCode.NEEDS_LEGAL_CONFIRMATION: BlockerCopy(
        "Needs your confirmation",
        "This application has a legal/attestation question we never guess for you.",
        "REVIEW_AND_CONFIRM",
    ),
    BlockerCode.UNSUPPORTED_FIELD: BlockerCopy(
        "Can't fill this field automatically",
        "This application has a field we can't fill automatically (e.g. an unavailable file upload).",
        "CONTINUE_MANUALLY",
    ),
    BlockerCode.PROVIDER_UNSUPPORTED: BlockerCopy(
        "Manual submission required",
        "Automated final submission isn't verified for this employer's application system.",
        "CONTINUE_MANUALLY",
    ),
    BlockerCode.APPLICATION_ERROR: BlockerCopy(
        "Something went wrong",
        "We hit an unexpected problem preparing this application.",
        "CONTINUE_MANUALLY",
    ),
    BlockerCode.SUBMISSION_STATUS_UNKNOWN: BlockerCopy(
        "Submission status unknown",
        "We couldn't confirm whether this application actually went through. We will never resubmit automatically.",
        "CHECK_APPLICATION_STATUS",
    ),
}


def copy_for(code: BlockerCode) -> BlockerCopy:
    return _COPY[code]


# --- mapping helpers (pure, no side effects) --------------------------------

_POLICY_REASON_MAP: dict[str, BlockerCode] = {
    PolicyReason.CAPTCHA_PRESENT.value: BlockerCode.NEEDS_CAPTCHA,
    PolicyReason.MFA_REQUIRED.value: BlockerCode.NEEDS_OTP,
    PolicyReason.AUTH_REQUIRED.value: BlockerCode.NEEDS_AUTH,
    PolicyReason.ACCOUNT_CREATION_REQUIRED.value: BlockerCode.NEEDS_ACCOUNT_CREATION,
    PolicyReason.EMAIL_VERIFICATION_REQUIRED.value: BlockerCode.NEEDS_EMAIL_VERIFICATION,
    PolicyReason.UNKNOWN_LEGAL_QUESTION.value: BlockerCode.NEEDS_LEGAL_CONFIRMATION,
    PolicyReason.UNKNOWN_DEMOGRAPHIC_QUESTION.value: BlockerCode.NEEDS_USER_INPUT,
    PolicyReason.UNRESOLVED_REQUIRED_FIELD.value: BlockerCode.NEEDS_USER_INPUT,
    PolicyReason.FILE_UPLOAD_UNSUPPORTED.value: BlockerCode.UNSUPPORTED_FIELD,
    PolicyReason.PLATFORM_POLICY_RESTRICTED.value: BlockerCode.PROVIDER_UNSUPPORTED,
    PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED.value: BlockerCode.PROVIDER_UNSUPPORTED,
    PolicyReason.FORM_SCHEMA_CHANGED.value: BlockerCode.APPLICATION_ERROR,
    # NOT_ELIGIBLE / DUPLICATE / RATE_LIMITED deliberately unmapped -- each
    # already has its own correct, honest handling elsewhere (permanent
    # failure / duplicate-blocked / internal throttle), not a user-facing
    # blocker in this feature's sense.
}


def from_policy_reason(reason) -> Optional[BlockerCode]:
    value = reason.value if isinstance(reason, PolicyReason) else reason
    return _POLICY_REASON_MAP.get(value)


def from_policy_reasons(reasons: list) -> Optional[BlockerCode]:
    """First mapped reason wins -- deterministic given callers already
    produce a sorted/stable list (see MockATSProvider.validate())."""
    for r in reasons:
        code = from_policy_reason(r)
        if code is not None:
            return code
    return None


_ACCOUNT_CREATION_PHRASES = ("create an account", "create your account", "sign up", "register to apply", "new user")
_EMAIL_VERIFICATION_PHRASES = ("verify your email", "check your inbox", "confirmation code has been sent",
                               "verification link", "verify email address")

_BROWSER_STATUS_MAP: dict[str, BlockerCode] = {
    "PAUSED_CAPTCHA": BlockerCode.NEEDS_CAPTCHA,
    "PAUSED_MFA_REQUIRED": BlockerCode.NEEDS_OTP,
    "PAUSED_LEGAL_QUESTION": BlockerCode.NEEDS_LEGAL_CONFIRMATION,
    "PAUSED_UNKNOWN_FIELD": BlockerCode.NEEDS_USER_INPUT,
    "PAUSED_APPLY_ENTRY_UNRECOGNIZED": BlockerCode.NEEDS_USER_INPUT,
    "PAUSED_AMBIGUOUS_APPLY_CONTROL": BlockerCode.NEEDS_USER_INPUT,
    "PAUSED_JOB_IDENTITY_UNVERIFIED": BlockerCode.NEEDS_USER_INPUT,
    "PAUSED_PLATFORM_RESTRICTED": BlockerCode.PROVIDER_UNSUPPORTED,
    "PAUSED_UNSUPPORTED_SUBMISSION": BlockerCode.PROVIDER_UNSUPPORTED,
    "PAUSED_FORM_CHANGED": BlockerCode.APPLICATION_ERROR,
    "PAUSED_IFRAME_UNEXPECTED_HOST": BlockerCode.APPLICATION_ERROR,
    "PAUSED_JOB_IDENTITY_MISMATCH": BlockerCode.APPLICATION_ERROR,
    "DUPLICATE_APPLICATION_DETECTED": BlockerCode.APPLICATION_ERROR,
    "SUBMISSION_STATUS_UNKNOWN": BlockerCode.SUBMISSION_STATUS_UNKNOWN,
}

# Statuses that mean "no longer waiting on the user" -- an active blocker for
# the owning execution should be resolved when a session reaches one of
# these. CLOSED/EXPIRED (browser SESSION lifecycle, e.g. an abandoned tab
# timing out) are deliberately excluded: that is not evidence the underlying
# blocking condition was resolved, so a blocker in that state is left for a
# human/reprocessing attempt to resolve explicitly instead.
_BROWSER_UNBLOCKED_STATUSES = frozenset({
    "ACTIVE", "DISCOVERING", "STARTING", "READY_FOR_FINAL_SUBMIT", "AWAITING_USER_SUBMIT", "CONFIRMED",
})


def from_browser_session_status(status: str, page_text: str = "") -> Optional[BlockerCode]:
    """PAUSED_LOGIN_REQUIRED is refined by page text into the more specific
    NEEDS_ACCOUNT_CREATION/NEEDS_EMAIL_VERIFICATION when genuinely present,
    else the generic NEEDS_AUTH -- DOM/text-phrase based only, mirroring
    this project's existing CAPTCHA-detection convention (element/text
    evidence, never a guess). `page_text` is expected already-lowercased
    caller-captured text; lowercased again here defensively."""
    if status == "PAUSED_LOGIN_REQUIRED":
        lowered = (page_text or "").lower()
        if any(p in lowered for p in _ACCOUNT_CREATION_PHRASES):
            return BlockerCode.NEEDS_ACCOUNT_CREATION
        if any(p in lowered for p in _EMAIL_VERIFICATION_PHRASES):
            return BlockerCode.NEEDS_EMAIL_VERIFICATION
        return BlockerCode.NEEDS_AUTH
    return _BROWSER_STATUS_MAP.get(status)


def is_browser_status_unblocked(status: str) -> bool:
    return status in _BROWSER_UNBLOCKED_STATUSES


_JOB_INACTIVE_REASON_MAP: dict[str, BlockerCode] = {
    "EXPIRED": BlockerCode.JOB_EXPIRED,
    "REMOVED": BlockerCode.JOB_REMOVED,
    "CLOSED": BlockerCode.APPLICATION_CLOSED,
}


def from_job_inactive_reason(reason: Optional[str]) -> BlockerCode:
    """Defaults to JOB_EXPIRED when the provider can't be more specific --
    matches this feature's benchmark narrative ("the employer posting was
    detected as expired") without ever inventing a reason no provider
    actually supplied."""
    return _JOB_INACTIVE_REASON_MAP.get((reason or "").upper(), BlockerCode.JOB_EXPIRED)


# --- durable read/write operations ------------------------------------------

def raise_blocker(
    execution_id: str, job_id: int, code: BlockerCode, *,
    provider: str = "", detail: str = "", resume_checkpoint: Optional[dict] = None,
    attempt_id: str = "", source: str = "",
) -> dict:
    """Idempotent: if an unresolved blocker already exists for this
    execution, returns it unchanged rather than creating a duplicate (the
    partial unique index is the actual concurrency guard -- this never does
    a read-then-write check)."""
    existing = get_active_blocker_for_execution(execution_id)
    if existing is not None and existing["blocker_code"] == code.value:
        return existing

    copy = copy_for(code)
    blocker_id = new_blocker_id()
    now = utcnow()
    checkpoint_json = json.dumps(resume_checkpoint) if resume_checkpoint is not None else ""
    row = {
        "blocker_id": blocker_id, "execution_id": execution_id, "job_id": job_id,
        "blocker_code": code.value, "blocker_class": blocker_class_for(code).value,
        "human_title": copy.human_title, "human_message": copy.human_message,
        "required_action": copy.required_action, "provider": provider, "detail": detail[:2000],
        "resume_checkpoint": checkpoint_json, "attempt_id": attempt_id, "source": source, "created_at": now,
    }
    try:
        with db_session() as conn:
            # A prior unresolved row for a DIFFERENT code must be resolved
            # first (superseded) -- a fresh occurrence of a *different*
            # blocking condition is always a new row, never silently merged.
            conn.execute(
                "UPDATE application_blockers SET resolved_at = ?, resolution_note = ? "
                "WHERE execution_id = ? AND resolved_at IS NULL",
                (now, "superseded by a different blocker", execution_id),
            )
            conn.execute(
                """INSERT INTO application_blockers
                   (execution_id, job_id, blocker_code, blocker_class, human_title, human_message,
                    required_action, provider, detail, resume_checkpoint, attempt_id, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (execution_id, job_id, row["blocker_code"], row["blocker_class"], row["human_title"],
                 row["human_message"], row["required_action"], provider, row["detail"], checkpoint_json,
                 attempt_id, source, now),
            )
    except sqlite3.IntegrityError:
        refetched = get_active_blocker_for_execution(execution_id)
        if refetched is not None:
            return refetched
        raise
    except Exception as exc:  # noqa: BLE001 -- psycopg raises its own IntegrityError subclass
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            refetched = get_active_blocker_for_execution(execution_id)
            if refetched is not None:
                return refetched
        raise

    # One-click-application-experience-v1 (CLAUDE.md section J): a NEW
    # resumable blocker is a genuine "Needs You" (or, for the specific
    # SUBMISSION_STATUS_UNKNOWN code, "status unknown") notification --
    # best-effort, deduped per-execution so a retried/re-raised occurrence
    # of the SAME code never re-notifies while the prior one is still
    # unread. Terminal blockers (job expired/removed/application closed)
    # are deliberately not notified here -- they land in the consumer
    # board's Issues bucket, not the Needs You notification set this
    # section defines.
    if blocker_class_for(code) == BlockerClass.RESUMABLE:
        from app import notifications

        kind = notifications.KIND_STATUS_UNKNOWN if code == BlockerCode.SUBMISSION_STATUS_UNKNOWN \
            else notifications.KIND_NEEDS_YOU
        notifications.notify(
            kind, copy.human_title, copy.human_message,
            dedupe_key=f"blocker:{execution_id}:{code.value}", job_id=job_id, execution_id=execution_id,
        )
    return get_active_blocker_for_execution(execution_id) or row


def resolve_blocker(execution_id: str, *, resolution_note: str = "") -> Optional[dict]:
    """Resolves the current active blocker for an execution. Idempotent --
    a no-op returning None if there is nothing active to resolve. Returns
    the now-resolved row (never the execution's next active blocker, if a
    concurrent raise_blocker() happened to land in between)."""
    active = get_active_blocker_for_execution(execution_id)
    if active is None:
        return None
    now = utcnow()
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE application_blockers SET resolved_at = ?, resolution_note = ? "
            "WHERE id = ? AND resolved_at IS NULL",
            (now, resolution_note[:2000], active["id"]),
        )
        if cur.rowcount == 0:
            return None
    active["resolved_at"] = now
    active["resolution_note"] = resolution_note[:2000]
    return active


def get_active_blocker_for_execution(execution_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM application_blockers WHERE execution_id = ? AND resolved_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        return dict(row) if row else None


def list_blockers_for_execution(execution_id: str, limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_blockers WHERE execution_id = ? ORDER BY id ASC LIMIT ?",
            (execution_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_blockers_for_job(job_id: int, limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_blockers WHERE job_id = ? ORDER BY id ASC LIMIT ?", (job_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
