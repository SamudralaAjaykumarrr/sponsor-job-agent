from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkArrangement(str, Enum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    UNKNOWN = "UNKNOWN"


class SponsorshipStatus(str, Enum):
    CONFIRMED_SPONSOR = "CONFIRMED_SPONSOR"
    LIKELY_SPONSOR = "LIKELY_SPONSOR"
    UNKNOWN = "UNKNOWN"
    NO_SPONSORSHIP = "NO_SPONSORSHIP"


class FreshnessTier(str, Enum):
    MAXIMUM = "MAXIMUM"       # 0-60 min
    VERY_HIGH = "VERY_HIGH"   # 1-3 hr
    HIGH = "HIGH"             # 3-12 hr
    MODERATE = "MODERATE"     # 12-24 hr
    LOWER = "LOWER"           # older / unknown


class FreshnessSource(str, Enum):
    PUBLISHED_AT = "PUBLISHED_AT"
    FIRST_SEEN = "FIRST_SEEN"


class ApplicationState(str, Enum):
    NEW = "NEW"
    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    SKIPPED = "SKIPPED"                        # legacy/generic + manual "Skip" button
    SKIPPED_NO_SPONSORSHIP = "SKIPPED_NO_SPONSORSHIP"
    SKIPPED_SENIORITY = "SKIPPED_SENIORITY"
    SKIPPED_COMPENSATION = "SKIPPED_COMPENSATION"
    SKIPPED_POOR_MATCH = "SKIPPED_POOR_MATCH"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CLAIM_VALIDATION_FAILED = "CLAIM_VALIDATION_FAILED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLIED = "APPLIED"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"

    # --- Phase 8 (CLAUDE.md Phase 8 section 4): coarse, dashboard-facing
    # execution states. These mirror (never replace) the fine-grained,
    # versioned app.applications.models.ExecutionStatus machine recorded per
    # application_executions row -- see docs/phase8-application-executor.md
    # "Two-layer state model". Every existing state above keeps its exact
    # prior meaning; nothing here is repurposed.
    EXECUTION_QUEUED = "EXECUTION_QUEUED"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    SUBMITTING = "SUBMITTING"
    SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    DUPLICATE_APPLICATION_BLOCKED = "DUPLICATE_APPLICATION_BLOCKED"
    WITHDRAWN = "WITHDRAWN"


class ApplicationMode(str, Enum):
    ANALYZE = "ANALYZE"
    ASSIST = "ASSIST"
    AUTO = "AUTO"


class EmploymentType(str, Enum):
    """Positive employment-type classification (CLAUDE.md Phase 8 section 1)
    -- distinct from the older, deliberately permissive
    app.matching.employment_type.is_full_time() boolean (kept unchanged, still
    used by the discovery-time filter in app.agent.cycle). The executor's
    hard gate requires FULL_TIME to be POSITIVELY established; UNKNOWN is
    never treated as FULL_TIME for submission purposes."""
    FULL_TIME = "FULL_TIME"
    CONTRACT = "CONTRACT"
    C2C = "C2C"
    PART_TIME = "PART_TIME"
    INTERNSHIP = "INTERNSHIP"
    TEMPORARY = "TEMPORARY"
    SEASONAL = "SEASONAL"
    FREELANCE = "FREELANCE"
    UNKNOWN = "UNKNOWN"


class PriorityTier(str, Enum):
    P1_REMOTE_CONFIRMED = "P1_REMOTE_CONFIRMED"
    P2_REMOTE_LIKELY = "P2_REMOTE_LIKELY"
    P3_HYBRID_CONFIRMED = "P3_HYBRID_CONFIRMED"
    P4_HYBRID_LIKELY = "P4_HYBRID_LIKELY"
    P5_ONSITE_CONFIRMED = "P5_ONSITE_CONFIRMED"
    P6_ONSITE_LIKELY = "P6_ONSITE_LIKELY"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class JobIngestRequest(BaseModel):
    title: str
    company: str
    location: str = ""
    description: str
    url: str = ""
    source: str = "manual"
    published_at: Optional[str] = None
    mode: ApplicationMode = ApplicationMode.ASSIST


class Job(BaseModel):
    id: Optional[int] = None
    title: str
    company: str
    location: str = ""
    description: str
    url: str = ""
    source: str = "manual"

    provider: str = "manual"
    external_job_id: str = ""
    employment_type: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    dedup_fingerprint: str = ""

    # Phase 3 normalized-model fields, all optional -- None/"" when a provider
    # doesn't expose the field. Never fabricated.
    company_identifier: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    remote_status: str = ""
    department: str = ""
    team: str = ""
    office: str = ""
    source_url: str = ""
    canonical_url: str = ""
    salary_currency: str = ""
    salary_period: str = ""
    provider_metadata: str = "{}"  # JSON-encoded, provider-specific extras

    published_at: Optional[str] = None
    first_seen_at: str = Field(default_factory=utcnow)
    last_seen_at: str = Field(default_factory=utcnow)
    freshness_source: FreshnessSource = FreshnessSource.FIRST_SEEN

    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    sponsorship_status: SponsorshipStatus = SponsorshipStatus.UNKNOWN
    sponsorship_evidence: str = ""

    freshness_tier: FreshnessTier = FreshnessTier.LOWER
    freshness_minutes: Optional[float] = None

    # Phase 7 (CLAUDE.md sections 21/24): sponsorship decision audit linkage.
    # sponsorship_status/sponsorship_evidence above are already the CURRENT
    # decision's summary; these add the versioning/JD-change-detection/
    # conflict-flag fields the dashboard and JSON API expose.
    sponsorship_decision_version: int = 0
    jd_sponsorship_fingerprint: str = ""
    sponsorship_conflict: bool = False
    sponsorship_blocking_reason: str = ""

    technical_match_score: float = 0.0
    matched_skills: str = ""   # comma-separated
    gap_skills: str = ""       # comma-separated
    score_breakdown: str = "{}"  # JSON-encoded machine-readable reasons

    priority_tier: PriorityTier = PriorityTier.NOT_ELIGIBLE
    priority_score: float = 0.0

    application_state: ApplicationState = ApplicationState.NEW
    mode: ApplicationMode = ApplicationMode.ASSIST

    resume_docx_path: Optional[str] = None
    resume_pdf_path: Optional[str] = None
    resume_txt_path: Optional[str] = None
    job_analysis_path: Optional[str] = None
    application_answers_path: Optional[str] = None
    cover_letter_path: Optional[str] = None

    notes: str = ""

    # Phase 6 (CLAUDE.md section 36): correlation id propagated from the
    # poll_attempts row that discovered this job (its attempt_id already
    # uniquely ties one worker's one attempt at one portal together, so it
    # doubles as the correlation id -- no separate id scheme needed). Empty
    # for jobs ingested outside the worker fleet (e.g. manual JD paste).
    correlation_id: str = ""

    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
