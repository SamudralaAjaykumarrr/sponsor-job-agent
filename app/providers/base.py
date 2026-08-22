from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.providers.capabilities import ProviderCapabilities, SupportLevel


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RawJobPosting:
    """Normalized shape every provider must produce, before dedup/analysis.

    Only `provider`/`external_job_id`/`title`/`company`/`location`/`description`/`url`
    are guaranteed. Every Phase 3 addition below is Optional/defaulted -- a
    provider that genuinely does not expose a field MUST leave it None/""
    rather than fabricate a value."""

    provider: str
    external_job_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    employment_type_raw: str = ""
    published_at: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None

    # Phase 3 normalized-model fields (see docs/provider-development.md).
    company_identifier: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    remote_status: Optional[str] = None  # "remote" | "hybrid" | "onsite" | None
    source_url: Optional[str] = None  # canonical/original discovery URL, if distinct from apply url
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None  # "year" | "hour" | etc, provider-reported only
    department: Optional[str] = None
    team: Optional[str] = None
    office: Optional[str] = None
    provider_metadata: dict = field(default_factory=dict)

    @property
    def apply_url(self) -> str:
        return self.url

    @property
    def provider_job_id(self) -> str:
        return self.external_job_id


class JobProvider(ABC):
    """Discovery connector for a public, unauthenticated job-board API.
    Implementations MUST isolate per-board/per-company fetch errors internally
    (log + skip) so one bad source never aborts a whole discovery cycle.

    Every concrete provider MUST set `capabilities` (a ProviderCapabilities
    instance) describing what it actually supports -- see
    docs/provider-capabilities.md. Never claim FULL/discovery_supported=True
    without a working, tested implementation."""

    name: str = "base"
    # Phase 6 (CLAUDE.md sections 12-14): the last exception a subclass's own
    # internal per-tenant fetch helper swallowed (logged + returned [] for),
    # if any -- class-level default so subclasses never need to touch their
    # __init__ to support this. fetch_jobs_result() below reads it to tell
    # "genuinely empty board" apart from "the fetch actually failed" without
    # changing fetch_jobs()'s existing swallow-and-return-[] behavior at all.
    _last_error: Optional[BaseException] = None
    capabilities: ProviderCapabilities = ProviderCapabilities(
        provider_name="base",
        provider_version="0.0.0",
        discovery_supported=False,
        detail_fetch_supported=False,
        structured_location_supported=False,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=False,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.UNSUPPORTED,
        notes="Abstract base -- not a real provider.",
    )

    @abstractmethod
    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        raise NotImplementedError

    @classmethod
    def get_capabilities(cls) -> ProviderCapabilities:
        return cls.capabilities

    def fetch_jobs_result(self, max_jobs: int, *, tenant: str = "") -> "ProviderFetchResult":
        """CLAUDE.md Phase 6 sections 12-13: structured counterpart to
        fetch_jobs() that lets a caller distinguish SUCCESS_WITH_JOBS /
        SUCCESS_EMPTY / a specific typed failure, instead of an empty list
        meaning either "nothing to report" or "the fetch actually broke".
        fetch_jobs() itself is never modified by this -- same behavior,
        same tests, unconditionally. UNSUPPORTED providers (support_level
        never implemented discovery) short-circuit without even attempting
        a request, matching their existing fetch_jobs()==[] contract."""
        from app.providers.errors import ProviderFetchResult, ProviderFetchStatus, build_result, utcnow

        started_at = utcnow()
        if not self.capabilities.discovery_supported:
            finished_at = utcnow()
            return ProviderFetchResult(
                status=ProviderFetchStatus.UNSUPPORTED, jobs=[], provider=self.name, tenant=tenant,
                started_at=started_at, finished_at=finished_at, latency_ms=0.0,
                error_type=ProviderFetchStatus.UNSUPPORTED.value,
                error_message_safe=f"{self.name}: discovery not implemented ({self.capabilities.notes})",
                retryable=False,
            )

        self._last_error = None
        jobs = self.fetch_jobs(max_jobs)
        error = self._last_error
        self._last_error = None
        return build_result(provider=self.name, tenant=tenant, jobs=jobs, started_at=started_at, error=error)
