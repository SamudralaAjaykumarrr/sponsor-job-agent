"""Single authoritative product-facing stage + predicate layer, derived
LIVE from the existing two-layer state machine (app.models.ApplicationState
+ app.applications.models.ExecutionStatus) -- never a redefinition or a
third storage-level enum. Approval-gated-autonomy-v1 spec section 1 asks
for "one authoritative set of predicates for: ready_for_approval,
approved_for_submission, needs_user_action, submitted, confirmed" used by
both dashboard counts and dashboard lists so they can never disagree --
this module is that single definition, matching this project's existing
convention (app.pipeline_dashboard.is_actionable /
app.pipeline_dashboard._NEEDS_ACTION_QUERIES already play this exact role
for their own narrower questions).

ProductStage exists purely for display (labels, grouping) -- nothing here
is ever persisted; every function takes an already-fetched execution dict
(as returned by app.applications.repo.get_execution/
get_active_execution_for_job) and computes its answer fresh every call."""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.applications.models import ExecutionStatus


class ProductStage(str, Enum):
    DISCOVERED = "DISCOVERED"
    ELIGIBILITY_CHECKED = "ELIGIBILITY_CHECKED"
    JD_ANALYZED = "JD_ANALYZED"
    RESUME_GENERATING = "RESUME_GENERATING"
    RESUME_READY = "RESUME_READY"
    APPLICATION_PREPARING = "APPLICATION_PREPARING"
    FORM_FILLING = "FORM_FILLING"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    TRACKING = "TRACKING"
    # Exceptional states (spec section 1).
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_AUTH = "NEEDS_AUTH"
    NEEDS_CAPTCHA = "NEEDS_CAPTCHA"
    NEEDS_LEGAL_CONFIRMATION = "NEEDS_LEGAL_CONFIRMATION"
    IDENTITY_REVIEW_REQUIRED = "IDENTITY_REVIEW_REQUIRED"
    UNSUPPORTED_SUBMISSION = "UNSUPPORTED_SUBMISSION"
    SKIPPED = "SKIPPED"


# Genuine blockers only (spec section 15: "Do NOT use Needs Action merely
# for LIKELY sponsor / resume generated / ATS check complete / normal final
# review -- normal final review belongs in READY FOR APPROVAL"). Deliberately
# excludes SUBMISSION_READY and APPROVED, even though both may carry
# requires_user_action-shaped follow-up -- neither represents a blocker
# that stops automatic preparation; they are the intended stop points.
_GENUINE_BLOCKER_EXEC_STATUSES = frozenset({
    ExecutionStatus.NEEDS_USER_ACTION.value,
    ExecutionStatus.VALIDATION_REQUIRED.value,
    ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value,
})

_EXEC_TO_STAGE: dict[str, ProductStage] = {
    ExecutionStatus.QUEUED.value: ProductStage.APPLICATION_PREPARING,
    ExecutionStatus.STARTED.value: ProductStage.APPLICATION_PREPARING,
    ExecutionStatus.FORM_DISCOVERED.value: ProductStage.FORM_FILLING,
    ExecutionStatus.FORM_MAPPED.value: ProductStage.FORM_FILLING,
    ExecutionStatus.FORM_FILLED.value: ProductStage.FORM_FILLING,
    ExecutionStatus.VALIDATION_REQUIRED.value: ProductStage.NEEDS_USER_INPUT,
    ExecutionStatus.NEEDS_USER_ACTION.value: ProductStage.NEEDS_USER_INPUT,
    ExecutionStatus.SUBMISSION_READY.value: ProductStage.READY_FOR_APPROVAL,
    ExecutionStatus.APPROVED.value: ProductStage.APPROVED,
    ExecutionStatus.SUBMITTING.value: ProductStage.SUBMITTING,
    ExecutionStatus.SUBMITTED.value: ProductStage.SUBMITTED,
    ExecutionStatus.SUBMISSION_CONFIRMED.value: ProductStage.CONFIRMED,
    ExecutionStatus.APPLIED.value: ProductStage.CONFIRMED,
    ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value: ProductStage.SUBMISSION_STATUS_UNKNOWN,
    ExecutionStatus.SUBMISSION_FAILED.value: ProductStage.TRACKING,
    ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value: ProductStage.TRACKING,
    ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value: ProductStage.TRACKING,
    ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED.value: ProductStage.TRACKING,
    ExecutionStatus.WITHDRAWN.value: ProductStage.TRACKING,
    ExecutionStatus.JOB_NO_LONGER_ACTIVE.value: ProductStage.TRACKING,
}

# NEEDS_USER_ACTION carries a specific reason via validation.policy_reasons
# (app.applications.models.PolicyReason) -- refine the generic
# NEEDS_USER_INPUT stage into the specific exceptional stage the spec asks
# for (CAPTCHA/auth/legal) whenever that reason is present.
_POLICY_REASON_TO_STAGE: dict[str, ProductStage] = {
    "CAPTCHA_PRESENT": ProductStage.NEEDS_CAPTCHA,
    "MFA_REQUIRED": ProductStage.NEEDS_AUTH,
    "AUTH_REQUIRED": ProductStage.NEEDS_AUTH,
    "UNKNOWN_LEGAL_QUESTION": ProductStage.NEEDS_LEGAL_CONFIRMATION,
}

_STAGE_LABELS: dict[ProductStage, str] = {
    ProductStage.DISCOVERED: "Discovered",
    ProductStage.ELIGIBILITY_CHECKED: "Eligibility checked",
    ProductStage.JD_ANALYZED: "JD analyzed",
    ProductStage.RESUME_GENERATING: "Generating resume",
    ProductStage.RESUME_READY: "Resume ready",
    ProductStage.APPLICATION_PREPARING: "Preparing application",
    ProductStage.FORM_FILLING: "Filling application",
    ProductStage.READY_FOR_APPROVAL: "Ready for approval",
    ProductStage.APPROVED: "Approved",
    ProductStage.SUBMITTING: "Submitting",
    ProductStage.SUBMISSION_STATUS_UNKNOWN: "Submission status unknown",
    ProductStage.SUBMITTED: "Submitted",
    ProductStage.CONFIRMED: "Confirmed",
    ProductStage.TRACKING: "Tracking",
    ProductStage.NEEDS_USER_INPUT: "Needs your input",
    ProductStage.NEEDS_AUTH: "Needs sign-in",
    ProductStage.NEEDS_CAPTCHA: "Needs CAPTCHA",
    ProductStage.NEEDS_LEGAL_CONFIRMATION: "Needs legal confirmation",
    ProductStage.IDENTITY_REVIEW_REQUIRED: "Identity needs review",
    ProductStage.UNSUPPORTED_SUBMISSION: "Manual submission required",
    ProductStage.SKIPPED: "Skipped",
}


@dataclass
class StageInfo:
    stage: ProductStage
    label: str
    stale: bool = False
    stale_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"stage": self.stage.value, "label": self.label, "stale": self.stale, "stale_reasons": self.stale_reasons}


def compute_stage(execution: Optional[dict]) -> StageInfo:
    if execution is None:
        return StageInfo(ProductStage.DISCOVERED, _STAGE_LABELS[ProductStage.DISCOVERED])
    status = execution.get("status")
    if status == ExecutionStatus.NEEDS_USER_ACTION.value:
        try:
            reasons = json.loads(execution.get("policy_reasons") or "[]")
        except ValueError:
            reasons = []
        for r in reasons:
            if r in _POLICY_REASON_TO_STAGE:
                stage = _POLICY_REASON_TO_STAGE[r]
                return StageInfo(stage, _STAGE_LABELS[stage])
    stage = _EXEC_TO_STAGE.get(status, ProductStage.APPLICATION_PREPARING)
    return StageInfo(stage, _STAGE_LABELS[stage])


# --- authoritative predicates (spec section 1) ------------------------------

def ready_for_approval(execution: Optional[dict]) -> bool:
    """The ONE normal human gate this whole feature exists to implement."""
    return bool(execution) and execution.get("active") == 1 \
        and execution.get("status") == ExecutionStatus.SUBMISSION_READY.value


def approved_for_submission(execution: Optional[dict]) -> bool:
    return bool(execution) and execution.get("status") == ExecutionStatus.APPROVED.value


def needs_user_action(execution: Optional[dict]) -> bool:
    """Genuine blockers only (CAPTCHA/MFA/login/legal/unknown-answer/
    unknown-submission-status) -- deliberately excludes SUBMISSION_READY/
    APPROVED, matching spec section 15."""
    return bool(execution) and execution.get("active") == 1 \
        and execution.get("status") in _GENUINE_BLOCKER_EXEC_STATUSES


def submitted(execution: Optional[dict]) -> bool:
    return bool(execution) and execution.get("status") in (
        ExecutionStatus.SUBMITTED.value, ExecutionStatus.SUBMISSION_CONFIRMED.value, ExecutionStatus.APPLIED.value,
    )


def confirmed(execution: Optional[dict]) -> bool:
    return bool(execution) and execution.get("status") in (
        ExecutionStatus.APPLIED.value, ExecutionStatus.SUBMISSION_CONFIRMED.value,
    )
