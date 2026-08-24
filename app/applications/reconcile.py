"""Reconciliation for SUBMISSION_STATUS_UNKNOWN executions (CLAUDE.md Phase 8
sections 33, 36). Always an explicit, human-driven operator action -- never
automatic, matching the same philosophy as app.workers.dead_letter.requeue().
"""

from dataclasses import dataclass

from app.applications import blockers, repo
from app.applications.models import ExecutionStatus
from app.jobs_repo import get_job


@dataclass
class ReconcileResult:
    ok: bool
    detail: str


def reconcile_execution(execution_id: str, resolution: str, *, confirmation_id: str = "",
                         confirmation_url: str = "", note: str = "") -> ReconcileResult:
    """`resolution` is one of:
      - "confirmed_applied": operator found independent evidence the
        application WAS actually submitted (e.g. checked the ATS/email
        directly) -- marks APPLIED with the supplied confirmation.
      - "confirmed_not_submitted": operator confirmed it was NOT submitted --
        marks the execution WITHDRAWN and allows a fresh queue_application()
        call to try again cleanly.
      - "manual_applied": operator applied manually outside the executor
        entirely (CLAUDE.md section 43 "Mark Applied Manually") -- same
        effect as confirmed_applied but without a confirmation_id.
    """
    execution = repo.get_execution(execution_id)
    if execution is None:
        return ReconcileResult(False, f"execution {execution_id} not found")
    if execution["status"] != ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value:
        return ReconcileResult(False, f"execution is {execution['status']}, not SUBMISSION_STATUS_UNKNOWN")

    job_id = execution["job_id"]
    job = get_job(job_id)
    if job is None:
        return ReconcileResult(False, f"job {job_id} not found")

    if resolution in ("confirmed_applied", "manual_applied"):
        repo.update_execution(execution_id, job_id, ExecutionStatus.APPLIED,
                               confirmation_id=confirmation_id, confirmation_url=confirmation_url,
                               user_action_reason=note, requires_user_action=0)
        repo.log_event(execution_id, job_id, "confirmed", detail=f"reconciled:{resolution}")
        blockers.resolve_blocker(execution_id, resolution_note=f"reconciled:{resolution}" + (f" -- {note}" if note else ""))
        return ReconcileResult(True, "marked APPLIED")

    if resolution == "confirmed_not_submitted":
        repo.update_execution(execution_id, job_id, ExecutionStatus.WITHDRAWN,
                               user_action_reason=note or "reconciled: not actually submitted", requires_user_action=0)
        repo.log_event(execution_id, job_id, "manually_applied", detail="reconciled:not_submitted")
        blockers.resolve_blocker(execution_id, resolution_note="reconciled:confirmed_not_submitted" +
                                  (f" -- {note}" if note else ""))
        return ReconcileResult(True, "marked WITHDRAWN -- job may be re-queued")

    return ReconcileResult(False, f"unknown resolution '{resolution}'")
