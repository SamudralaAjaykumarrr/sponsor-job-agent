"""ApplicationProvider interface (CLAUDE.md Phase 8 section 6). Deliberately
separate from app.providers.base.JobProvider (discovery) -- an
ApplicationProvider answers "can I fill out / submit this posting's
application form", never "can I find postings", and the two must never be
conflated or share a base class, since a provider can be FULL for discovery
and UNSUPPORTED for application (e.g. Lever)."""

from abc import ABC, abstractmethod
from typing import Optional

from app.applications.models import (
    ApplicationCapabilities,
    ApplicationField,
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
from app.models import Job


class ApplicationProvider(ABC):
    """One connector per ATS's APPLICATION flow (not discovery). Every
    concrete provider MUST set `capabilities` truthfully -- see
    docs/application-provider-interface.md. `submission_supported=True` is
    only ever set on a provider that has been genuinely tested end-to-end
    (currently: only the deterministic MockATSProvider)."""

    name: str = "base"
    capabilities: ApplicationCapabilities = ApplicationCapabilities(
        provider="base", provider_version="0.0.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.UNSUPPORTED, support_level=SupportLevel.UNSUPPORTED,
        notes="Abstract base -- not a real provider.",
    )

    @classmethod
    def get_capabilities(cls) -> ApplicationCapabilities:
        return cls.capabilities

    @abstractmethod
    def detect_application(self, job: Job) -> bool:
        """True if this provider can even attempt to handle job's ATS."""

    @abstractmethod
    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        """Returns a FormSnapshot, or None if discovery isn't supported/failed
        for this specific posting. Never fabricates fields that weren't
        actually observed."""

    @abstractmethod
    def map_fields(self, form: FormSnapshot, application_fields: list[ApplicationField]) -> MappingResult:
        """Deterministic mapping via app.applications.mapping.match_field --
        every concrete provider should just delegate to the shared engine
        rather than reimplementing matching."""

    @abstractmethod
    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        """Builds the in-memory filled draft. Never performs a network
        submission."""

    @abstractmethod
    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        """Determines the AutomationPolicy for this specific draft -- CAPTCHA/
        MFA/unresolved required fields/legal-unknowns all resolve here into a
        PolicyReason. Never bypasses a detected restriction."""

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        """Only ever called by the executor when validate() returned
        AutomationPolicy.PERMITTED_AUTO. The base implementation refuses --
        a real provider must override this explicitly and truthfully set
        capabilities.submission_supported=True to be reachable at all (the
        executor itself also refuses to call submit() unless that capability
        is set, so this is defense in depth, not the only guard)."""
        return SubmitResult(
            success=False, error_type=PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED.value,
            error_message_safe=f"{self.name}: submission not implemented/permitted.",
        )

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        """Base: no confirmation without a real success payload. A provider
        with submission_supported=True must override to check for genuine
        evidence (success page, confirmation id, receipt) -- never marks
        confirmed merely because submit() returned success=True without
        independent evidence, per CLAUDE.md Phase 8 section 35."""
        if not submit_result.success or not submit_result.confirmation_id:
            return ConfirmationResult(confirmed=False)
        return ConfirmationResult(
            confirmed=True, confirmation_id=submit_result.confirmation_id,
            confirmation_url=submit_result.confirmation_url,
        )
