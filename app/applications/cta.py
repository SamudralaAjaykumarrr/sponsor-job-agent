"""Universal Apply CTA view-model (application-action-experience-v1).

Single source of truth for what application-action button/label a user sees
for a given job, computed from its authoritative state. Every surface (Jobs
page, Dashboard, Job detail, Applications page) renders from this one
function's output, so the same underlying state can never show a different
label/action on two different pages.

Deliberately built ON TOP OF app.applications.product_state.compute_stage()
rather than re-deriving stage logic from raw ExecutionStatus values a
second time -- product_state is already the authoritative,
policy_reasons-driven CAPTCHA/auth/legal disambiguation (approval-gated-
autonomy-v1 spec section 1); duplicating that branching here would risk the
two silently disagreeing. This module's only job is the last mile:
stage -> user-facing label/style/action, plus the one thing product_state
doesn't cover (browser-assist session status refinement and the
pre-execution/skipped-job branches). Never expose a raw internal enum name
(ExecutionStatus/BrowserSessionStatus/ApplicationState/ProductStage)
directly to the user -- every branch below maps to a plain-language label.
This module never calls approve_and_apply() or any other mutating function
itself -- it is a pure read-side view-model."""

from dataclasses import dataclass
from typing import Optional

from app.applications.browser_session import BrowserSessionStatus, PAUSED_STATUSES
from app.applications.models import ExecutionStatus
from app.applications.product_state import ProductStage, compute_stage
from app.models import ApplicationState

STYLE_PRIMARY = "primary"      # the one dominant action to take right now
STYLE_PROGRESS = "progress"    # disabled, actively in-progress ("APPLYING...")
STYLE_WAITING = "waiting"      # disabled, no action needed yet ("Preparing...")
STYLE_SUCCESS = "success"      # terminal positive ("APPLIED (check)")
STYLE_SECONDARY = "secondary"  # a real, non-primary action (Continue, Check status, Answer)
STYLE_NONE = "none"            # no CTA at all -- reason text only (skipped/failed)

_PAUSED_VALUES = frozenset(s.value for s in PAUSED_STATUSES)


@dataclass
class JobCTA:
    label: str
    style: str
    action: dict
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {"label": self.label, "style": self.style, "action": self.action, "reason": self.reason}


def _link(href: str) -> dict:
    return {"type": "link", "href": href}


def _none_action() -> dict:
    return {"type": "none"}


def _approve_action(job_id: int) -> dict:
    return {"type": "approve", "job_id": job_id, "href": f"/jobs/{job_id}/applications/approve"}


_SKIP_REASONS: dict[str, str] = {
    ApplicationState.SKIPPED_NO_SPONSORSHIP.value: "Skipped -- no sponsorship.",
    ApplicationState.SKIPPED_SENIORITY.value: "Skipped -- seniority mismatch.",
    ApplicationState.SKIPPED_COMPENSATION.value: "Skipped -- compensation below threshold.",
    ApplicationState.SKIPPED_POOR_MATCH.value: "Skipped -- weak technical match.",
    ApplicationState.SKIPPED.value: "Skipped.",
}

# Only stages product_state's _EXEC_TO_STAGE actually collapses into
# TRACKING need the raw execution status to recover a specific reason --
# every other stage already carries enough information on its own.
_FAILED_LABELS: dict[str, str] = {
    ExecutionStatus.SUBMISSION_FAILED.value: "Application attempt failed.",
    ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value: "Attempt failed -- retryable.",
    ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value: "Application failed permanently.",
    ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED.value: "Blocked -- a duplicate application was detected.",
    ExecutionStatus.JOB_NO_LONGER_ACTIVE.value: "Job is no longer active.",
    ExecutionStatus.WITHDRAWN.value: "Application withdrawn.",
}

_BROWSER_PAUSE_LABELS: dict[str, str] = {
    BrowserSessionStatus.PAUSED_LOGIN_REQUIRED.value: "SIGN IN & CONTINUE",
    BrowserSessionStatus.PAUSED_MFA_REQUIRED.value: "SIGN IN & CONTINUE",
    BrowserSessionStatus.PAUSED_CAPTCHA.value: "COMPLETE CAPTCHA",
    BrowserSessionStatus.PAUSED_LEGAL_QUESTION.value: "REVIEW & CONFIRM",
}

# Plain-language labels for every stage product_state.compute_stage() can
# return, EXCEPT READY_FOR_APPROVAL, APPROVED, SUBMITTING, CONFIRMED, and
# TRACKING, which need job_id/browser_session/raw-status context and are
# handled explicitly in compute_apply_cta() below.
_WAITING_STAGE_LABELS: dict[ProductStage, str] = {
    ProductStage.DISCOVERED: "Preparing automatically...",
    ProductStage.ELIGIBILITY_CHECKED: "Preparing automatically...",
    ProductStage.JD_ANALYZED: "Preparing automatically...",
    ProductStage.RESUME_GENERATING: "Preparing automatically...",
    ProductStage.RESUME_READY: "Preparing automatically...",
    ProductStage.APPLICATION_PREPARING: "Preparing application...",
    ProductStage.FORM_FILLING: "Filling application...",
}

_NEEDS_ACTION_STAGE_LABELS: dict[ProductStage, str] = {
    ProductStage.NEEDS_USER_INPUT: "ANSWER & CONTINUE",
    ProductStage.NEEDS_AUTH: "SIGN IN & CONTINUE",
    ProductStage.NEEDS_CAPTCHA: "COMPLETE CAPTCHA",
    ProductStage.NEEDS_LEGAL_CONFIRMATION: "REVIEW & CONFIRM",
    ProductStage.IDENTITY_REVIEW_REQUIRED: "ANSWER & CONTINUE",
    ProductStage.UNSUPPORTED_SUBMISSION: "CONTINUE APPLICATION",
}


def _browser_session_cta(session: dict) -> Optional[JobCTA]:
    """A CTA driven by the browser-assist session's own structured status,
    which is fresher/more specific than the execution-level stage whenever a
    session is active. Returns None when the session doesn't override the
    default stage-driven CTA (still starting/discovering/actively filling)."""
    status = session.get("status")
    href = f"/applications/browser-sessions/{session['session_id']}"

    if status == BrowserSessionStatus.CONFIRMED.value:
        return None  # the execution-level APPLIED/CONFIRMED mirror already covers this
    if status in (BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value, BrowserSessionStatus.AWAITING_USER_SUBMIT.value):
        return JobCTA("CONTINUE APPLICATION", STYLE_SECONDARY, _link(href))
    if status in (BrowserSessionStatus.DUPLICATE_APPLICATION_DETECTED.value, BrowserSessionStatus.SUBMISSION_STATUS_UNKNOWN.value):
        return JobCTA("CHECK APPLICATION STATUS", STYLE_SECONDARY, _link(href))
    if status in _BROWSER_PAUSE_LABELS:
        return JobCTA(_BROWSER_PAUSE_LABELS[status], STYLE_SECONDARY, _link(href))
    if status in _PAUSED_VALUES:
        return JobCTA("ANSWER & CONTINUE", STYLE_SECONDARY, _link(href))
    if status in (BrowserSessionStatus.STARTING.value, BrowserSessionStatus.DISCOVERING.value, BrowserSessionStatus.ACTIVE.value):
        return JobCTA("Filling application...", STYLE_WAITING, _none_action())
    return None


def compute_apply_cta(
    job_id: int,
    application_state: Optional[str],
    *,
    execution: Optional[dict] = None,
    browser_session: Optional[dict] = None,
) -> JobCTA:
    """The one function every surface calls. `application_state` may be
    None when the caller already knows an application_executions row exists
    (e.g. the Applications page, where a skipped job never has one) --
    only used here for the pre-execution / skipped-job branches, which sit
    outside product_state.compute_stage()'s scope (it only ever looks at the
    execution row)."""

    if application_state in _SKIP_REASONS:
        return JobCTA("", STYLE_NONE, _none_action(), reason=_SKIP_REASONS[application_state])

    if execution is None:
        if application_state == ApplicationState.APPLIED.value:
            return JobCTA("APPLIED ✓", STYLE_SUCCESS, _link(f"/jobs/{job_id}"))
        if application_state == ApplicationState.REVIEW_REQUIRED.value:
            return JobCTA(
                "Preparing automatically...", STYLE_WAITING, _none_action(),
                reason="Historical sponsorship signal only -- verify before applying.",
            )
        if application_state == ApplicationState.CLAIM_VALIDATION_FAILED.value:
            return JobCTA(
                "", STYLE_NONE, _link(f"/jobs/{job_id}"),
                reason="Resume claim check failed -- needs manual review.",
            )
        return JobCTA("Preparing automatically...", STYLE_WAITING, _none_action())

    stage_info = compute_stage(execution)
    stage = stage_info.stage

    if stage in _WAITING_STAGE_LABELS:
        return JobCTA(_WAITING_STAGE_LABELS[stage], STYLE_WAITING, _none_action())

    if stage == ProductStage.READY_FOR_APPROVAL:
        return JobCTA("APPROVE & APPLY", STYLE_PRIMARY, _approve_action(job_id))

    if stage == ProductStage.APPROVED:
        if browser_session:
            bcta = _browser_session_cta(browser_session)
            if bcta is not None:
                return bcta
        return JobCTA(
            "CONTINUE APPLICATION", STYLE_SECONDARY, _link(f"/jobs/{job_id}#application-execution"),
            reason="Approved. Automated final submission is not verified for this provider.",
        )

    if stage in _NEEDS_ACTION_STAGE_LABELS:
        if browser_session:
            bcta = _browser_session_cta(browser_session)
            if bcta is not None:
                return bcta
        return JobCTA(_NEEDS_ACTION_STAGE_LABELS[stage], STYLE_SECONDARY, _link(f"/jobs/{job_id}#application-execution"))

    if stage == ProductStage.SUBMITTING or stage == ProductStage.SUBMITTED:
        return JobCTA("APPLYING...", STYLE_PROGRESS, _none_action())

    if stage == ProductStage.SUBMISSION_STATUS_UNKNOWN:
        return JobCTA("CHECK APPLICATION STATUS", STYLE_SECONDARY, _link(f"/jobs/{job_id}#application-execution"))

    if stage == ProductStage.CONFIRMED:
        return JobCTA("APPLIED ✓", STYLE_SUCCESS, _link(f"/jobs/{job_id}#application-execution"))

    if stage == ProductStage.TRACKING:
        raw_status = execution.get("status")
        return JobCTA("", STYLE_NONE, _link(f"/jobs/{job_id}"), reason=_FAILED_LABELS.get(raw_status, "Application needs review."))

    return JobCTA("Preparing automatically...", STYLE_WAITING, _none_action())
