"""Ready-for-Final-Review human hand-off (Tsenta-parity-closure-v1, P0#2).

The Tsenta parity audit correctly found that every real ATS provider has
`submission_supported=False` (app.applications.execution_contract) -- by
design, per app.applications.doctor's own capability-honesty rule (only a
genuine, explicitly-authorized real-employer canary run could ever justify
flipping it, and this project has never performed one). That is NOT changed
here. What this module closes is the observable gap around it: once an
execution reaches the maximum safe automated point for such a provider
(ExecutionStatus.APPROVED, or a browser-assist session paused specifically
because of PAUSED_UNSUPPORTED_SUBMISSION / PAUSED_PLATFORM_RESTRICTED --
see app.applications.cta's LABEL_READY_FOR_FINAL_REVIEW), there was
previously no way for the human to tell the app what they actually did
after opening the application themselves. app.applications.reconcile.
reconcile_execution() stays exactly as it was -- scoped strictly to
resolving SUBMISSION_STATUS_UNKNOWN executions, a distinct concern this
module never duplicates. This module is the analogous, separately-scoped
entry point for the READY FOR FINAL REVIEW hand-off moment specifically,
matching this project's existing pattern of several small, deliberately
separate human-action entry points (app.applications.approval.
approve_and_apply, app.applications.reconcile.reconcile_execution,
app.applications.browser_assist.attempt_user_submit_reconciliation).

Never fabricates a receipt or confirmation, and never marks an execution
APPLIED merely because the human opened the application -- SUBMITTED_
CONFIRMED requires the human to supply a real confirmation id or URL they
personally observed."""

from dataclasses import dataclass
from typing import Optional

from app.applications import blockers as _blockers
from app.applications import repo
from app.applications.models import ExecutionStatus
from app.applications.presubmit_manifest import PreSubmitManifest, build_manifest
from app.jobs_repo import get_job

# The two non-terminal "the agent has done everything it safely can -- it's
# on the human now" execution states. SUBMISSION_STATUS_UNKNOWN executions
# already have their own dedicated path (app.applications.reconcile) and are
# deliberately NOT included here, to avoid two mechanisms resolving the same
# row.
HANDOFF_ELIGIBLE_STATUSES = frozenset({
    ExecutionStatus.APPROVED.value, ExecutionStatus.NEEDS_USER_ACTION.value,
})

OUTCOME_SUBMITTED_CONFIRMED = "SUBMITTED_CONFIRMED"
OUTCOME_USER_COMPLETED_EXTERNALLY = "USER_COMPLETED_EXTERNALLY"
OUTCOME_SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"
OUTCOME_NOT_SUBMITTED = "NOT_SUBMITTED"

VALID_OUTCOMES = frozenset({
    OUTCOME_SUBMITTED_CONFIRMED, OUTCOME_USER_COMPLETED_EXTERNALLY,
    OUTCOME_SUBMISSION_STATUS_UNKNOWN, OUTCOME_NOT_SUBMITTED,
})


@dataclass
class HandoffOutcomeResult:
    ok: bool
    detail: str
    execution_status: str = ""


def is_ready_for_final_review(execution: Optional[dict]) -> bool:
    """True exactly when a human handoff outcome may be recorded for this
    execution -- the same eligibility record_manual_outcome() itself
    re-checks, exposed for read-side callers (templates/routes)."""
    return bool(execution) and execution.get("active") == 1 \
        and execution.get("status") in HANDOFF_ELIGIBLE_STATUSES


def build_final_review(job_id: int) -> Optional[PreSubmitManifest]:
    """The exact same read-only pre-submit manifest the pre-approval Final
    Review tab already renders -- reused, never recomputed, for the
    post-approval hand-off moment. Carries employer/title/provider, exact
    bound resume + cover letter (with sha256), unresolved required fields,
    high-risk pending questions, form fingerprint, and the provider's real
    submission-capability status."""
    return build_manifest(job_id, discover_form=False)


def record_manual_outcome(execution_id: str, outcome: str, *, confirmation_id: str = "",
                           confirmation_url: str = "", note: str = "") -> HandoffOutcomeResult:
    """The one place a human, after using "Open Application / Continue
    Manually" on a provider without verified auto-submit capability, tells
    the app what happened.

    outcome is one of:
      - SUBMITTED_CONFIRMED: the human observed genuine confirmation
        evidence themselves (a confirmation id or URL) -- marks
        ExecutionStatus.APPLIED, the same terminal state/job-state mirror
        every other genuine confirmation uses. REQUIRES a confirmation_id
        or confirmation_url; a bare click can never reach this outcome.
      - USER_COMPLETED_EXTERNALLY: the human says they finished the
        application themselves but supplied nothing independently
        verifiable. Recorded honestly as its own distinct state -- never
        treated as APPLIED/CONFIRMED, never given a receipt.
      - SUBMISSION_STATUS_UNKNOWN: outcome genuinely unclear -- hands the
        execution to the EXISTING app.applications.reconcile.
        reconcile_execution() queue for later resolution, never a second
        parallel reconciliation mechanism.
      - NOT_SUBMITTED: the human did not submit (decided not to, or
        couldn't) -- reuses the existing WITHDRAWN terminal state, exactly
        like reconcile_execution()'s own "confirmed_not_submitted"
        resolution, so the job can be cleanly re-queued.
    """
    if outcome not in VALID_OUTCOMES:
        return HandoffOutcomeResult(False, f"unknown outcome '{outcome}'")

    execution = repo.get_execution(execution_id)
    if execution is None:
        return HandoffOutcomeResult(False, f"execution {execution_id} not found")
    if not is_ready_for_final_review(execution):
        return HandoffOutcomeResult(
            False,
            f"execution is {execution['status']} (active={execution.get('active')}) -- not eligible for a "
            "manual hand-off outcome (expected an active APPROVED or NEEDS_USER_ACTION execution)",
        )

    job_id = execution["job_id"]
    job = get_job(job_id)
    if job is None:
        return HandoffOutcomeResult(False, f"job {job_id} not found")

    if outcome == OUTCOME_SUBMITTED_CONFIRMED:
        if not confirmation_id and not confirmation_url:
            return HandoffOutcomeResult(
                False, "SUBMITTED_CONFIRMED requires a confirmation id or URL you observed yourself",
            )
        repo.update_execution(
            execution_id, job_id, ExecutionStatus.APPLIED,
            confirmation_id=confirmation_id, confirmation_url=confirmation_url,
            user_action_reason=note or "confirmed submitted (manual hand-off)", requires_user_action=0,
        )
        repo.log_event(execution_id, job_id, "confirmed", detail="handoff:submitted_confirmed")
        _blockers.resolve_blocker(
            execution_id, resolution_note="handoff:submitted_confirmed" + (f" -- {note}" if note else ""),
        )
        return HandoffOutcomeResult(True, "marked APPLIED", ExecutionStatus.APPLIED.value)

    if outcome == OUTCOME_USER_COMPLETED_EXTERNALLY:
        repo.update_execution(
            execution_id, job_id, ExecutionStatus.USER_COMPLETED_EXTERNALLY,
            user_action_reason=note or "you told us you completed this application yourself", requires_user_action=0,
        )
        repo.log_event(execution_id, job_id, "user_completed_externally", detail="handoff:completed_externally")
        _blockers.resolve_blocker(
            execution_id, resolution_note="handoff:completed_externally" + (f" -- {note}" if note else ""),
        )
        return HandoffOutcomeResult(
            True, "marked completed by you (self-reported, unverified)", ExecutionStatus.USER_COMPLETED_EXTERNALLY.value,
        )

    if outcome == OUTCOME_SUBMISSION_STATUS_UNKNOWN:
        repo.update_execution(
            execution_id, job_id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN,
            user_action_reason=note or "outcome unclear after manual hand-off", requires_user_action=1,
        )
        repo.log_event(execution_id, job_id, "status_unknown", detail="handoff:status_unknown")
        return HandoffOutcomeResult(
            True, "marked submission status unknown -- queued for reconciliation",
            ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value,
        )

    # OUTCOME_NOT_SUBMITTED
    repo.update_execution(
        execution_id, job_id, ExecutionStatus.WITHDRAWN,
        user_action_reason=note or "not submitted (manual hand-off)", requires_user_action=0,
    )
    repo.log_event(execution_id, job_id, "manually_applied", detail="handoff:not_submitted")
    _blockers.resolve_blocker(
        execution_id, resolution_note="handoff:not_submitted" + (f" -- {note}" if note else ""),
    )
    return HandoffOutcomeResult(True, "marked not submitted -- job may be re-queued", ExecutionStatus.WITHDRAWN.value)
