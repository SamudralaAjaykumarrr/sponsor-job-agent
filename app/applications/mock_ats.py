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
from app.db import db_session
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

_VISA_TYPE_FIELD = FormField(
    "visa_type_q", "What type of visa sponsorship would you require?",
    "multi_value_single_select", required=False, choices=["H-1B", "TN", "O-1", "Other"],
)

_DEMOGRAPHIC_FIELD = FormField(
    "veteran_q", "Veteran Status", "multi_value_single_select", required=False,
    choices=["I am a veteran", "I am not a veteran", "I don't wish to answer"],
)

_LEGAL_UNKNOWN_FIELD = FormField(
    "non_compete_q", "Are you subject to any employment agreements and/or post-employment restrictions?",
    "multi_value_single_select", required=True, choices=["Yes", "No"],
)

# Application-lifecycle-exception-resume-v1 "Demo Unknown Question": a plain
# custom required text question with no verified-profile mapping and no
# category-specific handling (unlike the legal/demographic/file-upload
# fields above) -- exercises the generic UNRESOLVED_REQUIRED_FIELD ->
# NEEDS_USER_INPUT path distinctly from those other, more specific ones.
_UNKNOWN_CUSTOM_FIELD = FormField(
    "referral_program_q", "Which internal employee referred you to this role?",
    "input_text", required=True,
)

# CLAUDE.md Phase 9 section 41: a "page 2" set of fields for the multi_page
# scenario, so app.applications.schema/mapping's field-mapping engine is
# exercised against a form whose fields don't all arrive in one flat list
# conceptually -- FormSnapshot.total_steps records how many pages the real
# form would have had (surfaced for dashboard/reporting per section 29).
_PAGE_TWO_FIELDS = [
    FormField("education_school", "School", "input_text", required=False),
    FormField("education_degree", "Degree", "input_text", required=False),
    FormField("linkedin_url", "LinkedIn Profile", "input_text", required=False),
]


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
    elif scenario == "conditional_sponsorship":
        fields.append(_SPONSORSHIP_FIELD)
        fields.append(_VISA_TYPE_FIELD)
    elif scenario == "demographic_question":
        fields.append(_DEMOGRAPHIC_FIELD)
    elif scenario == "legal_unknown":
        fields.append(_LEGAL_UNKNOWN_FIELD)
    elif scenario == "unknown_question":
        fields.append(_UNKNOWN_CUSTOM_FIELD)
    elif scenario == "required_fields":
        fields.append(FormField("cover_letter", "Cover Letter", "input_file", required=True))
    elif scenario == "file_upload":
        fields.append(FormField("resume_text", "Resume Text", "textarea", required=False))
    elif scenario == "multi_page":
        fields.extend(_PAGE_TWO_FIELDS)
    return fields


def _record_server_side_submission(job: Job, confirmation_id: str) -> None:
    """Simulates the mock ATS's OWN server-side record of a received
    application -- genuinely separate storage from application_executions,
    written even when the CLIENT never observed a successful response
    (timeout_after_submit). This is what makes
    MockATSProvider.check_submission_status() a real evidence lookup rather
    than a fabricated confirmation (CLAUDE.md Phase 9 section 8)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO mock_ats_server_records (job_id, external_job_id, confirmation_id, received_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET confirmation_id=excluded.confirmation_id,
                 received_at=excluded.received_at""",
            (job.id, job.external_job_id, confirmation_id, now),
        )


class MockATSProvider(ApplicationProvider):
    name = PROVIDER_NAME
    capabilities = ApplicationCapabilities(
        provider=PROVIDER_NAME, provider_version="1.0.0",
        form_discovery_supported=True, field_mapping_supported=True,
        draft_fill_supported=True, file_upload_supported=True,
        submission_supported=True, confirmation_detection_supported=True,
        automation_policy=AutomationPolicy.PERMITTED_AUTO, support_level=SupportLevel.FULL,
        live_validated=False, confirmation_recheck_supported=True,
        notes="Deterministic in-process fixture ATS for executor testing only -- never a real ATS.",
    )

    def detect_application(self, job: Job) -> bool:
        return (job.provider or "").lower() == PROVIDER_NAME

    def discover_form(self, job: Job) -> FormSnapshot | None:
        from app.applications.fingerprint import compute_fingerprint

        scenario = _scenario_for(job)
        if scenario == "form_not_found":
            return None
        fields = _fields_for_scenario(scenario)
        snap = FormSnapshot(
            provider=PROVIDER_NAME, tenant_identifier=job.company, external_job_id=job.external_job_id,
            fields=fields, captcha_present=(scenario == "captcha"), mfa_required=(scenario == "mfa"),
            auth_required=(scenario == "login_required"),
            account_creation_required=(scenario == "account_creation_required"),
            email_verification_required=(scenario == "email_verification"),
            total_steps=2 if scenario == "multi_page" else 1,
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

    def validate(self, job: Job, form: FormSnapshot | None, draft: DraftResult) -> ValidationResult:
        if form is None:
            # discover_form() returned None (e.g. the "form_not_found"
            # scenario) -- honestly ASSIST_ONLY, matching every other
            # provider's behavior when form discovery itself failed.
            return ValidationResult(
                ok=False, policy=AutomationPolicy.ASSIST_ONLY,
                policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
                detail=["Application form could not be discovered for this posting."],
            )

        detail: list[str] = []
        reasons: list[PolicyReason] = []
        # Application-lifecycle-exception-resume-v1 "Demo Unknown Question":
        # answering the question must never change the FORM'S OWN SHAPE
        # (that would be a genuine, separately-detected FORM_SCHEMA_CHANGED
        # condition, not "the user answered") -- so resolution is a metadata
        # flag, never a scenario switch that removes the field.
        try:
            meta = json.loads(job.provider_metadata or "{}")
        except (ValueError, TypeError):
            meta = {}
        demo_answered = bool(meta.get("demo_answered"))

        if form.captcha_present:
            reasons.append(PolicyReason.CAPTCHA_PRESENT)
            detail.append("CAPTCHA detected on application form.")
        if form.mfa_required:
            reasons.append(PolicyReason.MFA_REQUIRED)
            detail.append("MFA/login required to submit.")
        if form.auth_required:
            reasons.append(PolicyReason.AUTH_REQUIRED)
            detail.append("A candidate account/login is required to submit this application.")
        if form.account_creation_required:
            reasons.append(PolicyReason.ACCOUNT_CREATION_REQUIRED)
            detail.append("A new candidate account must be created to submit this application.")
        if form.email_verification_required:
            reasons.append(PolicyReason.EMAIL_VERIFICATION_REQUIRED)
            detail.append("Email verification is required to submit this application.")

        for name in draft.unresolved_field_ids:
            if name == _UNKNOWN_CUSTOM_FIELD.name and demo_answered:
                continue
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
        if scenario == "timeout_before_submit":
            # No server-side record -- the request never reached the mock
            # ATS at all, unlike timeout_after_submit below.
            return SubmitResult(
                success=False, status_unknown=True, error_type="TIMEOUT",
                error_message_safe="mock_ats: request timed out before reaching the server.",
            )
        if scenario == "rate_limited":
            return SubmitResult(success=False, error_type="RATE_LIMITED",
                                 error_message_safe="mock_ats: 429 Too Many Requests.")
        if scenario == "service_unavailable":
            return SubmitResult(success=False, error_type="TEMPORARY_HTTP",
                                 error_message_safe="mock_ats: 503 Service Unavailable.")
        if scenario == "transient_then_recovers":
            # Autonomous-ux-reliability-v1: deterministically fails with a
            # RETRYABLE error_type (app.applications.models.
            # SUBMIT_RETRYABLE_ERROR_TYPES) on the first submit attempt, then
            # succeeds -- demonstrates the bounded submit-retry path
            # (executor.process_execution -> ExecutionStatus.
            # RETRYABLE_SUBMISSION_FAILURE -> reclaimed after backoff ->
            # retried -> succeeds) end to end without ever touching a real
            # network. `attempt_count` already reflects THIS attempt (the
            # executor increments and persists it immediately before calling
            # submit()), so >= 2 means this is a retry.
            with db_session() as conn:
                row = conn.execute(
                    "SELECT attempt_count FROM application_executions WHERE job_id = ? AND active = 1",
                    (job.id,),
                ).fetchone()
            attempt_count = row["attempt_count"] if row else 1
            if attempt_count < 2:
                return SubmitResult(success=False, error_type="TEMPORARY_HTTP",
                                     error_message_safe="mock_ats: 503 Service Unavailable (recovers on retry).")
            # Falls through to the ordinary success path below.
        if scenario == "rejection":
            return SubmitResult(success=False, error_type="SUBMISSION_REJECTED",
                                 error_message_safe="mock_ats: application rejected by ATS-side validation.")
        if scenario == "duplicate_application":
            return SubmitResult(success=False, error_type="SUBMISSION_REJECTED",
                                 error_message_safe="mock_ats: an application for this candidate already exists.")

        confirmation_id = f"MOCK-{job.id}-{job.external_job_id or 'X'}"
        if scenario == "timeout_after_submit":
            # CLAUDE.md Phase 9 section 7/41: the request DID reach the
            # server (recorded below) but the client never observed the
            # response -- a genuine "may have gone through" case, distinct
            # from timeout_before_submit above.
            _record_server_side_submission(job, confirmation_id)
            return SubmitResult(
                success=False, status_unknown=True, error_type="TIMEOUT",
                error_message_safe="mock_ats: request sent but no response received before timeout.",
            )
        _record_server_side_submission(job, confirmation_id)
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

    def check_submission_status(self, job: Job, execution: dict) -> ConfirmationResult | None:
        """CLAUDE.md Phase 9 section 8: genuine evidence lookup against the
        mock ATS's own server-side record table -- returns confirmed=True
        only when a real row exists (e.g. after timeout_after_submit),
        confirmed=False when the mock ATS genuinely has no record (a real
        negative), never a guess."""
        with db_session() as conn:
            row = conn.execute(
                "SELECT * FROM mock_ats_server_records WHERE job_id = ?", (job.id,)
            ).fetchone()
        if row is None:
            return ConfirmationResult(confirmed=False)
        return ConfirmationResult(
            confirmed=True, confirmation_id=row["confirmation_id"],
            confirmation_url=f"https://mock-ats.local/applications/{row['confirmation_id']}",
        )

    def check_job_still_active(self, job: Job) -> bool | None:
        scenario = _scenario_for(job)
        if scenario in ("job_removed", "job_expired", "application_closed"):
            return False
        return True

    def classify_job_inactive_reason(self, job: Job) -> str | None:
        """Application-lifecycle-exception-resume-v1: the mock ATS genuinely
        knows which of the three terminal reasons a scenario represents (it
        chose the scenario itself), unlike a real provider -- see
        app.applications.provider.ApplicationProvider.classify_job_inactive_reason's
        default-None contract for every real adapter."""
        scenario = _scenario_for(job)
        return {"job_removed": "REMOVED", "job_expired": "EXPIRED", "application_closed": "CLOSED"}.get(scenario)
