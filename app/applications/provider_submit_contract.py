"""Generic multi-provider submit-contract readiness report (Canary Candidate
Pool Expansion + Multi-Provider Readiness V1).

`app.applications.greenhouse_submit_contract` proves 6 of its 12 (8
non-browser-time) steps without a browser because Greenhouse uniquely
publishes BOTH a liveness API and a structured question-schema API. This
module answers the SAME 8-step question for any OTHER provider registered in
`app.applications.provider_registry`, honestly bounded by what that
provider's own real adapter (`app.applications.providers_lever`/
`providers_ashby`/etc.) can actually prove without opening a browser:

  1. canonical application/job identity  -- via `provider.canonical_identity()`
  2. current posting still active        -- via `provider.check_job_still_active()`
     if the adapter implements it (Lever/Ashby both do, via a genuine public
     read API -- see their own modules); NOT_YET_CHECKED otherwise.
  3. current form fingerprint            -- via `provider.discover_form()`.
     Lever/Ashby/Workable's `discover_form()` always returns None (no public
     question-schema API exists for any of them -- confirmed by reading
     their own adapter code, not guessed), so this step is honestly
     NOT_YET_CHECKED "requires a live browser session" for those providers,
     never FAILED (FAILED would wrongly imply something was checked and
     found broken) and never faked as PASSED.
  4. exact approved answer set           -- identical to Greenhouse: reuses
     `app.applications.approval` directly, fully provider-neutral already.
  5. exact approved documents            -- identical to Greenhouse: reuses
     `app.applications.document_binding` directly, fully provider-neutral.
  6. required fields complete            -- only checkable when step 3
     produced a real form; NOT_YET_CHECKED when it didn't.
  7. submit control uniquely identified  -- browser-time only, same as Greenhouse.
  8. submit-once claim acquired          -- via `app.applications.provider_submit_claim`
     (the generalized version of `greenhouse_submit_claim`).

Like `greenhouse_submit_contract`, this module is STRICTLY READ-ONLY and
introduces no new gate for the ordinary executor pipeline. It never opens a
browser, never submits, and never sets any provider's `submission_supported`.
No submit engine exists yet for Lever/Ashby/Workable/etc -- this module only
reports how close the READ-ONLY-checkable part of their contract is."""

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.applications import approval as _approval
from app.applications import document_binding
from app.applications import provider_submit_claim as _claim
from app.applications import repo as _repo
from app.applications.provider_registry import get_application_provider
from app.jobs_repo import get_job


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StepStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_YET_CHECKED = "NOT_YET_CHECKED"


@dataclass(frozen=True)
class ContractStep:
    number: int
    name: str
    status: StepStatus
    detail: str = ""

    def as_dict(self) -> dict:
        return {"number": self.number, "name": self.name, "status": self.status.value, "detail": self.detail}


@dataclass(frozen=True)
class BrowserEvidence:
    """Same shape/contract as `greenhouse_submit_contract.BrowserEvidence` --
    genuine, already-observed browser-time facts only, never fabricated."""
    submit_control_unique: Optional[bool] = None
    submit_control_detail: str = ""
    job_identity_verified: Optional[bool] = None
    job_identity_detail: str = ""
    captcha_present: bool = False
    login_required: bool = False


@dataclass
class ProviderSubmitContract:
    provider: str
    job_id: int
    execution_id: str = ""
    identity_recognized: bool = False
    identity_detail: dict = dataclass_field(default_factory=dict)
    steps: list[ContractStep] = dataclass_field(default_factory=list)
    ready: bool = False
    blocking_reasons: list[str] = dataclass_field(default_factory=list)
    already_attempted: bool = False
    generated_at: str = dataclass_field(default_factory=utcnow)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "job_id": self.job_id, "execution_id": self.execution_id,
            "identity": {"recognized": self.identity_recognized, **self.identity_detail},
            "steps": [s.as_dict() for s in self.steps], "ready": self.ready,
            "blocking_reasons": list(self.blocking_reasons), "already_attempted": self.already_attempted,
            "generated_at": self.generated_at,
        }


def _step(number: int, name: str, ok: bool, detail: str) -> ContractStep:
    return ContractStep(number, name, StepStatus.PASSED if ok else StepStatus.FAILED, detail)


def build_submit_contract(
    provider_name: str, job_id: int, *, browser_evidence: Optional[BrowserEvidence] = None,
) -> Optional[ProviderSubmitContract]:
    """Builds the generic 8-checkable-step contract for `job_id` against
    `provider_name`. Returns None only when the job itself does not exist or
    its own `job.provider` does not match `provider_name` (never silently
    evaluates a job against the wrong provider's identity rules)."""
    job = get_job(job_id)
    if job is None:
        return None
    if (job.provider or "").lower() != provider_name.lower():
        return None

    provider = get_application_provider(job)
    identity = provider.canonical_identity(job)
    execution = _repo.get_active_execution_for_job(job_id)
    contract = ProviderSubmitContract(
        provider=provider_name, job_id=job_id,
        identity_recognized=bool(identity.recognized), identity_detail=identity.as_dict(),
    )

    if execution is not None:
        contract.execution_id = execution["execution_id"]
        contract.already_attempted = _claim.already_attempted(provider_name, execution["execution_id"])

    steps: list[ContractStep] = []
    steps.append(_step(1, "canonical_identity", bool(identity.recognized), identity.reason))

    if execution is None:
        steps.append(_step(2, "job_still_active", False, "no active execution exists for this job yet"))
        steps.append(_step(3, "current_form_fingerprint", False, "no execution to compare a form fingerprint against"))
        steps.append(_step(4, "approved_answer_set", False, "no execution to check approval against"))
        steps.append(_step(5, "approved_documents", False, "no execution to check document bindings against"))
        steps.append(_step(6, "required_fields_complete", False, "no execution to check required fields against"))
        contract.steps = steps + [
            ContractStep(7, "submit_control_unique", StepStatus.NOT_YET_CHECKED, "requires an open browser"),
            ContractStep(8, "submit_once_claim", StepStatus.NOT_YET_CHECKED, "requires a submit attempt in progress"),
        ]
        contract.blocking_reasons = [s.detail for s in steps if s.status == StepStatus.FAILED]
        contract.ready = False
        return contract

    # --- 2. current posting still active ------------------------------------
    if not identity.recognized or not hasattr(provider, "check_job_still_active"):
        steps.append(ContractStep(2, "job_still_active", StepStatus.NOT_YET_CHECKED,
                                   "no recognized identity or no liveness API for this provider"))
    else:
        still_active = provider.check_job_still_active(job)
        if still_active is False:
            reason = ""
            if hasattr(provider, "classify_job_inactive_reason"):
                reason = provider.classify_job_inactive_reason(job) or ""
            steps.append(_step(2, "job_still_active", False,
                                f"provider reports this posting is no longer active ({reason or 'REMOVED'})"))
        elif still_active is True:
            steps.append(_step(2, "job_still_active", True, "provider confirms the posting is live"))
        else:
            steps.append(ContractStep(2, "job_still_active", StepStatus.NOT_YET_CHECKED,
                                       "liveness not independently checkable right now (fetch failure/refusal)"))

    # --- 3. current form fingerprint ----------------------------------------
    current_form = None
    stored_fingerprint = execution.get("form_fingerprint") or ""
    if not identity.recognized or not hasattr(provider, "discover_form"):
        steps.append(ContractStep(3, "current_form_fingerprint", StepStatus.NOT_YET_CHECKED,
                                   "no recognized identity or no form-discovery capability for this provider"))
    else:
        current_form = provider.discover_form(job)
        if current_form is None:
            steps.append(ContractStep(
                3, "current_form_fingerprint", StepStatus.NOT_YET_CHECKED,
                f"{provider_name} publishes no public application-question API -- the real form is only "
                "discoverable via a live browser session (out of scope for this read-only contract)",
            ))
        elif stored_fingerprint and stored_fingerprint != current_form.fingerprint:
            steps.append(_step(3, "current_form_fingerprint", False,
                                "the application form changed since this execution's fingerprint was recorded -- "
                                "stale form, must not submit against it"))
        else:
            steps.append(_step(3, "current_form_fingerprint", True,
                                "current form fingerprint matches the execution's recorded fingerprint"
                                if stored_fingerprint else "form fingerprint recorded fresh (no prior value to compare)"))

    # --- 4. exact approved answer set ---------------------------------------
    approval_row = _approval.get_latest_approval(execution["execution_id"])
    if approval_row is None:
        steps.append(_step(4, "approved_answer_set", False, "no durable approval record exists for this execution"))
    elif (approval_row.get("status") or "") != "ACTIVE":
        steps.append(_step(4, "approved_answer_set", False,
                            f"latest approval status is '{approval_row.get('status')}', not ACTIVE"))
    else:
        valid, reasons = _approval.is_current_valid(job, execution, approval_row)
        steps.append(_step(4, "approved_answer_set", valid,
                            "approval is current" if valid else "approval is stale: " + "; ".join(reasons)))

    # --- 5. exact approved resume/cover-letter artifacts --------------------
    resume_check = document_binding.verify_artifact_matches_job(job.id, job.resume_pdf_path or "")
    doc_ok = resume_check.ok
    doc_detail = resume_check.reason or "resume artifact verified"
    if doc_ok and approval_row is not None:
        approved_hash = approval_row.get("resume_fingerprint") or ""
        if approved_hash and approved_hash != resume_check.sha256:
            doc_ok = False
            doc_detail = "resume artifact hash no longer matches the hash the approval was recorded against"
    if doc_ok and job.cover_letter_path:
        cover_check = document_binding.verify_artifact_matches_job(job.id, job.cover_letter_path)
        if not cover_check.ok:
            doc_ok = False
            doc_detail = f"cover letter artifact: {cover_check.reason}"
    steps.append(_step(5, "approved_documents", doc_ok, doc_detail))

    # --- 6. required fields complete ----------------------------------------
    if current_form is not None:
        from app.applications.form_model import normalize_form_snapshot
        from app.applications.schema import build_application_fields
        from app.candidate.profile import load_profile

        profile = load_profile()
        application_fields = build_application_fields(
            profile, resume_path=job.resume_pdf_path or "", cover_letter_path=job.cover_letter_path or "",
        )
        normalized = normalize_form_snapshot(current_form, application_fields)
        unanswered = normalized.unanswered_required()
        high_risk_pending = [f for f in normalized.high_risk_fields() if not f.safe_answer_available]
        if unanswered:
            steps.append(_step(6, "required_fields_complete", False,
                                f"{len(unanswered)} required field(s) have no safe verified answer: "
                                + ", ".join(f.label or f.provider_field_id for f in unanswered[:5])))
        elif high_risk_pending:
            steps.append(_step(6, "required_fields_complete", False,
                                f"{len(high_risk_pending)} high-risk question(s) need the candidate's own decision: "
                                + ", ".join(f.label or f.provider_field_id for f in high_risk_pending[:5])))
        else:
            steps.append(_step(6, "required_fields_complete", True, "every required field has a safe verified answer"))
    else:
        steps.append(ContractStep(6, "required_fields_complete", StepStatus.NOT_YET_CHECKED,
                                   "no form available yet to check required fields against (see step 3)"))

    # --- 7-8: browser-time facts --------------------------------------------
    if browser_evidence is None:
        steps.append(ContractStep(7, "submit_control_unique", StepStatus.NOT_YET_CHECKED, "requires an open browser"))
        steps.append(ContractStep(8, "submit_once_claim", StepStatus.NOT_YET_CHECKED,
                                   "requires a submit attempt in progress"))
    else:
        if browser_evidence.captcha_present:
            steps.append(_step(7, "submit_control_unique", False, "CAPTCHA present -- never bypassed"))
        elif browser_evidence.login_required:
            steps.append(_step(7, "submit_control_unique", False, "login/auth wall present -- never bypassed"))
        elif browser_evidence.job_identity_verified is False:
            steps.append(_step(7, "submit_control_unique", False,
                                f"job identity not verified: {browser_evidence.job_identity_detail}"))
        elif browser_evidence.submit_control_unique is None:
            steps.append(ContractStep(7, "submit_control_unique", StepStatus.NOT_YET_CHECKED,
                                       browser_evidence.submit_control_detail or "not yet scanned"))
        else:
            steps.append(_step(7, "submit_control_unique", bool(browser_evidence.submit_control_unique),
                                browser_evidence.submit_control_detail))
        already = contract.already_attempted
        steps.append(_step(8, "submit_once_claim", not already,
                            "no prior submit attempt recorded for this execution" if not already
                            else "a submit action was already attempted for this execution -- never retried"))

    contract.steps = steps
    checkable = [s for s in steps if s.status != StepStatus.NOT_YET_CHECKED]
    contract.blocking_reasons = [s.detail for s in checkable if s.status == StepStatus.FAILED]
    contract.ready = all(s.status != StepStatus.FAILED for s in steps)
    return contract


def render_text(contract: ProviderSubmitContract) -> str:
    lines = [
        f"{contract.provider.title()} Submit Contract", "=" * 60,
        f"Job:        #{contract.job_id}",
        f"Execution:  {contract.execution_id or '(none)'}",
        f"Identity:   recognized={contract.identity_recognized} {contract.identity_detail}",
        f"Already attempted: {contract.already_attempted}",
        "",
    ]
    for step in contract.steps:
        lines.append(f"  [{step.status.value:<16}] {step.number}. {step.name}: {step.detail}")
    lines += ["", f"READY: {contract.ready}"]
    for reason in contract.blocking_reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines) + "\n"
