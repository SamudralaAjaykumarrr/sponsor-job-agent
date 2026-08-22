"""Greenhouse application-form adapter (CLAUDE.md Phase 8 section 24).

Form discovery is LIVE-VALIDATED against the real, public, unauthenticated
Greenhouse Job Board API endpoint
`https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`
(https://developers.greenhouse.io/job-board.html) -- confirmed during this
phase's own development to return genuine structured application-question
fields (name/label/type/required/choices), including the EEOC demographic
questions and, on the specific posting checked, a real sponsorship question.
This is the same officially documented read API the discovery connector
(app.providers.greenhouse) already uses, just with the `questions=true` flag.

Submission is explicitly NOT implemented. The actual "apply" action on a
Greenhouse job board goes through the site's own embedded, CSRF-protected
form flow -- not the documented public Job Board API -- so automating it
would mean reverse-engineering an undocumented interface rather than using
one "explicitly permitted" for programmatic use. Per CLAUDE.md's own
instruction ("If Greenhouse submission requires interfaces that should not
be automated without explicit permission: mark ASSIST_ONLY"), this adapter
stays ASSIST_ONLY / submission_supported=False."""

import logging
from typing import Optional

import httpx

from app.applications.mapping import match_field
from app.applications.models import (
    ApplicationCapabilities,
    AutomationPolicy,
    DraftResult,
    FieldConfidence,
    FormField,
    FormSnapshot,
    MappedField,
    MappingResult,
    PolicyReason,
    SupportLevel,
    ValidationResult,
)
from app.applications.provider import ApplicationProvider
from app.applications.schema import DECLINE_TO_SELF_IDENTIFY_PHRASES, find_field
from app.config import PROVIDER_HTTP_TIMEOUT_SECONDS
from app.models import Job
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("applications.greenhouse")

GREENHOUSE_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"

_TYPE_MAP = {
    "input_text": "input_text",
    "input_file": "input_file",
    "textarea": "textarea",
    "multi_value_single_select": "multi_value_single_select",
}


def _extract_fields(payload: dict) -> list[FormField]:
    fields: list[FormField] = []
    for q in payload.get("questions", []) or []:
        label = q.get("label") or ""
        required = bool(q.get("required"))
        for f in q.get("fields", []) or []:
            name = f.get("name") or label
            ftype = _TYPE_MAP.get(f.get("type"), f.get("type") or "input_text")
            choices = [v.get("label", "") for v in (f.get("values") or [])]
            fields.append(FormField(name=name, label=label, field_type=ftype, required=required, choices=choices))
    return fields


class GreenhouseApplicationProvider(ApplicationProvider):
    name = "greenhouse"
    capabilities = ApplicationCapabilities(
        provider="greenhouse", provider_version="1.0.0",
        form_discovery_supported=True, field_mapping_supported=True,
        draft_fill_supported=True, file_upload_supported=True,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.PARTIAL,
        live_validated=True,
        notes=(
            "Form discovery live-verified against the public boards-api.greenhouse.io "
            "?questions=true Job Board API (real structured fields, including EEOC "
            "demographic questions and, on the posting checked, a sponsorship question). "
            "Submission is NOT implemented -- Greenhouse's actual apply flow is not a "
            "documented public API for programmatic use, so ASSIST_ONLY per CLAUDE.md "
            "Phase 8 section 24."
        ),
    )

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client

    def detect_application(self, job: Job) -> bool:
        return (job.provider or "").lower() == "greenhouse" and bool(job.external_job_id) and bool(job.company_identifier)

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        from app.applications.fingerprint import compute_fingerprint

        if not self.detect_application(job):
            return None
        client = self._client or build_client(PROVIDER_HTTP_TIMEOUT_SECONDS)
        owns_client = self._client is None
        try:
            url = GREENHOUSE_JOB_URL.format(token=job.company_identifier, job_id=job.external_job_id)
            try:
                payload = get_json(client, url, provider="greenhouse-application", params={"questions": "true"})
            except ProviderHTTPError as exc:
                logger.warning("greenhouse application form discovery failed for job %s: %s", job.id, exc)
                return None
        finally:
            if owns_client:
                client.close()

        fields = _extract_fields(payload)
        if not fields:
            return None
        snap = FormSnapshot(
            provider="greenhouse", tenant_identifier=job.company_identifier, external_job_id=job.external_job_id,
            fields=fields,
        )
        snap.fingerprint = compute_fingerprint(snap)
        return snap

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        mapped: list[MappedField] = []
        unmapped_required: list[FormField] = []
        for ff in form.fields:
            field_id, confidence = match_field(ff.label, ff.name)
            app_field = find_field(application_fields, field_id) if field_id else None
            mapped.append(MappedField(form_field=ff, application_field=app_field, confidence=confidence))
            if ff.required and app_field is None:
                unmapped_required.append(ff)
        return MappingResult(mapped=mapped, unmapped_required=unmapped_required)

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        filled: list[str] = []
        unresolved: list[str] = []
        uploads: list[str] = []
        for m in mapping.mapped:
            af = m.application_field
            if af is None:
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            if not af.auto_fill_allowed:
                if af.category.value == "DEMOGRAPHICS" and m.form_field.choices:
                    decline = next(
                        (c for c in m.form_field.choices
                         if any(p in c.lower().replace("'", "") for p in DECLINE_TO_SELF_IDENTIFY_PHRASES)),
                        None,
                    )
                    if decline:
                        m.fill_value, m.will_fill, m.confidence = decline, True, FieldConfidence.HIGH
                        filled.append(m.form_field.name)
                        continue
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            if m.form_field.field_type == "input_file":
                if af.verified_value:
                    m.fill_value, m.will_fill = af.verified_value, True
                    uploads.append(m.form_field.name)
                    filled.append(m.form_field.name)
                elif m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            value = af.verified_value
            if m.form_field.choices and value not in m.form_field.choices:
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            m.fill_value, m.will_fill = value, True
            filled.append(m.form_field.name)
        return DraftResult(mapping=mapping, filled_field_ids=filled, unresolved_field_ids=unresolved,
                            file_uploads_ready=uploads)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        # ASSIST_ONLY unconditionally -- see class docstring. Still reports
        # exactly what remains unresolved so the human review queue is useful.
        detail = [f"Unresolved field: '{name}'" for name in draft.unresolved_field_ids]
        return ValidationResult(
            ok=len(draft.unresolved_field_ids) == 0,
            policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=detail or ["Draft fully prepared -- submission requires the candidate to complete it manually."],
        )
