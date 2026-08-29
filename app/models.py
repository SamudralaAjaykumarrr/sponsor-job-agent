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
    ASSESSMENT = "ASSESSMENT"                  # premium-ui tracker: manual-only, see app.applications.tracker
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"                            # premium-ui tracker: manual-only, see app.applications.tracker
    REJECTED = "REJECTED"

    # --- Phase 8 (CLAUDE.md Phase 8 section 4): coarse, dashboard-facing
    # execution states. These mirror (never replace) the fine-grained,
    # versioned app.applications.models.ExecutionStatus machine recorded per
    # application_executions row -- see docs/phase8-application-executor.md
    # "Two-layer state model". Every existing state above keeps its exact
    # prior meaning; nothing here is repurposed.
    EXECUTION_QUEUED = "EXECUTION_QUEUED"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"

    # --- Approval-gated-autonomy-v1: mirrors
    # app.applications.models.ExecutionStatus.APPROVED -- the job has an
    # explicit, durable APPROVE & APPLY record (app.applications.approval)
    # but the provider has no verified final-submission capability, so it
    # rests here awaiting browser-assist/manual completion. Never set
    # without a real application_approvals row backing it.
    APPROVED = "APPROVED"

    SUBMITTING = "SUBMITTING"
    SUBMISSION_STATUS_UNKNOWN = "SUBMISSION_STATUS_UNKNOWN"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    DUPLICATE_APPLICATION_BLOCKED = "DUPLICATE_APPLICATION_BLOCKED"
    WITHDRAWN = "WITHDRAWN"

    # --- Phase 9 (CLAUDE.md Phase 9 sections 24-27): a job that was found to
    # be no longer active, or whose JD changed materially, immediately before
    # submission -- distinct from SUBMISSION_FAILED (that means a submission
    # attempt was made and failed; this means submission was correctly never
    # attempted at all).
    JOB_NO_LONGER_ACTIVE = "JOB_NO_LONGER_ACTIVE"

    # Tsenta-parity-closure-v1: mirrors
    # app.applications.models.ExecutionStatus.USER_COMPLETED_EXTERNALLY --
    # the candidate told us, after a READY FOR FINAL REVIEW hand-off, that
    # they finished this application themselves. Distinct from APPLIED
    # (which always implies genuine confirmation evidence): this is an
    # honest, self-reported, unverified completion. Never set without an
    # explicit app.applications.handoff.record_manual_outcome() call.
    COMPLETED_BY_USER = "COMPLETED_BY_USER"


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

    # Employment Type Evidence Hardening V1: the RAW schema.org JobPosting
    # JSON-LD `employmentType` value found on this job's real public posting
    # page, the last time it was checked (see
    # app.applications.employment_type_evidence) -- never a cached decision.
    # app.matching.employment_type.resolve_employment_type_evidence()
    # recomputes the actual FULL_TIME/.../UNKNOWN decision live from this
    # plus `employment_type`/`title`/`description` every time it's called.
    employment_type_page_evidence_raw: str = ""
    employment_type_page_evidence_checked_at: str = ""

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

    # CLAUDE.md Phase 13 sections 43-45: which JD fingerprint the currently
    # generated resume artifact was built against -- reuses the existing
    # jd_sponsorship_fingerprint value rather than a second, parallel
    # fingerprinting scheme. Empty until a resume has actually been
    # generated for this job.
    resume_jd_fingerprint: str = ""

    # One-click agent (app.agent.orchestrator._run_resume_stage): which
    # resume_optimizer variant (if any) was promoted to be the resume
    # artifact actually used for this job's application -- distinct from
    # resume_variants.current (the optimizer's own latest variant,
    # independent of whether it was ever promoted).
    promoted_resume_variant_id: str = ""

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

    # CLAUDE.md production-v2 "CURRENT REAL DASHBOARD DEFECTS" item 6: a
    # synthetic/test-only row (TEST MODE's mock_ats fixture; any future
    # deliberately-seeded demo/benchmark job) is marked explicitly at ingest
    # time so the real-mode dashboard/summary/needs-action queries can
    # exclude it by default without guessing from provider-name string
    # matching scattered across call sites -- see app.pipeline_dashboard.
    is_test_fixture: bool = False

    # Phase 6 (CLAUDE.md section 36): correlation id propagated from the
    # poll_attempts row that discovered this job (its attempt_id already
    # uniquely ties one worker's one attempt at one portal together, so it
    # doubles as the correlation id -- no separate id scheme needed). Empty
    # for jobs ingested outside the worker fleet (e.g. manual JD paste).
    correlation_id: str = ""

    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
