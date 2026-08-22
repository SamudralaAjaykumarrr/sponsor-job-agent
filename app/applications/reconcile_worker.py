"""Automated reconciliation EVIDENCE pass for SUBMISSION_STATUS_UNKNOWN
executions (CLAUDE.md Phase 9 section 8). This module NEVER itself decides
an execution's fate by fiat -- it only calls a provider's OPTIONAL, genuinely
legitimate `check_submission_status()` hook (app.applications.provider
.ApplicationProvider.check_submission_status, default None/unsupported for
every real ATS adapter in this project) and, when that returns real evidence
one way or the other, funnels the result through the SAME
app.applications.reconcile.reconcile_execution() function a human operator
would use from the CLI/dashboard -- preserving CLAUDE.md Phase 8's durable
rule that reconcile_execution() is the only path that resolves an unknown
execution, and never fabricating a confirmation.

An execution whose provider has no such interface (i.e. every real ATS
adapter today) is left completely untouched -- NEEDS_USER_ACTION for a human,
exactly as Phase 8 already behaves."""

from dataclasses import dataclass, field

from app.applications import repo
from app.applications.models import ExecutionStatus
from app.applications.provider_registry import get_application_provider
from app.applications.reconcile import reconcile_execution
from app.jobs_repo import get_job


@dataclass
class ReconcileWorkerResult:
    checked: int = 0
    auto_resolved_applied: int = 0
    auto_resolved_not_submitted: int = 0
    unsupported_provider: int = 0
    still_unknown: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked, "auto_resolved_applied": self.auto_resolved_applied,
            "auto_resolved_not_submitted": self.auto_resolved_not_submitted,
            "unsupported_provider": self.unsupported_provider, "still_unknown": self.still_unknown,
            "errors": self.errors,
        }


def run_pass(limit: int = 50) -> ReconcileWorkerResult:
    result = ReconcileWorkerResult()
    executions = repo.list_executions(status=ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value, limit=limit)

    for execution in executions:
        job = get_job(execution["job_id"])
        if job is None:
            result.errors.append(f"execution {execution['execution_id']}: job {execution['job_id']} missing")
            continue

        provider = get_application_provider(job)
        if not provider.capabilities.confirmation_recheck_supported:
            result.unsupported_provider += 1
            continue

        result.checked += 1
        try:
            confirmation = provider.check_submission_status(job, execution)
        except Exception as exc:  # noqa: BLE001 -- a provider-side check failing must never crash the pass
            result.errors.append(f"execution {execution['execution_id']}: check_submission_status raised {exc!r}")
            result.still_unknown += 1
            continue

        if confirmation is None:
            result.still_unknown += 1
            continue

        if confirmation.confirmed:
            outcome = reconcile_execution(
                execution["execution_id"], "confirmed_applied",
                confirmation_id=confirmation.confirmation_id, confirmation_url=confirmation.confirmation_url,
                note="auto-reconciled via provider status check (app.applications.reconcile_worker)",
            )
            if outcome.ok:
                result.auto_resolved_applied += 1
            else:
                result.errors.append(f"execution {execution['execution_id']}: {outcome.detail}")
        else:
            outcome = reconcile_execution(
                execution["execution_id"], "confirmed_not_submitted",
                note="auto-reconciled via provider status check: provider reports no record of this submission",
            )
            if outcome.ok:
                result.auto_resolved_not_submitted += 1
            else:
                result.errors.append(f"execution {execution['execution_id']}: {outcome.detail}")

    return result
