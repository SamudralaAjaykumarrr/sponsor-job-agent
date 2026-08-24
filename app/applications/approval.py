"""Durable, per-application approval record for the approval-gated autonomy
workflow (branch feat/approval-gated-autonomy-v1). The agent prepares every
eligible job automatically all the way to ExecutionStatus.SUBMISSION_READY
(the product-facing READY_FOR_APPROVAL stage -- see
app.applications.product_state) and stops there; nothing past that point
ever runs without an explicit APPROVE & APPLY action recorded by this
module. START AGENT never implies approval -- approval is always specific
to one job/execution.

`application_approvals` is APPEND-ONLY, mirroring this project's existing
sponsorship_decisions/capability_evidence pattern -- a row is never UPDATEd
once written. Approval validity is always LIVE-recomputed
(is_current_valid) by comparing the latest row's stored fingerprints
against the job/execution's CURRENT fingerprints, never a stored boolean
that could silently go stale -- the same "never cached, always
live-recomputed" idiom app.applications.provider_health.compute_health()
and app.applications.job_identity.verify_job_identity_full() already use.

approve_and_apply() is the ONLY function that creates an approval row, and
the ONLY caller of app.applications.executor.process_execution(approved=True)
-- the sole extra gate (besides the pre-existing AUTO_PERMITTED/
AUTO_SUBMIT_ENABLED path) that may unlock a provider.submit() call. It
records that row ONLY after winning the atomic SUBMISSION_READY -> STARTED
claim (_claim_ready_execution) -- never before -- so that same rowcount==1
win is also the single-current-approval invariant: two simultaneous clicks
can never each insert their own ACTIVE approval row for one execution, with
no separate lock and no destructive rewriting of any prior row. Before
process_execution() may actually call provider.submit() on this path, it
independently re-verifies the durable approval row is present, ACTIVE, and
still current via verify_durable_approval_for_submission() below -- the
`approved=True` parameter alone is never sufficient."""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.applications import post_approval, repo
from app.applications.models import ExecutionStatus
from app.applications.provider_registry import get_application_provider
from app.db import db_session
from app.jobs_repo import get_job
from app.matching.employment_type import classify_employment_type
from app.models import Job


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_approval_id() -> str:
    return f"appr_{uuid.uuid4().hex}"


def _profile_fingerprint() -> str:
    # Deliberately re-implemented (not imported from app.applications.executor)
    # to avoid a circular import (executor imports this module's sibling
    # process_execution path indirectly via approve_and_apply) -- identical
    # definition to app.applications.executor._profile_fingerprint.
    from app.candidate.profile import load_profile

    profile = load_profile()
    return hashlib.sha256(profile.model_dump_json().encode("utf-8")).hexdigest()[:16]


def _job_identity_fingerprint(job: Job) -> str:
    raw = f"{job.provider}|{job.external_job_id}|{job.canonical_url or job.url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ApprovalResult:
    ok: bool
    execution_id: Optional[str]
    approval_id: Optional[str]
    execution: Optional[dict]
    reason: str = ""
    already: bool = False
    # Provider Post-Approval Execution V1: best-effort report of whether the
    # browser-assist session bridge (app.applications.post_approval) was
    # attempted/started for this approval -- None when the execution didn't
    # land on APPROVED (nothing to bridge), never a claim of submission.
    browser_assist: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "execution_id": self.execution_id, "approval_id": self.approval_id,
            "execution": self.execution, "reason": self.reason, "already": self.already,
            "browser_assist": self.browser_assist,
        }


def _record_approval_row(job: Job, execution: dict, *, provider_submission_supported: bool) -> str:
    approval_id = new_approval_id()
    employment_type = classify_employment_type(job.employment_type, job.title, job.description)
    with db_session() as conn:
        conn.execute(
            """INSERT INTO application_approvals
               (approval_id, execution_id, job_id, provider, approved_at, approved_by,
                job_identity_fingerprint, jd_fingerprint, resume_variant_id, resume_fingerprint,
                answers_version, profile_fingerprint, form_fingerprint,
                sponsorship_status_at_approval, employment_type_at_approval,
                submission_capability, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)""",
            (
                approval_id, execution["execution_id"], job.id, job.provider or "", utcnow(),
                _job_identity_fingerprint(job),
                job.resume_jd_fingerprint or job.jd_sponsorship_fingerprint or "",
                job.promoted_resume_variant_id or "", execution.get("resume_artifact_hash") or "",
                execution.get("answers_version") or 0, _profile_fingerprint(),
                execution.get("form_fingerprint") or "",
                job.sponsorship_status.value, employment_type.value,
                "SUPPORTED" if provider_submission_supported else "UNSUPPORTED",
                utcnow(),
            ),
        )
    return approval_id


def get_latest_approval(execution_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM application_approvals WHERE execution_id = ? ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        return dict(row) if row else None


def list_approvals_for_job(job_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_approvals WHERE job_id = ? ORDER BY id DESC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def is_current_valid(job: Job, execution: dict, approval: dict) -> tuple[bool, list[str]]:
    """Live comparison only -- never trusts a stored 'valid' flag (spec
    section 7, "Approval Invalidation"). Any mismatch means the approval no
    longer covers what would actually be submitted, and the application
    must be treated as needing a fresh review/approval rather than
    continuing on stale authorization."""
    reasons: list[str] = []
    if _job_identity_fingerprint(job) != (approval.get("job_identity_fingerprint") or ""):
        reasons.append("job identity changed since approval")
    current_jd = job.resume_jd_fingerprint or job.jd_sponsorship_fingerprint or ""
    approved_jd = approval.get("jd_fingerprint") or ""
    if current_jd and approved_jd and current_jd != approved_jd:
        reasons.append("job description changed since approval")
    current_resume_hash = execution.get("resume_artifact_hash") or ""
    approved_resume_hash = approval.get("resume_fingerprint") or ""
    if current_resume_hash and approved_resume_hash and current_resume_hash != approved_resume_hash:
        reasons.append("resume changed since approval")
    if job.promoted_resume_variant_id and approval.get("resume_variant_id") and \
            job.promoted_resume_variant_id != approval["resume_variant_id"]:
        reasons.append("resume variant changed since approval")
    if _profile_fingerprint() != (approval.get("profile_fingerprint") or ""):
        reasons.append("candidate answers changed since approval")
    # answers_version / form_fingerprint (spec requirement 1): unlike the
    # "both present" comparisons above, these two use a DIRECT equality
    # check, including empty-vs-non-empty -- by the time an execution
    # reaches SUBMISSION_READY (a precondition of approve_and_apply()) both
    # are always genuinely populated, so an approval row with an empty value
    # here means "unknown/unset at approval time", and a current value that
    # is now non-empty (or simply different) is exactly the "materially
    # known changed value" requirement 2 says must never be silently treated
    # as valid -- conservative in both directions (known->different AND
    # unknown->known both invalidate; unknown->unknown stays valid).
    current_answers_version = int(execution.get("answers_version") or 0)
    approved_answers_version = int(approval.get("answers_version") or 0)
    if current_answers_version != approved_answers_version:
        reasons.append("application answers changed since approval (answers_version)")
    current_form_fingerprint = execution.get("form_fingerprint") or ""
    approved_form_fingerprint = approval.get("form_fingerprint") or ""
    if current_form_fingerprint != approved_form_fingerprint:
        reasons.append("application form changed since approval (form_fingerprint)")
    if job.sponsorship_status.value != (approval.get("sponsorship_status_at_approval") or ""):
        reasons.append("sponsorship status changed since approval")
    employment_type = classify_employment_type(job.employment_type, job.title, job.description)
    if employment_type.value != (approval.get("employment_type_at_approval") or ""):
        reasons.append("employment classification changed since approval")
    return (len(reasons) == 0, reasons)


def check_approval_freshness(job_id: int) -> dict:
    """Read-only helper for the dashboard/review page -- reports whether the
    latest approval (if any) for this job's active execution still covers
    the job/execution's current state. Never mutates anything (see module
    docstring: validity is always live-recomputed, not stored)."""
    job = get_job(job_id)
    execution = repo.get_active_execution_for_job(job_id) if job else None
    if job is None or execution is None:
        return {"has_approval": False, "valid": True, "reasons": []}
    approval = get_latest_approval(execution["execution_id"])
    if approval is None:
        return {"has_approval": False, "valid": True, "reasons": []}
    valid, reasons = is_current_valid(job, execution, approval)
    return {"has_approval": True, "valid": valid, "reasons": reasons, "approval": approval}


def verify_durable_approval_for_submission(job: Job, execution: dict) -> tuple[bool, str]:
    """The server-side durable-approval gate the approval-gated-autonomy
    spec requires immediately before app.applications.executor.
    process_execution() may call provider.submit() on the approved path --
    called from app.applications.executor._approved_submit_permitted() and
    NEVER trusts the caller's `approved=True` parameter alone. Re-fetches
    the latest application_approvals row fresh from the database (never a
    value cached on the execution dict passed in) and re-validates it
    against the execution's CURRENT fingerprints via is_current_valid() --
    a missing row, a non-ACTIVE row, or any live mismatch (job identity, JD,
    resume variant/fingerprint, answers_version, profile fingerprint,
    form_fingerprint, sponsorship status, employment classification --
    everything is_current_valid compares) always blocks submission."""
    approval = get_latest_approval(execution["execution_id"])
    if approval is None:
        return False, "no durable approval record exists for this execution"
    if (approval.get("status") or "") != "ACTIVE":
        return False, f"latest approval record status is '{approval.get('status')}', not ACTIVE"
    valid, reasons = is_current_valid(job, execution, approval)
    if not valid:
        return False, "approval is stale: " + "; ".join(reasons)
    return True, "durable approval verified current"


def _claim_ready_execution(execution_id: str) -> bool:
    """Atomic SUBMISSION_READY -> STARTED transition -- the actual guard
    against a double APPROVE & APPLY click / two open tabs (spec section
    22's "approval double-click" acceptance scenario). A losing concurrent
    caller's UPDATE affects 0 rows and is told the application is already
    being processed rather than re-running the pipeline a second time.
    Mirrors the same atomic `UPDATE ... WHERE <still-the-expected-status>`
    claim idiom this project's leasing/queue code already uses throughout
    (app.applications.queue.claim_execution_batch, app.workers.leasing)."""
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE application_executions SET status = ?, updated_at = ? "
            "WHERE execution_id = ? AND active = 1 AND status = ?",
            (ExecutionStatus.STARTED.value, utcnow(), execution_id, ExecutionStatus.SUBMISSION_READY.value),
        )
        return cur.rowcount == 1


def approve_and_apply(job_id: int) -> ApprovalResult:
    """The APPROVE & APPLY action (spec section 6). Requires the job's
    active execution to genuinely be at SUBMISSION_READY (READY_FOR_APPROVAL)
    -- never approves/continues anything else. Records a durable approval
    row bound to the exact fingerprints being approved, then immediately
    (synchronously, same as the existing "Prepare Application" button)
    re-runs the executor pipeline with approved=True so every gate is
    revalidated fresh before anything is ever submitted."""
    from app.applications.executor import process_execution

    job = get_job(job_id)
    if job is None:
        return ApprovalResult(False, None, None, None, "job not found")

    execution = repo.get_active_execution_for_job(job_id)
    if execution is None:
        return ApprovalResult(False, None, None, None, "no active application prepared for this job yet")

    if execution["status"] == ExecutionStatus.APPROVED.value:
        return ApprovalResult(True, execution["execution_id"], None, execution,
                               "already approved -- awaiting completion", already=True)
    if execution["status"] != ExecutionStatus.SUBMISSION_READY.value:
        return ApprovalResult(False, execution["execution_id"], None, execution,
                               f"not ready for approval yet (current status={execution['status']})")

    # Claim FIRST, record SECOND: only the caller that wins the atomic
    # SUBMISSION_READY -> STARTED transition may ever record a durable
    # approval row for this execution. Recording before claiming would let
    # two simultaneous clicks each successfully insert their own ACTIVE
    # approval row before either learned it lost the race, leaving two
    # ACTIVE rows for one execution -- exactly the duplicate-approval defect
    # this ordering prevents.
    if not _claim_ready_execution(execution["execution_id"]):
        # Lost the race to a concurrent approval/prepare/retry call -- never
        # re-run the pipeline, and never record a second approval row; no
        # approval_id to report since this caller recorded nothing.
        current = repo.get_execution(execution["execution_id"])
        return ApprovalResult(True, execution["execution_id"], None, current,
                               "already being processed", already=True)

    provider = get_application_provider(job)
    approval_id = _record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )
    repo.log_event(execution["execution_id"], job_id, "approved", detail=f"approval_id={approval_id}")

    updated = process_execution(execution["execution_id"], approved=True)

    # Provider Post-Approval Execution V1: if the pipeline landed on APPROVED
    # (a real provider with no verified automated final-submission
    # capability), immediately try to open/resume the browser-assist session
    # for THIS job -- see app.applications.post_approval's module docstring
    # for why this never bypasses any existing gate and never touches any
    # other job. Best-effort: never turns an already-successful approval
    # into a failure.
    bridge_result = None
    if updated is not None and updated.get("status") == ExecutionStatus.APPROVED.value:
        bridge_result = post_approval.advance_after_approval(execution["execution_id"])
        refreshed = repo.get_execution(execution["execution_id"])
        if refreshed is not None:
            updated = refreshed

    return ApprovalResult(True, execution["execution_id"], approval_id, updated, "approved",
                           browser_assist=bridge_result)


@dataclass
class BulkApprovalResult:
    results: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"results": self.results}

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r["ok"])

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r["ok"])


def approve_and_apply_bulk(job_ids: list[int]) -> BulkApprovalResult:
    """APPROVE & APPLY SELECTED (spec section 14): each job's approval
    remains individually recorded (one approve_and_apply() call per job,
    each its own durable row) -- one job failing must never stop the
    others, matching this project's pervasive per-tenant/per-provider
    isolation convention."""
    out = BulkApprovalResult()
    for jid in job_ids:
        try:
            result = approve_and_apply(jid)
            out.results.append(dict(result.as_dict(), job_id=jid))
        except Exception as exc:  # noqa: BLE001 -- one job's failure must never abort the batch
            out.results.append({
                "job_id": jid, "ok": False, "reason": f"error: {exc}",
                "execution_id": None, "approval_id": None, "execution": None, "already": False,
            })
    return out
