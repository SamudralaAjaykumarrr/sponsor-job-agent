"""Greenhouse Verified Submission Contract V1: the provider-specific
pre-submit contract for Greenhouse.

This module proves, in order, the twelve facts the build brief's SUBMIT
CONTRACT section requires, before `app.applications.greenhouse_submit_engine`
is ever allowed to open a browser or click anything:

  1. canonical application/job identity
  2. current posting still active
  3. current form fingerprint
  4. exact approved answer set
  5. exact approved resume/cover-letter artifacts
  6. required fields complete
  7. submit control uniquely identified   -- browser-time evidence only
  8. submit-once execution claim acquired -- browser-time evidence only

Steps 7-8 can only genuinely be proven once a real page is open (a submit
control's uniqueness is a DOM fact; the claim is acquired by
`app.applications.greenhouse_submit_engine` immediately before it clicks).
This module reports them as `NOT_YET_CHECKED` when built without browser
evidence (the ordinary case: dashboard preview, pre-submit manifest, the
canary's own pre-flight check before it opens a browser) and accepts an
optional `browser_evidence` to fold in genuine post-navigation facts once
they exist.

Like `app.applications.presubmit_manifest`, this module is STRICTLY
READ-ONLY and introduces NO new gate of its own for the ORDINARY executor
pipeline -- `app.applications.executor`/`app.applications.eligibility`/
`app.applications.approval` remain the real gates for that pipeline,
completely unmodified and untouched by this feature. This contract is
consulted ONLY by `app.applications.greenhouse_submit_engine` and
`app.applications.greenhouse_canary`, which is the sole, disabled-by-default
consumer of the readiness this module reports."""

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.applications import approval as _approval
from app.applications import document_binding
from app.applications import greenhouse_submit_claim as _claim
from app.applications import repo as _repo
from app.applications.provider_registry import get_application_provider
from app.applications.providers_greenhouse import (
    CanonicalIdentity,
    FormDiscoveryOutcome,
    canonical_identity,
)
from app.jobs_repo import get_job
from app.models import Job


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StepStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_YET_CHECKED = "NOT_YET_CHECKED"


class SubmitOutcome(str, Enum):
    """The build brief's exact post-submit result vocabulary (SUBMIT
    CONTRACT item 10). CONFIRMED/REJECTED are both DEFINITE, machine-readable
    outcomes; BLOCKED means the contract itself refused before/without a
    genuinely ambiguous submit attempt; SUBMISSION_STATUS_UNKNOWN is the
    honest result of any outcome that cannot be told apart from the other
    three -- and, per the brief's NON-NEGOTIABLE list, is NEVER retried
    automatically."""
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"


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
    """Genuine, already-observed browser-time facts -- never fabricated,
    never guessed. Supplied only by `greenhouse_submit_engine` after it has
    actually opened a page. Every field defaults to "not observed", never to
    a value that would look like a pass."""
    submit_control_unique: Optional[bool] = None
    submit_control_detail: str = ""
    job_identity_verified: Optional[bool] = None
    job_identity_detail: str = ""
    captcha_present: bool = False
    login_required: bool = False


@dataclass
class GreenhouseSubmitContract:
    job_id: int
    execution_id: str = ""
    identity: CanonicalIdentity = CanonicalIdentity(False)
    steps: list[ContractStep] = dataclass_field(default_factory=list)
    ready: bool = False
    blocking_reasons: list[str] = dataclass_field(default_factory=list)
    already_attempted: bool = False
    generated_at: str = dataclass_field(default_factory=utcnow)

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id, "execution_id": self.execution_id, "identity": self.identity.as_dict(),
            "steps": [s.as_dict() for s in self.steps], "ready": self.ready,
            "blocking_reasons": list(self.blocking_reasons), "already_attempted": self.already_attempted,
            "generated_at": self.generated_at,
        }


def _step(number: int, name: str, ok: bool, detail: str) -> ContractStep:
    return ContractStep(number, name, StepStatus.PASSED if ok else StepStatus.FAILED, detail)


def build_submit_contract(
    job_id: int, *, browser_evidence: Optional[BrowserEvidence] = None,
) -> Optional[GreenhouseSubmitContract]:
    """Builds the full 8-of-12-checkable-now contract for `job_id`'s current
    active execution. Returns None only when the job itself does not exist.
    A job with no active execution yet still gets a contract (every step
    simply fails on "no execution"), matching `presubmit_manifest.build_manifest`'s
    own "always report something honest" behavior."""
    job = get_job(job_id)
    if job is None:
        return None

    identity = canonical_identity(job)
    execution = _repo.get_active_execution_for_job(job_id)
    contract = GreenhouseSubmitContract(job_id=job_id, identity=identity)

    if execution is not None:
        contract.execution_id = execution["execution_id"]
        contract.already_attempted = _claim.already_attempted(execution["execution_id"])

    steps: list[ContractStep] = []

    # --- 1. canonical application/job identity -----------------------------
    steps.append(_step(1, "canonical_identity", identity.recognized, identity.reason))

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

    provider = get_application_provider(job)
    # `get_application_provider` falls back to the generic ASSIST-only
    # provider (which has no discover_form_detailed/check_job_still_active/
    # classify_job_inactive_reason -- those are Greenhouse-adapter-specific)
    # whenever identity isn't recognized. Steps 2-3 can only genuinely be
    # checked with the real Greenhouse adapter, so they honestly fail rather
    # than risk an AttributeError against the generic fallback.
    has_greenhouse_adapter = identity.recognized and hasattr(provider, "discover_form_detailed")

    # --- 2. current posting still active ------------------------------------
    if not has_greenhouse_adapter:
        steps.append(_step(2, "job_still_active", False, "no recognized Greenhouse identity to check liveness against"))
    else:
        still_active = provider.check_job_still_active(job)
        if still_active is False:
            reason = provider.classify_job_inactive_reason(job) or "posting no longer active"
            steps.append(_step(2, "job_still_active", False,
                                f"provider reports this posting is no longer active ({reason})"))
        else:
            steps.append(_step(
                2, "job_still_active", True,
                "provider confirms the posting is live" if still_active else "not independently checkable -- treated as active",
            ))

    # --- 3. current form fingerprint ----------------------------------------
    current_form = None
    if not has_greenhouse_adapter:
        steps.append(_step(3, "current_form_fingerprint", False,
                            "no recognized Greenhouse identity to (re)discover a form against"))
    else:
        discovery = provider.discover_form_detailed(job)
        stored_fingerprint = execution.get("form_fingerprint") or ""
        if discovery.outcome != FormDiscoveryOutcome.DISCOVERED or discovery.form is None:
            steps.append(_step(3, "current_form_fingerprint", False,
                                f"form could not be (re)discovered: {discovery.outcome.value} -- {discovery.detail}"))
        else:
            current_form = discovery.form
            if stored_fingerprint and stored_fingerprint != current_form.fingerprint:
                steps.append(_step(3, "current_form_fingerprint", False,
                                    "the application form changed since this execution's fingerprint was recorded -- "
                                    "stale form, must not submit against it"))
            else:
                steps.append(_step(3, "current_form_fingerprint", True,
                                    "current form fingerprint matches the execution's recorded fingerprint"
                                    if stored_fingerprint else
                                    "form fingerprint recorded fresh (no prior value to compare)"))

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
        # Reliable Form Interaction / Browser-Verified Answer Canonical
        # Readiness Integration: this step must stay consistent with every
        # other readiness check in the project (presubmit_manifest.
        # build_manifest, form_model._normalize_one, browser_runtime.
        # _fill_pass) -- a required field with genuine, individually
        # human-verified evidence on file must never be reported here as
        # "no safe verified answer" just because THIS step rebuilt its own
        # application_fields list without merging it. Real live bug: this
        # step disagreed with the browser session's own (correctly
        # evidence-aware) readiness check for the exact same execution.
        if execution.get("execution_id"):
            from app.applications.verified_field_evidence import build_application_field_overrides

            application_fields = application_fields + build_application_field_overrides(
                execution["execution_id"], job,
            ).fields
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
        steps.append(_step(6, "required_fields_complete", False, "form was not available to check required fields"))

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


def render_text(contract: GreenhouseSubmitContract) -> str:
    lines = [
        "Greenhouse Submit Contract", "=" * 60,
        f"Job:        #{contract.job_id}",
        f"Execution:  {contract.execution_id or '(none)'}",
        f"Identity:   recognized={contract.identity.recognized} "
        f"token={contract.identity.board_token or '-'} posting={contract.identity.posting_id or '-'}",
        f"Already attempted: {contract.already_attempted}",
        "",
    ]
    for step in contract.steps:
        lines.append(f"  [{step.status.value:<16}] {step.number}. {step.name}: {step.detail}")
    lines += ["", f"READY: {contract.ready}"]
    for reason in contract.blocking_reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines) + "\n"
