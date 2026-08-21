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


class ApplicationMode(str, Enum):
    ANALYZE = "ANALYZE"
    ASSIST = "ASSIST"
    AUTO = "AUTO"


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

    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
