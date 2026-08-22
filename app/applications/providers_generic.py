"""Generic ASSIST_ONLY fallback adapter (CLAUDE.md Phase 8 sections 26-28).

Covers every ATS this phase has a discovery connector for but did NOT live-
validate an application-form-discovery interface for: Ashby, Workable,
SmartRecruiters, BambooHR, Breezy, Recruitee, Comeet, Teamtailor, Workday,
and the discovery-only "unsupported" connectors (Jobvite/Pinpoint/JazzHR/
iCIMS/Oracle Recruiting). Honestly reports UNSUPPORTED form discovery rather
than claiming a capability that was never tested -- matching CLAUDE.md's
"Do not claim submission support unless actually tested" and Workday's
explicit instruction not to build "a fake universal auto-apply system".

The candidate still gets full value from everything ELSE the executor
prepared (tailored resume, cover letter, mapped answers) via the one-click
application package (CLAUDE.md section 41) -- this adapter's only job is to
hand back the apply URL and say "open this and finish it yourself"."""

from typing import Optional

from app.applications.models import (
    ApplicationCapabilities,
    AutomationPolicy,
    ConfirmationResult,
    DraftResult,
    FormSnapshot,
    MappingResult,
    PolicyReason,
    SubmitResult,
    SupportLevel,
    ValidationResult,
)
from app.applications.provider import ApplicationProvider
from app.models import Job

# Every provider name this adapter is willing to claim (i.e. "at least I know
# your ATS, I just can't automate the form for it yet") -- anything else
# still falls through to this same class via the registry's default, but is
# listed here for clarity/tests.
KNOWN_ASSIST_ONLY_PROVIDERS = frozenset({
    "ashby", "workable", "smartrecruiters", "bamboohr", "breezy", "recruitee",
    "comeet", "teamtailor", "workday", "jobvite", "pinpoint", "jazzhr", "icims", "oracle",
})


class GenericAssistOnlyProvider(ApplicationProvider):
    name = "generic"
    capabilities = ApplicationCapabilities(
        provider="generic", provider_version="1.0.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.UNSUPPORTED,
        live_validated=False,
        notes=(
            "No live-validated public application-form-discovery interface for this ATS "
            "in this phase. Prepares the full package (resume/cover letter/answers) and "
            "hands back the apply URL for the candidate to complete manually."
        ),
    )

    def detect_application(self, job: Job) -> bool:
        return True  # deliberate catch-all fallback -- see registry ordering

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        return None

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        return MappingResult()

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        return DraftResult(mapping=mapping, preserved=False)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        return ValidationResult(
            ok=False, policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=[f"No automated form support for provider '{job.provider}' -- open the job URL manually."],
        )

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        return SubmitResult(success=False, error_type="SUBMISSION_INTERFACE_UNSUPPORTED",
                             error_message_safe=f"{job.provider}: submission not supported.")

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        return ConfirmationResult(confirmed=False)
