"""Lever application-form adapter (CLAUDE.md Phase 8 section 25).

Live-checked during this phase's development: Lever's public postings API
(`api.lever.co/v0/postings/{site}?mode=json`) exposes only
`hostedUrl`/`applyUrl` for a posting -- no structured custom-question schema
is present anywhere in the response. Unlike Greenhouse, there is no
documented public endpoint that returns Lever's actual application field
list, so form discovery is honestly UNSUPPORTED here rather than guessed
from a hardcoded "typical Lever form" template (which would risk silently
going stale or simply being wrong -- CLAUDE.md's "do not inflate
capabilities" rule). This adapter only ever hands back the known apply URL
for the human to complete manually."""

from typing import Optional

from app.applications.models import (
    ApplicationCapabilities,
    AutomationPolicy,
    ConfirmationResult,
    DraftResult,
    FormSnapshot,
    MappingResult,
    SubmitResult,
    SupportLevel,
    ValidationResult,
)
from app.applications.provider import ApplicationProvider
from app.models import Job


class LeverApplicationProvider(ApplicationProvider):
    name = "lever"
    capabilities = ApplicationCapabilities(
        provider="lever", provider_version="1.0.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.UNSUPPORTED,
        live_validated=True,
        notes=(
            "Live-checked: Lever's public postings API exposes only hostedUrl/applyUrl, "
            "no structured question schema -- form discovery genuinely UNSUPPORTED, not "
            "guessed. ASSIST_ONLY: only the apply URL is handed to the candidate."
        ),
    )

    def detect_application(self, job: Job) -> bool:
        return (job.provider or "").lower() == "lever"

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        return None

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        return MappingResult()

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        return DraftResult(mapping=mapping, preserved=False)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        from app.applications.models import PolicyReason

        return ValidationResult(
            ok=False, policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=["Lever form structure is not discoverable via any public API -- open the apply URL manually."],
        )

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        return SubmitResult(success=False, error_type="SUBMISSION_INTERFACE_UNSUPPORTED",
                             error_message_safe="lever: submission not supported.")

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        return ConfirmationResult(confirmed=False)
