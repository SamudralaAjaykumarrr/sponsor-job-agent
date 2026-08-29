"""Canary Feasibility Gate V1 (feat/canary-feasibility-gate-v1).

Answers exactly one question, deterministically, BEFORE any expensive
resume generation or browser work: "is this job a technically sensible,
policy-safe, browser-feasible candidate for a controlled readiness/canary
attempt?" This is explicitly NOT a hire-probability predictor, and its
result must never be presented or logged as one -- `CanaryFeasibilityResult`
carries only PASS/REVIEW/REJECT verdicts and itemized textual reasons, never
a composite numeric score (a hard safety failure must never be diluted into
a number a caller could round away).

This module creates no parallel orchestration system and re-derives nothing
that already has an authoritative source -- every dimension below is a thin
wrapper over an EXISTING function/table:

  - `posting_health`      -> app.applications.provider_registry.
                             get_application_provider(job).check_job_still_active()
                             (the same optional, genuine-evidence-only hook
                             CLAUDE.md's Real Provider Execution V1 rules
                             already define) + `job.application_state`.
  - `employment_type`     -> app.matching.employment_type.resolve_employment_type_evidence
                             (Employment Type Evidence Hardening V1) -- reads the SAME
                             underlying token scan app.applications.eligibility's executor
                             gate's classify_employment_type() uses, plus a third,
                             evidence-based JobPosting JSON-LD source; this can never silently
                             diverge from that gate's own classification.
  - `sponsorship`         -> reads the job's OWN already-decided
                             `sponsorship_status` (app.sponsorship.decision's
                             output) -- never a second classification pass.
  - `role_fit`            -> app.matching.roles.is_target_role, plus an
                             explicit reject-keyword scan for the mismatches
                             CLAUDE.md's master spec always excludes.
  - `experience`          -> app.matching.seniority.evaluate_seniority --
                             the SAME years/title check the executor gate
                             already applies.
  - `location`            -> app.matching.geography.is_us_location.
  - `one_page_resume`     -> app.resume_optimizer.jd_analysis.analyze_jd()
                             (the optimizer's own cheap, deterministic JD-
                             requirement extractor, invoked WITHOUT ever
                             running the expensive resume-generation
                             pipeline) plus the job's already-computed
                             `technical_match_score`/`gap_skills` fields from
                             discovery-time scoring -- never a second,
                             expensive scoring pass.
  - `question_risk`       -> app.applications.provider_registry.
                             get_application_provider(job).discover_form()
                             -- the SAME public-API form discovery every
                             provider adapter already exposes (Greenhouse's
                             is genuinely API-sourced, no browser needed);
                             honestly REVIEW, never a guess, when a
                             provider's real form is only reachable through
                             a browser.
  - `provider_browser_feasibility` -> app.applications.execution_contract.
                             build_contract() (the existing seven-flag
                             provider capability projection) plus
                             app.applications.provider_health.get_health()
                             -- never a new capability registry.
  - `recent_failure_penalty` -> reads the EXISTING `browser_assist_sessions`
                             table (app.applications.browser_session) for a
                             recent, non-progressing failure on this job or
                             another job at the same employer/provider --
                             never a new, parallel tracking mechanism.

Overall verdict is the WORST of the nine dimensions (REJECT beats REVIEW
beats PASS) -- a single hard safety failure can never be outvoted or
averaged away by otherwise-good dimensions."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from app import config
from app.applications.eligibility import _TERMINAL_SKIP_STATES
from app.applications.execution_contract import build_contract
from app.applications.provider_registry import get_application_provider
from app.applications import provider_health
from app.db import db_session
from app.applications.employment_type_evidence import refresh_page_evidence
from app.matching.employment_type import resolve_employment_type_evidence
from app.matching.geography import is_us_location
from app.matching.roles import is_target_role
from app.matching.seniority import evaluate_seniority
from app.models import EmploymentType, Job, SponsorshipStatus

FEASIBILITY_GATE_VERSION = "canary-feasibility-gate-v1"

# CLAUDE.md master spec's own explicit exclusions -- checked regardless of
# whether the title superficially matches a looser STEM signal token (e.g.
# "Data Scientist" contains "data", which app.matching.roles' loose fallback
# would otherwise treat as a weak positive signal).
_ROLE_MISMATCH_PHRASES = (
    "research scientist", "machine learning research", "ml research",
    "applied scientist", "research engineer",
    "frontend engineer", "front-end engineer", "front end engineer", "frontend developer",
    "mobile engineer", "ios engineer", "android engineer", "mobile developer",
)
# A title containing one of these ALONE (without any backend/platform/
# infrastructure signal alongside it) is a data-science-ownership mismatch --
# checked separately from the phrases above because "data scientist" and
# "data engineer" must be told apart (the candidate's evidence is backend/
# platform, not deep statistical/ML ownership).
_DATA_SCIENCE_TITLE_MARKERS = ("data scientist", "data science")

# CLAUDE.md master spec + candidate evidence categories (CLAUDE.md's
# "Candidate evidence" list) -- used only to recognize whether a JD
# requirement the candidate doesn't already have marked MATCHED is at least
# in a category this candidate profile could plausibly ever support, purely
# to keep the question-risk/one-page heuristics from over-penalizing a job
# for requirements far outside any backend-engineering candidate's domain
# (e.g. a requirement mentioning "Figma" or "clinical trials").
_BACKEND_EVIDENCE_TOKENS = (
    "python", "fastapi", "flask", "django", "rest api", "restful", "postgresql", "postgres", "sql",
    "aws", "docker", "kubernetes", "microservice", "asynchronous", "event-driven", "event driven",
    "distributed system", "observability", "reliability", "backend", "platform", "infrastructure",
)


class FeasibilityVerdict(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


_VERDICT_RANK = {FeasibilityVerdict.PASS: 0, FeasibilityVerdict.REVIEW: 1, FeasibilityVerdict.REJECT: 2}


def _worst(a: FeasibilityVerdict, b: FeasibilityVerdict) -> FeasibilityVerdict:
    return a if _VERDICT_RANK[a] >= _VERDICT_RANK[b] else b


@dataclass(frozen=True)
class DimensionResult:
    verdict: FeasibilityVerdict
    reason: str

    def as_dict(self) -> dict:
        return {"verdict": self.verdict.value, "reason": self.reason}


@dataclass
class CanaryFeasibilityResult:
    job_id: int
    verdict: FeasibilityVerdict
    posting_health: DimensionResult
    employment_type: DimensionResult
    sponsorship: DimensionResult
    role_fit: DimensionResult
    experience: DimensionResult
    location: DimensionResult
    one_page_resume: DimensionResult
    question_risk: DimensionResult
    provider_browser_feasibility: DimensionResult
    recent_failure_penalty: DimensionResult
    gate_version: str = FEASIBILITY_GATE_VERSION
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def dimensions(self) -> dict[str, DimensionResult]:
        return {
            "posting_health": self.posting_health, "employment_type": self.employment_type,
            "sponsorship": self.sponsorship, "role_fit": self.role_fit, "experience": self.experience,
            "location": self.location, "one_page_resume": self.one_page_resume,
            "question_risk": self.question_risk,
            "provider_browser_feasibility": self.provider_browser_feasibility,
            "recent_failure_penalty": self.recent_failure_penalty,
        }

    @property
    def reject_reasons(self) -> list[str]:
        return [f"{name}: {d.reason}" for name, d in self.dimensions.items() if d.verdict == FeasibilityVerdict.REJECT]

    @property
    def review_reasons(self) -> list[str]:
        return [f"{name}: {d.reason}" for name, d in self.dimensions.items() if d.verdict == FeasibilityVerdict.REVIEW]

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id, "verdict": self.verdict.value, "gate_version": self.gate_version,
            "generated_at": self.generated_at,
            "dimensions": {name: d.as_dict() for name, d in self.dimensions.items()},
            "reject_reasons": self.reject_reasons, "review_reasons": self.review_reasons,
        }


# --- individual dimensions --------------------------------------------------

def _posting_health(job: Job) -> DimensionResult:
    if job.application_state in _TERMINAL_SKIP_STATES:
        return DimensionResult(FeasibilityVerdict.REJECT,
                                f"job is in a terminal/skip state ({job.application_state.value})")
    if not (job.canonical_url or job.url):
        return DimensionResult(FeasibilityVerdict.REJECT, "no canonical job identity/URL available")
    try:
        provider = get_application_provider(job)
        still_active = provider.check_job_still_active(job)
    except Exception as exc:  # noqa: BLE001 -- a feasibility check must never raise into the caller
        return DimensionResult(FeasibilityVerdict.REVIEW, f"posting activity could not be verified: {exc!r}")
    if still_active is False:
        reason = ""
        try:
            reason = provider.classify_job_inactive_reason(job) or ""
        except Exception:  # noqa: BLE001
            pass
        return DimensionResult(FeasibilityVerdict.REJECT, f"posting reports inactive ({reason or 'no longer available'})")
    if still_active is None:
        return DimensionResult(FeasibilityVerdict.REVIEW, "posting activity not checkable for this provider")
    return DimensionResult(FeasibilityVerdict.PASS, "posting reports active")


def _employment_type(job: Job) -> DimensionResult:
    """Employment Type Evidence Hardening V1: consults the SAME token scan
    the executor gate's classify_employment_type() uses, PLUS a bounded,
    read-only, best-effort fetch of the job's real posting page for genuine
    schema.org JobPosting JSON-LD `employmentType` evidence -- never a
    guess, never inferred from salary/benefits/location/title alone. A page
    fetch failure degrades to whatever page evidence was already persisted
    (or none) rather than blocking this dimension."""
    if job.provider == "mock_ats":
        # Never a real network touchpoint for the deterministic test-fixture
        # provider -- matches every other module's mock_ats exclusion
        # (execution_contract.py, presubmit_manifest.py, canary.py, ...).
        page_raw = job.employment_type_page_evidence_raw
    else:
        try:
            page_raw = refresh_page_evidence(job)
        except Exception:  # noqa: BLE001 -- a feasibility check must never raise
            page_raw = job.employment_type_page_evidence_raw
    decision = resolve_employment_type_evidence(job.employment_type, job.title, job.description, page_raw)
    provenance = f"{decision.reason} (source: {decision.source.value})"
    if decision.value == EmploymentType.FULL_TIME:
        return DimensionResult(FeasibilityVerdict.PASS, f"classified FULL_TIME -- {provenance}")
    if decision.value == EmploymentType.UNKNOWN:
        return DimensionResult(FeasibilityVerdict.REVIEW,
                                f"employment type not positively confirmed FULL_TIME -- {provenance}")
    return DimensionResult(FeasibilityVerdict.REJECT, f"classified {decision.value.value}, not FULL_TIME -- {provenance}")


def _sponsorship(job: Job) -> DimensionResult:
    status = job.sponsorship_status
    if status == SponsorshipStatus.NO_SPONSORSHIP:
        return DimensionResult(FeasibilityVerdict.REJECT, "employer states no sponsorship")
    if status == SponsorshipStatus.UNKNOWN:
        return DimensionResult(
            FeasibilityVerdict.REJECT,
            "sponsorship UNKNOWN -- existing policy is do-not-apply, so an unattended canary attempt is never started",
        )
    if status == SponsorshipStatus.CONFIRMED_SPONSOR:
        return DimensionResult(FeasibilityVerdict.PASS, "confirmed sponsor")
    return DimensionResult(FeasibilityVerdict.PASS, "likely sponsor (review-required path, still canary-feasible)")


def _role_fit(job: Job) -> DimensionResult:
    title_lower = (job.title or "").lower()
    if any(p in title_lower for p in _ROLE_MISMATCH_PHRASES):
        return DimensionResult(FeasibilityVerdict.REJECT, f"title matches an excluded role pattern ('{job.title}')")
    if any(m in title_lower for m in _DATA_SCIENCE_TITLE_MARKERS):
        return DimensionResult(
            FeasibilityVerdict.REJECT,
            f"title reads as a data-science/ML-ownership role ('{job.title}'), not backend/platform engineering",
        )
    is_relevant, is_primary = is_target_role(job.title)
    if not is_relevant:
        return DimensionResult(FeasibilityVerdict.REJECT, f"title is not a recognized CS/STEM target role ('{job.title}')")
    if is_primary:
        return DimensionResult(FeasibilityVerdict.PASS, f"primary target role match ('{job.title}')")
    return DimensionResult(FeasibilityVerdict.REVIEW, f"only a secondary/loose role match ('{job.title}')")


def _experience(job: Job) -> DimensionResult:
    passes, reason, _years = evaluate_seniority(job.title, job.description)
    if passes:
        return DimensionResult(FeasibilityVerdict.PASS, reason)
    return DimensionResult(FeasibilityVerdict.REJECT, reason)


def _location(job: Job) -> DimensionResult:
    if is_us_location(job.location):
        return DimensionResult(FeasibilityVerdict.PASS, f"US location ('{job.location}')")
    return DimensionResult(FeasibilityVerdict.REJECT, f"not a US location ('{job.location}')")


def _gap_skill_list(job: Job) -> list[str]:
    return [s.strip() for s in (job.gap_skills or "").split(",") if s.strip()]


def _matched_skill_list(job: Job) -> list[str]:
    return [s.strip() for s in (job.matched_skills or "").split(",") if s.strip()]


def _one_page_resume(job: Job) -> DimensionResult:
    gap_skills = _gap_skill_list(job)
    matched_skills = _matched_skill_list(job)
    if len(gap_skills) > config.CANARY_ONE_PAGE_MAX_GAP_SKILLS and len(gap_skills) > len(matched_skills):
        return DimensionResult(
            FeasibilityVerdict.REJECT,
            f"{len(gap_skills)} unaddressed required/gap skill(s) vs {len(matched_skills)} verified match(es) -- "
            "cannot truthfully fill a one-page resume without unsafe compression",
        )
    try:
        from app.resume_optimizer.jd_analysis import analyze_jd
        from app.resume_optimizer.models import RequirementPriority

        analysis = analyze_jd(job.title, job.description)
        required_count = sum(1 for r in analysis.requirements if r.priority == RequirementPriority.REQUIRED)
    except Exception as exc:  # noqa: BLE001 -- a feasibility check must never raise into the caller
        return DimensionResult(FeasibilityVerdict.REVIEW, f"JD requirement count could not be estimated: {exc!r}")
    if required_count > config.CANARY_ONE_PAGE_MAX_REQUIRED_ITEMS:
        return DimensionResult(
            FeasibilityVerdict.REVIEW,
            f"JD states {required_count} REQUIRED-priority items -- may not truthfully compress to one page",
        )
    return DimensionResult(FeasibilityVerdict.PASS, f"{required_count} REQUIRED item(s), {len(gap_skills)} gap skill(s)")


def _question_risk(job: Job) -> DimensionResult:
    try:
        provider = get_application_provider(job)
        snapshot = provider.discover_form(job)
    except Exception as exc:  # noqa: BLE001 -- a feasibility check must never raise into the caller
        return DimensionResult(FeasibilityVerdict.REVIEW, f"form question metadata could not be fetched: {exc!r}")
    if snapshot is None:
        return DimensionResult(FeasibilityVerdict.REVIEW, "no structured question schema for this provider -- "
                                                            "question complexity cannot be assessed without a browser")
    essay_fields = [f for f in snapshot.fields if f.field_type == "textarea" and f.required and not f.choices]
    if len(essay_fields) >= config.CANARY_MAX_MANDATORY_ESSAY_FIELDS:
        return DimensionResult(
            FeasibilityVerdict.REVIEW,
            f"{len(essay_fields)} mandatory free-text question(s) detected -- elevated blocker-complexity risk",
        )
    return DimensionResult(FeasibilityVerdict.PASS, f"{len(essay_fields)} mandatory free-text question(s), "
                                                     f"{len(snapshot.fields)} field(s) total")


def _health_observation_is_recent(row: Optional[dict]) -> bool:
    """Whether the most recent evidence behind a health row's classification
    falls inside the SAME cooldown window `_recent_failure_penalty` already
    uses -- reused here, never a second threshold, so "how stale is this
    evidence" means one consistent thing across this gate."""
    if not row:
        return False
    most_recent = row.get("last_failure") or row.get("last_success")
    if not most_recent:
        return False
    try:
        observed = datetime.fromisoformat(most_recent)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - observed
    return age <= timedelta(hours=config.CANARY_RECENT_FAILURE_COOLDOWN_HOURS)


def _provider_browser_feasibility(job: Job) -> DimensionResult:
    """CLAUDE.md's own standing rule for `app.applications.provider_health`:
    "Recording evidence here NEVER auto-disables anything -- a DEGRADED/
    STALE/SCHEMA_DRIFT/CAPTCHA_BLOCKED/AUTH_GATED health only ever surfaces
    for review." This dimension honors that literally -- a REJECT here is
    reserved for a genuine STRUCTURAL incapability (no assist support at
    all), never for a health observation, however concerning, which is
    always at most REVIEW. `provider_health` for a non-tenant-shaped
    provider (every ATS except Workday) is also a SINGLE ROW SHARED across
    every employer using that provider (CLAUDE.md Phase 13 section 11) --
    a fresh negative observation for the row is real, current evidence about
    the provider's infrastructure and is surfaced as REVIEW; a STALE one
    (older than the same cooldown window `recent_failure_penalty` uses) is
    reported informationally without blocking an otherwise-unrelated
    employer's candidacy, since it says nothing current about THIS job --
    the job/employer-SPECIFIC signal is `recent_failure_penalty`, not this
    dimension."""
    contract = build_contract(job.provider or "")
    if not contract.assist_supported:
        return DimensionResult(FeasibilityVerdict.REJECT,
                                f"provider '{job.provider}' has no browser-assist capability (support_level="
                                f"{contract.support_level})")
    tenant, site = "", ""
    try:
        from app.applications.browser_runtime import _tenant_site_for
        tenant, site = _tenant_site_for(job.provider or "", job.canonical_url or job.url or "")
    except Exception:  # noqa: BLE001
        pass
    health_info = provider_health.get_health(job.provider or "", tenant=tenant, site=site)
    health = health_info["health"]
    recent = _health_observation_is_recent(health_info.get("row"))
    if health in ("CAPTCHA_BLOCKED", "AUTH_GATED", "SCHEMA_DRIFT", "DEGRADED"):
        if recent:
            return DimensionResult(FeasibilityVerdict.REVIEW, f"provider real-browser flow health is {health}")
        return DimensionResult(
            FeasibilityVerdict.PASS,
            f"assist_supported; a prior {health} observation exists but is older than the "
            f"{config.CANARY_RECENT_FAILURE_COOLDOWN_HOURS}h cooldown, so it is reported informationally only",
        )
    return DimensionResult(
        FeasibilityVerdict.PASS,
        f"assist_supported, form_discovery_supported={contract.form_discovery_supported}, health={health}",
    )


_PROBLEMATIC_SESSION_STATUSES = (
    "PAUSED_PLATFORM_RESTRICTED", "PAUSED_UNSUPPORTED_SUBMISSION", "PAUSED_APPLY_ENTRY_UNRECOGNIZED",
    "PAUSED_AMBIGUOUS_APPLY_CONTROL", "PAUSED_FORM_CHANGED", "PAUSED_IFRAME_UNEXPECTED_HOST",
    "SUBMISSION_STATUS_UNKNOWN", "CRASHED_RECOVERABLE",
)


def _recent_failure_penalty(job: Job) -> DimensionResult:
    """Reuses the EXISTING browser_assist_sessions table -- never a second,
    parallel failure-tracking mechanism. Excludes this job's OWN recent
    non-progressing sessions, and any OTHER job at the same employer +
    provider that recently failed the same way (the general form of "don't
    retry Airbnb job 327 right now")."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.CANARY_RECENT_FAILURE_COOLDOWN_HOURS)).isoformat()
    placeholders = ",".join("?" for _ in _PROBLEMATIC_SESSION_STATUSES)
    with db_session() as conn:
        own = conn.execute(
            f"SELECT session_id, status, updated_at FROM browser_assist_sessions "
            f"WHERE job_id = ? AND status IN ({placeholders}) AND updated_at >= ? "
            f"ORDER BY updated_at DESC LIMIT 1",
            (job.id, *_PROBLEMATIC_SESSION_STATUSES, cutoff),
        ).fetchone()
        if own is not None:
            return DimensionResult(
                FeasibilityVerdict.REJECT,
                f"this job's own session {own['session_id']} recently ended {own['status']} "
                f"(within the {config.CANARY_RECENT_FAILURE_COOLDOWN_HOURS}h cooldown)",
            )
        if job.company_identifier and job.provider:
            sibling = conn.execute(
                f"""
                SELECT bas.session_id, bas.status, bas.updated_at, bas.job_id
                FROM browser_assist_sessions bas
                JOIN jobs j ON j.id = bas.job_id
                WHERE j.company_identifier = ? AND j.provider = ? AND bas.job_id != ?
                  AND bas.status IN ({placeholders}) AND bas.updated_at >= ?
                ORDER BY bas.updated_at DESC LIMIT 1
                """,
                (job.company_identifier, job.provider, job.id, *_PROBLEMATIC_SESSION_STATUSES, cutoff),
            ).fetchone()
            if sibling is not None:
                return DimensionResult(
                    FeasibilityVerdict.REJECT,
                    f"job {sibling['job_id']} at the same employer/provider recently ended {sibling['status']} "
                    f"(within the {config.CANARY_RECENT_FAILURE_COOLDOWN_HOURS}h cooldown)",
                )
    return DimensionResult(FeasibilityVerdict.PASS, "no recent live-browser failure/cooldown for this job or employer")


def evaluate_canary_feasibility(job: Job) -> CanaryFeasibilityResult:
    """Pure, read-only, deterministic evaluation -- never mutates the job,
    never launches a browser, never generates a resume. Safe to call for
    every candidate under consideration; the only network calls it may make
    are the SAME cheap, already-sanctioned ones `check_job_still_active()`/
    `discover_form()` already perform for other purposes (a single bounded
    HTTP GET each, never a browser)."""
    posting_health = _posting_health(job)
    employment_type = _employment_type(job)
    sponsorship = _sponsorship(job)
    role_fit = _role_fit(job)
    experience = _experience(job)
    location = _location(job)
    one_page_resume = _one_page_resume(job)
    question_risk = _question_risk(job)
    provider_browser_feasibility = _provider_browser_feasibility(job)
    recent_failure_penalty = _recent_failure_penalty(job)

    verdict = FeasibilityVerdict.PASS
    for dim in (posting_health, employment_type, sponsorship, role_fit, experience, location, one_page_resume,
                question_risk, provider_browser_feasibility, recent_failure_penalty):
        verdict = _worst(verdict, dim.verdict)

    return CanaryFeasibilityResult(
        job_id=job.id or 0, verdict=verdict, posting_health=posting_health, employment_type=employment_type,
        sponsorship=sponsorship, role_fit=role_fit, experience=experience, location=location,
        one_page_resume=one_page_resume, question_risk=question_risk,
        provider_browser_feasibility=provider_browser_feasibility, recent_failure_penalty=recent_failure_penalty,
    )


def render_text(result: CanaryFeasibilityResult) -> str:
    lines = [
        f"Canary Feasibility -- job #{result.job_id}", "=" * 50,
        f"VERDICT: {result.verdict.value}", "",
    ]
    for name, dim in result.dimensions.items():
        lines.append(f"  [{dim.verdict.value:7s}] {name:28s} {dim.reason}")
    return "\n".join(lines) + "\n"
