"""Deterministic local mock ATS (CLAUDE.md Phase 8 section 52). No network
access, no real ATS involved -- exists purely so the executor's mechanics
(discover -> map -> fill -> validate -> submit -> confirm) can be tested
end-to-end without ever touching a real production system. This is the ONLY
ApplicationProvider in this phase with submission_supported=True.

Scenario selection: a job opts into a scenario by setting
`provider_metadata` (JSON) `{"mock_scenario": "<name>"}` and `provider` =
"mock_ats". Supported scenario names map 1:1 to CLAUDE.md Phase 8 section 52's
list."""

import json

from app.applications.mapping import match_field
from app.applications.models import (
    ApplicationCapabilities,
    AutomationPolicy,
    ConfirmationResult,
    DraftResult,
    FieldConfidence,
    FormField,
    FormSnapshot,
    MappedField,
    MappingResult,
    PolicyReason,
    SubmitResult,
    SupportLevel,
    ValidationResult,
)
from app.applications.provider import ApplicationProvider
from app.applications.schema import DECLINE_TO_SELF_IDENTIFY_PHRASES, find_field
from app.models import Job

PROVIDER_NAME = "mock_ats"

_BASE_FIELDS = [
    FormField("first_name", "First Name", "input_text", required=True),
    FormField("last_name", "Last Name", "input_text", required=True),
    FormField("email", "Email", "input_text", required=True),
    FormField("phone", "Phone", "input_text", required=False),
    FormField("resume", "Resume/CV", "input_file", required=True),
]

_SPONSORSHIP_FIELD = FormField(
    "sponsorship_q", "Will you now or in the future require sponsorship?",
    "multi_value_single_select", required=True, choices=["Yes", "No"],
)

_DEMOGRAPHIC_FIELD = FormField(
    "veteran_q", "Veteran Status", "multi_value_single_select", required=False,
    choices=["I am a veteran", "I am not a veteran", "I don't wish to answer"],
)

_LEGAL_UNKNOWN_FIELD = FormField(
    "non_compete_q", "Are you subject to any employment agreements and/or post-employment restrictions?",
    "multi_value_single_select", required=True, choices=["Yes", "No"],
)


def _scenario_for(job: Job) -> str:
    try:
        meta = json.loads(job.provider_metadata or "{}")
    except (ValueError, TypeError):
        meta = {}
    return meta.get("mock_scenario", "simple")


def _fields_for_scenario(scenario: str) -> list[FormField]:
    fields = list(_BASE_FIELDS)
    if scenario == "sponsorship_question":
        fields.append(_SPONSORSHIP_FIELD)
    elif scenario == "demographic_question":
        fields.append(_DEMOGRAPHIC_FIELD)
    elif scenario == "legal_unknown":
        fields.append(_LEGAL_UNKNOWN_FIELD)
    elif scenario == "required_fields":
        fields.append(FormField("cover_letter", "Cover Letter", "input_file", required=True))
    elif scenario == "file_upload":
        fields.append(FormField("resume_text", "Resume Text", "textarea", required=False))
    return fields


class MockATSProvider(ApplicationProvider):
    name = PROVIDER_NAME
    capabilities = ApplicationCapabilities(
        provider=PROVIDER_NAME, provider_version="1.0.0",
        form_discovery_supported=True, field_mapping_supported=True,
        draft_fill_supported=True, file_upload_supported=True,
        submission_supported=True, confirmation_detection_supported=True,
        automation_policy=AutomationPolicy.PERMITTED_AUTO, support_level=SupportLevel.FULL,
        live_validated=False,
        notes="Deterministic in-process fixture ATS for executor testing only -- never a real ATS.",
    )

    def detect_application(self, job: Job) -> bool:
        return (job.provider or "").lower() == PROVIDER_NAME

    def discover_form(self, job: Job) -> FormSnapshot | None:
        from app.applications.fingerprint import compute_fingerprint

        scenario = _scenario_for(job)
        fields = _fields_for_scenario(scenario)
        snap = FormSnapshot(
            provider=PROVIDER_NAME, tenant_identifier=job.company, external_job_id=job.external_job_id,
            fields=fields, captcha_present=(scenario == "captcha"), mfa_required=(scenario == "mfa"),
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

            if af.category.value == "DEMOGRAPHICS" and not af.auto_fill_allowed and m.form_field.choices:
                decline = next(
                    (c for c in m.form_field.choices
                     if any(p in c.lower().replace("'", "") for p in DECLINE_TO_SELF_IDENTIFY_PHRASES)),
                    None,
                )
                if decline:
                    m.fill_value = decline
                    m.will_fill = True
                    m.confidence = FieldConfidence.HIGH
                    filled.append(m.form_field.name)
                    continue
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue

            if not af.auto_fill_allowed:
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue

            if m.form_field.field_type == "input_file":
                if af.verified_value:
                    m.fill_value = af.verified_value
                    m.will_fill = True
                    uploads.append(m.form_field.name)
                    filled.append(m.form_field.name)
                elif m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue

            value = af.verified_value
            if m.form_field.choices and value not in m.form_field.choices:
                # Value doesn't match one of the form's own offered choices --
                # never force-select a plausible-looking option (section 14:
                # no unsafe fuzzy matching for legal/choice fields).
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue

            m.fill_value = value
            m.will_fill = True
            filled.append(m.form_field.name)

        return DraftResult(mapping=mapping, filled_field_ids=filled, unresolved_field_ids=unresolved,
                            file_uploads_ready=uploads)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        detail: list[str] = []
        reasons: list[PolicyReason] = []

        if form.captcha_present:
            reasons.append(PolicyReason.CAPTCHA_PRESENT)
            detail.append("CAPTCHA detected on application form.")
        if form.mfa_required:
            reasons.append(PolicyReason.MFA_REQUIRED)
            detail.append("MFA/login required to submit.")

        for name in draft.unresolved_field_ids:
            mf = next((m for m in draft.mapping.mapped if m.form_field.name == name), None)
            if mf is None:
                reasons.append(PolicyReason.UNRESOLVED_REQUIRED_FIELD)
                continue
            cat = mf.application_field.category.value if mf.application_field else ""
            if cat == "LEGAL_ATTESTATION" or mf.form_field is _LEGAL_UNKNOWN_FIELD or "non_compete" in name:
                reasons.append(PolicyReason.UNKNOWN_LEGAL_QUESTION)
                detail.append(f"Unresolved legal/attestation question: '{mf.form_field.label}'.")
            elif cat == "DEMOGRAPHICS":
                reasons.append(PolicyReason.UNKNOWN_DEMOGRAPHIC_QUESTION)
                detail.append(f"Unresolved demographic question with no safe default offered: '{mf.form_field.label}'.")
            elif mf.form_field.field_type == "input_file":
                reasons.append(PolicyReason.FILE_UPLOAD_UNSUPPORTED)
                detail.append(f"Required file upload not available: '{mf.form_field.label}'.")
            else:
                reasons.append(PolicyReason.UNRESOLVED_REQUIRED_FIELD)
                detail.append(f"Unresolved required field: '{mf.form_field.label}'.")

        if reasons:
            return ValidationResult(ok=False, policy=AutomationPolicy.USER_ACTION_REQUIRED,
                                     policy_reasons=sorted(set(reasons), key=lambda r: r.value), detail=detail)

        return ValidationResult(ok=True, policy=AutomationPolicy.PERMITTED_AUTO, policy_reasons=[], detail=detail)

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        scenario = _scenario_for(job)
        if scenario == "timeout_after_submit":
            return SubmitResult(
                success=False, status_unknown=True, error_type="TIMEOUT",
                error_message_safe="mock_ats: request sent but no response received before timeout.",
            )
        confirmation_id = f"MOCK-{job.id}-{job.external_job_id or 'X'}"
        return SubmitResult(
            success=True, confirmation_id=confirmation_id,
            confirmation_url=f"https://mock-ats.local/applications/{confirmation_id}",
            confirmation_text="Thank you -- your application has been received.",
        )

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        if not submit_result.success or not submit_result.confirmation_id:
            return ConfirmationResult(confirmed=False)
        import hashlib

        fp = hashlib.sha256(submit_result.confirmation_text.encode("utf-8")).hexdigest()[:24]
        return ConfirmationResult(
            confirmed=True, confirmation_id=submit_result.confirmation_id,
            confirmation_url=submit_result.confirmation_url, confirmation_text_fingerprint=fp,
        )
