"""Typed enums/dataclasses for the Phase 8 safe ATS application executor.
Kept separate from app.models (Job-level state) and app.providers.capabilities
(discovery provider capabilities) -- these describe EXECUTION concepts: the
fine-grained per-attempt status machine, field-mapping confidence, and
application-provider capability metadata. See
docs/phase8-application-executor.md "Two-layer state model"."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.providers.capabilities import SupportLevel


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionStatus(str, Enum):
    """Fine-grained per-attempt status machine (CLAUDE.md Phase 8 section 4).
    Recorded on application_executions.status. Never written directly to
    jobs.application_state -- app.applications.executor mirrors a coarse
    summary onto the job row instead (see app.applications.repo.
    _JOB_STATE_MIRROR)."""
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    FORM_DISCOVERED = "FORM_DISCOVERED"
    FORM_MAPPED = "FORM_MAPPED"
    FORM_FILLED = "FORM_FILLED"
    VALIDATION_REQUIRED = "VALIDATION_REQUIRED"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    SUBMISSION_READY = "SUBMISSION_READY"
    # Approval-gated-autonomy-v1 (see app.applications.approval): reached
    # only via an explicit, durable APPROVE & APPLY action when the
    # provider has no verified final-submission capability -- distinct from
    # SUBMISSION_READY (which means "prepared, awaiting a human decision").
    # Non-terminal (stays active=1, like NEEDS_USER_ACTION) -- the next
    # real step is browser-assist or manual completion, never an
    # automatic resubmission of this same row.
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_CONFIRMED = "SUBMISSION_CONFIRMED"
    APPLIED = "APPLIED"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    RETRYABLE_SUBMISSION_FAILURE = "RETRYABLE_SUBMISSION_FAILURE"
    PERMANENT_SUBMISSION_FAILURE = "PERMANENT_SUBMISSION_FAILURE"
    DUPLICATE_APPLICATION_BLOCKED = "DUPLICATE_APPLICATION_BLOCKED"
    WITHDRAWN = "WITHDRAWN"
    SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"
    # CLAUDE.md Phase 9 sections 24-27: caught by the final revalidation pass
    # immediately before submission -- the job disappeared, or its JD flipped
    # to non-eligible, between preparation and the submit attempt. Distinct
    # from PERMANENT_SUBMISSION_FAILURE: no submission request was ever sent.
    JOB_NO_LONGER_ACTIVE = "JOB_NO_LONGER_ACTIVE"


# Once an execution reaches one of these, `application_executions.active` is
# flipped to 0 -- it stops blocking a fresh execution attempt (the partial
# unique index only guards active=1 rows) and stops being claimable by the
# queue. WITHDRAWN/APPLIED/DUPLICATE_APPLICATION_BLOCKED/
# PERMANENT_SUBMISSION_FAILURE are final. RETRYABLE_SUBMISSION_FAILURE and
# SUBMISSION_STATUS_UNKNOWN are ALSO terminal for a given execution row (a
# fresh execution attempt gets its own row -- CLAUDE.md section 33/37 "do
# NOT blindly retry" means retry is always a new, explicit, reconciled
# attempt, never resuming the same row in place).
TERMINAL_STATUSES = frozenset({
    ExecutionStatus.APPLIED,
    ExecutionStatus.SUBMISSION_FAILED,
    ExecutionStatus.PERMANENT_SUBMISSION_FAILURE,
    ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED,
    ExecutionStatus.WITHDRAWN,
    ExecutionStatus.JOB_NO_LONGER_ACTIVE,
})

# NEEDS_USER_ACTION/VALIDATION_REQUIRED/SUBMISSION_STATUS_UNKNOWN are
# deliberately NOT terminal -- they stay `active=1` (still blocking a second
# concurrent execution for the same job) until a human resolves them via the
# review queue, matching "duplicate protection" applying to in-flight work
# too, not just completed applications.


class ExecutionMode(str, Enum):
    """CLAUDE.md Phase 8 section 3. ASSIST is always the default -- never a
    generic 'force auto-submit' switch exists anywhere in this module."""
    ASSIST = "ASSIST"
    AUTO_PERMITTED = "AUTO_PERMITTED"


class AutomationPolicy(str, Enum):
    """CLAUDE.md Phase 8 section 7."""
    PERMITTED_AUTO = "PERMITTED_AUTO"
    ASSIST_ONLY = "ASSIST_ONLY"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"


class PolicyReason(str, Enum):
    """CLAUDE.md Phase 8 section 7."""
    NONE = "NONE"
    CAPTCHA_PRESENT = "CAPTCHA_PRESENT"
    MFA_REQUIRED = "MFA_REQUIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PLATFORM_POLICY_RESTRICTED = "PLATFORM_POLICY_RESTRICTED"
    UNKNOWN_LEGAL_QUESTION = "UNKNOWN_LEGAL_QUESTION"
    UNKNOWN_DEMOGRAPHIC_QUESTION = "UNKNOWN_DEMOGRAPHIC_QUESTION"
    FILE_UPLOAD_UNSUPPORTED = "FILE_UPLOAD_UNSUPPORTED"
    FORM_SCHEMA_CHANGED = "FORM_SCHEMA_CHANGED"
    SUBMISSION_INTERFACE_UNSUPPORTED = "SUBMISSION_INTERFACE_UNSUPPORTED"
    UNRESOLVED_REQUIRED_FIELD = "UNRESOLVED_REQUIRED_FIELD"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    DUPLICATE = "DUPLICATE"
    RATE_LIMITED = "RATE_LIMITED"
    # Application-lifecycle-exception-resume-v1: distinct from the broader
    # AUTH_REQUIRED/MFA_REQUIRED -- a candidate ACCOUNT (not merely a login)
    # must be created, or an emailed verification link/code must be
    # completed, before the form can proceed. Never inferred/guessed; only
    # ever set when the provider genuinely exposes this (see
    # MockATSProvider's "account_creation_required"/"email_verification"
    # scenarios and app.applications.blockers.from_browser_session_status's
    # page-text classification for the browser-assist path).
    ACCOUNT_CREATION_REQUIRED = "ACCOUNT_CREATION_REQUIRED"
    EMAIL_VERIFICATION_REQUIRED = "EMAIL_VERIFICATION_REQUIRED"


class FieldCategory(str, Enum):
    """CLAUDE.md Phase 8 section 8."""
    CONTACT = "CONTACT"
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    SPONSORSHIP = "SPONSORSHIP"
    EMPLOYMENT = "EMPLOYMENT"
    EDUCATION = "EDUCATION"
    EXPERIENCE = "EXPERIENCE"
    SKILLS = "SKILLS"
    LOCATION = "LOCATION"
    RELOCATION = "RELOCATION"
    SALARY = "SALARY"
    NOTICE_PERIOD = "NOTICE_PERIOD"
    PROJECTS = "PROJECTS"
    DEMOGRAPHICS = "DEMOGRAPHICS"
    VOLUNTARY_DISCLOSURE = "VOLUNTARY_DISCLOSURE"
    LEGAL_ATTESTATION = "LEGAL_ATTESTATION"
    CUSTOM_TEXT = "CUSTOM_TEXT"
    FILE_UPLOAD = "FILE_UPLOAD"
    CONSENT = "CONSENT"
    SIGNATURE = "SIGNATURE"


class FieldConfidence(str, Enum):
    """CLAUDE.md Phase 8 section 15."""
    EXACT = "EXACT"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# Categories that section 12 ("legal / attestation questions ... must NEVER
# be guessed") and section 11 ("never infer demographic answers") apply to.
# Auto-fill for these categories requires EXACT/HIGH confidence AND an
# explicit verified value -- MEDIUM is never enough here even though MEDIUM
# is allowed to auto-fill for an ordinary CONTACT/EMPLOYMENT field.
SENSITIVE_CATEGORIES = frozenset({
    FieldCategory.DEMOGRAPHICS,
    FieldCategory.VOLUNTARY_DISCLOSURE,
    FieldCategory.LEGAL_ATTESTATION,
    FieldCategory.SIGNATURE,
})


@dataclass
class ApplicationField:
    """CLAUDE.md Phase 8 section 8."""
    field_id: str
    label: str
    category: FieldCategory
    normalized_type: str  # "text" | "select" | "boolean" | "file" | "textarea"
    required: bool = False
    choices: list[str] = field(default_factory=list)
    value_source: str = ""       # e.g. "candidate_profile.contact.email"
    verified_value: Optional[str] = None
    confidence: FieldConfidence = FieldConfidence.LOW
    needs_user_input: bool = False
    sensitive: bool = False
    auto_fill_allowed: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "field_id": self.field_id, "label": self.label, "category": self.category.value,
            "normalized_type": self.normalized_type, "required": self.required, "choices": self.choices,
            "value_source": self.value_source, "verified_value": self.verified_value,
            "confidence": self.confidence.value, "needs_user_input": self.needs_user_input,
            "sensitive": self.sensitive, "auto_fill_allowed": self.auto_fill_allowed, "reason": self.reason,
        }


@dataclass(frozen=True)
class ApplicationCapabilities:
    """CLAUDE.md Phase 8 section 6. Analogous to
    app.providers.capabilities.ProviderCapabilities but for the APPLICATION
    side -- never inflate, matching the same durable rule."""
    provider: str
    provider_version: str
    form_discovery_supported: bool
    field_mapping_supported: bool
    draft_fill_supported: bool
    file_upload_supported: bool
    submission_supported: bool
    confirmation_detection_supported: bool
    automation_policy: AutomationPolicy
    support_level: SupportLevel
    live_validated: bool = False
    notes: str = ""
    # CLAUDE.md Phase 9 section 8: whether this provider exposes a genuine,
    # legitimate way to re-check a specific application's status after the
    # fact (e.g. a provider-side status lookup) -- used ONLY by
    # app.applications.reconcile_worker to gather real evidence for a
    # SUBMISSION_STATUS_UNKNOWN execution. False for every real ATS adapter
    # in this phase (none expose such an interface to candidates); True only
    # for the deterministic MockATSProvider fixture, to prove the mechanism.
    confirmation_recheck_supported: bool = False

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "provider_version": self.provider_version,
            "form_discovery_supported": self.form_discovery_supported,
            "field_mapping_supported": self.field_mapping_supported,
            "draft_fill_supported": self.draft_fill_supported,
            "file_upload_supported": self.file_upload_supported,
            "submission_supported": self.submission_supported,
            "confirmation_detection_supported": self.confirmation_detection_supported,
            "automation_policy": self.automation_policy.value,
            "support_level": self.support_level.value,
            "live_validated": self.live_validated,
            "notes": self.notes,
            "confirmation_recheck_supported": self.confirmation_recheck_supported,
        }


@dataclass
class FormField:
    name: str
    label: str
    field_type: str  # "input_text" | "input_file" | "textarea" | "multi_value_single_select" | "boolean"
    required: bool = False
    choices: list[str] = field(default_factory=list)


@dataclass
class FormSnapshot:
    """CLAUDE.md Phase 8 section 16. Never contains passwords/tokens --
    field names/labels/types/required flags/choice sets only."""
    provider: str
    tenant_identifier: str
    external_job_id: str
    fields: list[FormField] = field(default_factory=list)
    fingerprint: str = ""
    captcha_present: bool = False
    mfa_required: bool = False
    auth_required: bool = False
    # Application-lifecycle-exception-resume-v1: same "only when genuinely
    # exposed" contract as auth_required/mfa_required above -- never set
    # unless the provider genuinely detected this on the real form.
    account_creation_required: bool = False
    email_verification_required: bool = False
    # CLAUDE.md Phase 9 section 29: how many pages/steps the real form has,
    # when genuinely known -- never guessed for a real provider that doesn't
    # expose this; defaults to 1 (a single-page form, the common case).
    total_steps: int = 1
    discovered_at: str = field(default_factory=utcnow)

    def field_signature(self) -> list[dict]:
        return [
            {"name": f.name, "label": f.label, "type": f.field_type, "required": f.required,
             "choices": sorted(f.choices)}
            for f in self.fields
        ]


@dataclass
class MappedField:
    form_field: FormField
    application_field: Optional[ApplicationField]
    confidence: FieldConfidence
    fill_value: Optional[str] = None
    will_fill: bool = False
    reason: str = ""


@dataclass
class MappingResult:
    mapped: list[MappedField] = field(default_factory=list)
    unmapped_required: list[FormField] = field(default_factory=list)


@dataclass
class DraftResult:
    """CLAUDE.md Phase 8 section 6 ("fill_draft()"). The in-memory filled
    draft -- never itself a submission. `preserved` is True when the draft
    could be safely retained (e.g. after a CAPTCHA stop) for the user to
    finish manually."""
    mapping: MappingResult
    filled_field_ids: list[str] = field(default_factory=list)
    unresolved_field_ids: list[str] = field(default_factory=list)
    file_uploads_ready: list[str] = field(default_factory=list)
    preserved: bool = True


@dataclass
class ValidationResult:
    ok: bool
    policy: AutomationPolicy
    policy_reasons: list[PolicyReason] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)


@dataclass
class SubmitResult:
    success: bool
    confirmation_id: str = ""
    confirmation_url: str = ""
    confirmation_text: str = ""
    status_unknown: bool = False
    error_type: str = ""
    error_message_safe: str = ""


@dataclass
class ConfirmationResult:
    confirmed: bool
    confirmation_id: str = ""
    confirmation_url: str = ""
    confirmation_text_fingerprint: str = ""
