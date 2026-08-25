"""Provider-neutral PRE-SUBMIT MANIFEST (Real Provider Execution V1).

The brief's PRE-SUBMIT REVIEW requirement: one provider-neutral manifest
carrying job identity, provider, resume artifact/hash, cover-letter
artifact/hash if present, all mapped answers, unanswered required fields,
blockers, form fingerprint, profile fingerprint, approval/authorization
state, and provider capabilities -- "This should drive READY_FOR_APPROVAL."

This module is STRICTLY READ-ONLY. It is an aggregation view over facts other
modules already own and already decided:

  - `app.jobs_repo` / `app.applications.repo`  -- job + execution rows
  - `app.applications.execution_contract`      -- the seven capability flags
  - `app.applications.form_model`              -- the normalized form
  - `app.applications.document_binding`        -- which artifact is bound
  - `app.applications.blockers`                -- the durable active blocker
  - `app.applications.approval`                -- durable approval + LIVE
                                                  staleness recomputation
  - `app.applications.product_state`           -- the coarse product stage

It introduces NO new gate. `ready_for_approval` below is a REPORT of whether
the existing `app.applications.product_state.ready_for_approval()` already
says so, plus the manifest's own blocking observations -- it never authorizes
anything, and nothing in this project consults it to decide whether to
submit. The actual submission gates remain, unchanged and unbypassed:
`app.applications.eligibility.evaluate_executor_eligibility`,
`app.applications.executor._auto_submit_permitted` /
`_approved_submit_permitted`, and
`app.applications.approval.verify_durable_approval_for_submission`.

Privacy: the manifest carries the candidate's own prepared answers, so it is
built on demand for a human reviewing THEIR OWN application and is never
written to a log. `as_dict(include_values=False)` (the default for any
logging/metrics caller) redacts every prepared answer value while keeping
the field ids, labels, and whether an answer exists -- matching this
project's standing rule that no candidate PII enters structured logs.
"""

import hashlib
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.applications import approval as _approval
from app.applications import blockers as _blockers
from app.applications import document_binding
from app.applications import product_state
from app.applications import repo as _repo
from app.applications.execution_contract import ProviderExecutionContract, build_contract
from app.applications.form_model import FormFieldSource, NormalizedForm, normalize_form_snapshot
from app.applications.models import ApplicationField
from app.applications.provider_registry import get_application_provider
from app.applications.schema import build_application_fields
from app.candidate.profile import load_profile
from app.jobs_repo import get_job
from app.models import Job

_REDACTED = "[redacted]"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_fingerprint() -> str:
    """Same definition as `app.applications.executor._profile_fingerprint`
    and `app.applications.approval._profile_fingerprint` -- deliberately
    re-derived rather than imported, matching the existing precedent that
    avoids a circular import between the executor and approval modules."""
    profile = load_profile()
    return hashlib.sha256(profile.model_dump_json().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class DocumentEntry:
    kind: str
    path: str = ""
    filename: str = ""
    sha256: str = ""
    exists: bool = False
    resume_variant_id: str = ""
    bound_to_field: str = ""
    bound_at: str = ""
    binding_id: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "path": self.path, "filename": self.filename, "sha256": self.sha256,
            "exists": self.exists, "resume_variant_id": self.resume_variant_id,
            "bound_to_field": self.bound_to_field, "bound_at": self.bound_at, "binding_id": self.binding_id,
        }


@dataclass(frozen=True)
class AnswerEntry:
    canonical_field_id: str
    label: str
    value: Optional[str]
    value_source: str
    confidence: str
    high_risk: bool
    high_risk_class: str
    needs_user_input: bool

    def as_dict(self, include_values: bool) -> dict:
        return {
            "canonical_field_id": self.canonical_field_id, "label": self.label,
            "value": (self.value if include_values else (_REDACTED if self.value is not None else None)),
            "has_value": self.value is not None,
            "value_source": self.value_source, "confidence": self.confidence,
            "high_risk": self.high_risk, "high_risk_class": self.high_risk_class,
            "needs_user_input": self.needs_user_input,
        }


@dataclass
class PreSubmitManifest:
    # --- job identity -----------------------------------------------------
    job_id: int
    company: str
    title: str
    location: str
    provider: str
    canonical_url: str
    external_job_id: str
    job_identity_fingerprint: str

    # --- execution --------------------------------------------------------
    execution_id: str = ""
    execution_status: str = ""
    execution_mode: str = ""
    product_stage: str = ""

    # --- capabilities -----------------------------------------------------
    capabilities: Optional[ProviderExecutionContract] = None

    # --- documents --------------------------------------------------------
    documents: list[DocumentEntry] = dataclass_field(default_factory=list)

    # --- answers / form ---------------------------------------------------
    answers: list[AnswerEntry] = dataclass_field(default_factory=list)
    unanswered_required: list[str] = dataclass_field(default_factory=list)
    high_risk_pending: list[str] = dataclass_field(default_factory=list)
    form_fingerprint: str = ""
    form_source: str = ""
    form_field_count: int = 0
    profile_fingerprint: str = ""

    # --- authorization ----------------------------------------------------
    has_approval: bool = False
    approval_id: str = ""
    approval_status: str = ""
    approval_valid: bool = False
    approval_stale_reasons: list[str] = dataclass_field(default_factory=list)

    # --- blockers ---------------------------------------------------------
    active_blocker_code: str = ""
    active_blocker_title: str = ""
    active_blocker_action: str = ""

    # --- summary ----------------------------------------------------------
    blocking_reasons: list[str] = dataclass_field(default_factory=list)
    ready_for_approval: bool = False
    generated_at: str = dataclass_field(default_factory=utcnow)

    def as_dict(self, include_values: bool = False) -> dict:
        """`include_values=False` (the default) redacts every prepared answer
        VALUE -- use it for anything that could be logged/exported. A human
        reviewing their own application in the dashboard passes True."""
        return {
            "job_id": self.job_id, "company": self.company, "title": self.title, "location": self.location,
            "provider": self.provider, "canonical_url": self.canonical_url,
            "external_job_id": self.external_job_id,
            "job_identity_fingerprint": self.job_identity_fingerprint,
            "execution_id": self.execution_id, "execution_status": self.execution_status,
            "execution_mode": self.execution_mode, "product_stage": self.product_stage,
            "capabilities": self.capabilities.as_dict() if self.capabilities else None,
            "documents": [d.as_dict() for d in self.documents],
            "answers": [a.as_dict(include_values) for a in self.answers],
            "answer_count": len(self.answers),
            "unanswered_required": list(self.unanswered_required),
            "high_risk_pending": list(self.high_risk_pending),
            "form_fingerprint": self.form_fingerprint, "form_source": self.form_source,
            "form_field_count": self.form_field_count,
            "profile_fingerprint": self.profile_fingerprint,
            "has_approval": self.has_approval, "approval_id": self.approval_id,
            "approval_status": self.approval_status, "approval_valid": self.approval_valid,
            "approval_stale_reasons": list(self.approval_stale_reasons),
            "active_blocker_code": self.active_blocker_code,
            "active_blocker_title": self.active_blocker_title,
            "active_blocker_action": self.active_blocker_action,
            "blocking_reasons": list(self.blocking_reasons),
            "ready_for_approval": self.ready_for_approval,
            "generated_at": self.generated_at,
        }


def _document_entry(job: Job, kind: document_binding.DocumentKind, path_str: str) -> DocumentEntry:
    binding = document_binding.latest_binding(job.id, kind) or {}
    path = Path(path_str) if path_str else None
    exists = bool(path and path.exists())
    digest = ""
    if exists:
        digest = document_binding.sha256_file(str(path))
    return DocumentEntry(
        kind=kind.value, path=path_str or "", filename=(path.name if path else ""), sha256=digest,
        exists=exists,
        resume_variant_id=binding.get("resume_variant_id") or (job.promoted_resume_variant_id or ""),
        bound_to_field=binding.get("provider_field_id") or "",
        bound_at=binding.get("created_at") or "", binding_id=binding.get("binding_id") or "",
    )


def _normalized_form_for(job: Job, application_fields: list[ApplicationField]) -> Optional[NormalizedForm]:
    """Uses the provider adapter's own published schema when it genuinely has
    one (Greenhouse, mock_ats). Providers whose form is only reachable
    through the real browser return None here -- the manifest then reports
    the answers prepared from the verified profile without claiming to know
    the employer's field list, which is the honest state."""
    provider = get_application_provider(job)
    normalized = getattr(provider, "normalized_form", None)
    if callable(normalized):
        return normalized(job, application_fields)
    snapshot = provider.discover_form(job)
    if snapshot is None:
        return None
    source = FormFieldSource.MOCK_FIXTURE if provider.name == "mock_ats" else FormFieldSource.PROVIDER_API
    return normalize_form_snapshot(snapshot, application_fields, source=source)


def build_manifest(job_id: int, *, discover_form: bool = True) -> Optional[PreSubmitManifest]:
    """Builds the manifest for a job's CURRENT active execution (or for the
    job alone when none exists yet). Returns None only when the job itself
    does not exist. `discover_form=False` skips the provider form lookup
    (which can perform a real network read) -- used by callers that only
    need the identity/document/approval picture."""
    job = get_job(job_id)
    if job is None:
        return None

    execution = _repo.get_active_execution_for_job(job_id)
    contract = build_contract(job.provider or "")
    profile = load_profile()
    application_fields = build_application_fields(
        profile, resume_path=job.resume_pdf_path or "", cover_letter_path=job.cover_letter_path or "",
    )

    manifest = PreSubmitManifest(
        job_id=job.id, company=job.company or "", title=job.title or "", location=job.location or "",
        provider=job.provider or "", canonical_url=job.canonical_url or job.url or "",
        external_job_id=job.external_job_id or "",
        job_identity_fingerprint=_approval._job_identity_fingerprint(job),
        capabilities=contract, profile_fingerprint=_profile_fingerprint(),
        product_stage=product_state.compute_stage(execution).stage.value,
    )

    if execution is not None:
        manifest.execution_id = execution["execution_id"]
        manifest.execution_status = execution["status"]
        manifest.execution_mode = execution["mode"]
        manifest.form_fingerprint = execution.get("form_fingerprint") or ""

    # --- documents --------------------------------------------------------
    manifest.documents.append(
        _document_entry(job, document_binding.DocumentKind.RESUME, job.resume_pdf_path or "")
    )
    if job.cover_letter_path:
        manifest.documents.append(
            _document_entry(job, document_binding.DocumentKind.COVER_LETTER, job.cover_letter_path)
        )

    # --- form + answers ---------------------------------------------------
    normalized = _normalized_form_for(job, application_fields) if discover_form else None
    if normalized is not None:
        manifest.form_source = normalized.source.value
        manifest.form_field_count = len(normalized.fields)
        if normalized.fingerprint:
            manifest.form_fingerprint = normalized.fingerprint
        manifest.unanswered_required = [
            f.label or f.provider_field_id for f in normalized.unanswered_required()
        ]
        manifest.high_risk_pending = [
            f.label or f.provider_field_id for f in normalized.high_risk_fields()
            if not f.safe_answer_available
        ]
        manifest.answers = [
            AnswerEntry(
                canonical_field_id=f.canonical_field_id, label=f.label or f.provider_field_id,
                value=f.current_value, value_source=f.value_source, confidence=f.confidence.value,
                high_risk=f.high_risk, high_risk_class=f.high_risk_class.value,
                needs_user_input=f.needs_user_input,
            )
            for f in normalized.fields
        ]
    else:
        # No published employer field list. Report the answers genuinely
        # prepared from the verified candidate profile, clearly labelled as
        # such -- never a guessed employer form.
        from app.applications.form_model import classify_high_risk

        manifest.form_source = "CANDIDATE_PROFILE_ONLY"
        for f in application_fields:
            assessment = classify_high_risk(f, f.label)
            manifest.answers.append(AnswerEntry(
                canonical_field_id=f.field_id, label=f.label, value=f.verified_value,
                value_source=f.value_source, confidence=f.confidence.value,
                high_risk=assessment.high_risk, high_risk_class=assessment.risk.value,
                needs_user_input=f.needs_user_input,
            ))
        manifest.unanswered_required = [f.label for f in application_fields if f.required and f.needs_user_input]

    # --- approval ---------------------------------------------------------
    if execution is not None:
        approval_row = _approval.get_latest_approval(execution["execution_id"])
        if approval_row is not None:
            manifest.has_approval = True
            manifest.approval_id = approval_row["approval_id"]
            manifest.approval_status = approval_row.get("status") or ""
            valid, reasons = _approval.is_current_valid(job, execution, approval_row)
            manifest.approval_valid = valid and manifest.approval_status == "ACTIVE"
            manifest.approval_stale_reasons = list(reasons)

        blocker = _blockers.get_active_blocker_for_execution(execution["execution_id"])
        if blocker is not None:
            manifest.active_blocker_code = blocker["blocker_code"]
            manifest.active_blocker_title = blocker["human_title"]
            manifest.active_blocker_action = blocker["required_action"]

    # --- summary ----------------------------------------------------------
    reasons: list[str] = []
    resume_entry = manifest.documents[0] if manifest.documents else None
    if resume_entry is None or not resume_entry.exists:
        reasons.append("no generated resume artifact exists for this job")
    if manifest.unanswered_required:
        reasons.append(f"{len(manifest.unanswered_required)} required field(s) have no verified answer")
    if manifest.high_risk_pending:
        reasons.append(f"{len(manifest.high_risk_pending)} high-risk question(s) need the candidate's decision")
    if manifest.active_blocker_code:
        reasons.append(f"active blocker: {manifest.active_blocker_code}")
    if manifest.has_approval and not manifest.approval_valid:
        reasons.append("the recorded approval is no longer current: " + "; ".join(manifest.approval_stale_reasons))
    manifest.blocking_reasons = reasons
    # REPORT, never a gate -- see this module's docstring.
    manifest.ready_for_approval = product_state.ready_for_approval(execution) and not reasons
    return manifest


def render_text(manifest: PreSubmitManifest, *, include_values: bool = False) -> str:
    d = manifest.as_dict(include_values=include_values)
    caps = d["capabilities"] or {}
    lines = [
        "Pre-Submit Manifest", "=" * 60,
        f"Job:              #{d['job_id']} {d['title']} @ {d['company']} ({d['location']})",
        f"Provider:         {d['provider']}",
        f"Canonical URL:    {d['canonical_url']}",
        f"Job identity fp:  {d['job_identity_fingerprint']}",
        f"Execution:        {d['execution_id'] or '(none)'} status={d['execution_status'] or '-'} "
        f"mode={d['execution_mode'] or '-'}",
        f"Product stage:    {d['product_stage']}",
        "",
        "Provider capabilities:",
        f"  submission_supported    {caps.get('submission_supported')}",
        f"  confirmation_supported  {caps.get('confirmation_supported')}",
        f"  assist_supported        {caps.get('assist_supported')}",
        f"  automation_policy       {caps.get('automation_policy')}",
        "",
        "Documents:",
    ]
    for doc in d["documents"]:
        lines.append(
            f"  {doc['kind']:<13} {doc['filename'] or '(none)'} sha256={doc['sha256'][:16] or '-'} "
            f"exists={doc['exists']} variant={doc['resume_variant_id'] or '-'} "
            f"bound_field={doc['bound_to_field'] or '-'}"
        )
    lines += [
        "",
        f"Form:             source={d['form_source']} fields={d['form_field_count']} "
        f"fingerprint={d['form_fingerprint'] or '-'}",
        f"Profile fp:       {d['profile_fingerprint']}",
        f"Answers:          {d['answer_count']} prepared "
        f"({'values shown' if include_values else 'values redacted'})",
        f"Unanswered req.:  {', '.join(d['unanswered_required']) or '(none)'}",
        f"High-risk todo:   {', '.join(d['high_risk_pending']) or '(none)'}",
        "",
        f"Approval:         has={d['has_approval']} status={d['approval_status'] or '-'} "
        f"valid={d['approval_valid']}",
    ]
    if d["approval_stale_reasons"]:
        lines.append(f"  stale because:  {'; '.join(d['approval_stale_reasons'])}")
    lines += [
        f"Active blocker:   {d['active_blocker_code'] or '(none)'} {d['active_blocker_title']}",
        "",
        f"READY FOR APPROVAL: {d['ready_for_approval']}",
    ]
    for reason in d["blocking_reasons"]:
        lines.append(f"  - {reason}")
    return "\n".join(lines) + "\n"
