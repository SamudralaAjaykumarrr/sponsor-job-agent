"""Typed vocabulary for the resume optimizer (CLAUDE.md Phase 14 sections
3, 7, 9, 46-49). Every enum here is a DISPLAY/DIAGNOSTIC label, never a
claim of a fixed universal scoring formula -- see docs/jd-analysis-model.md
and docs/resume-quality-diagnostics.md."""

from dataclasses import dataclass, field
from enum import Enum


class RequirementCategory(str, Enum):
    LANGUAGE = "LANGUAGE"
    FRAMEWORK = "FRAMEWORK"
    DATABASE = "DATABASE"
    CLOUD = "CLOUD"
    DEVOPS = "DEVOPS"
    MESSAGING = "MESSAGING"
    TESTING = "TESTING"
    SECURITY = "SECURITY"
    ARCHITECTURE = "ARCHITECTURE"
    FRONTEND = "FRONTEND"
    BACKEND = "BACKEND"
    DATA_ML = "DATA_ML"
    TOOL = "TOOL"
    METHODOLOGY = "METHODOLOGY"
    OBSERVABILITY = "OBSERVABILITY"
    RESPONSIBILITY = "RESPONSIBILITY"
    DOMAIN = "DOMAIN"
    EDUCATION = "EDUCATION"
    CERTIFICATION = "CERTIFICATION"
    YEARS_EXPERIENCE = "YEARS_EXPERIENCE"
    TITLE = "TITLE"
    OTHER = "OTHER"


# Categories treated as "skill-shaped" for required/preferred skill coverage
# counting (CLAUDE.md sections 10-11) -- distinct from responsibility/
# domain/education/certification/years, which get their own diagnostics.
SKILL_CATEGORIES = frozenset({
    RequirementCategory.LANGUAGE, RequirementCategory.FRAMEWORK, RequirementCategory.DATABASE,
    RequirementCategory.CLOUD, RequirementCategory.DEVOPS, RequirementCategory.MESSAGING,
    RequirementCategory.TESTING, RequirementCategory.SECURITY, RequirementCategory.ARCHITECTURE,
    RequirementCategory.FRONTEND, RequirementCategory.BACKEND, RequirementCategory.DATA_ML,
    RequirementCategory.TOOL, RequirementCategory.METHODOLOGY, RequirementCategory.OBSERVABILITY,
})

# CLAUDE.md section 8 "transferable experience safety": a category is only
# eligible for TRANSFERABLE framing when a genuinely analogous verified
# skill honestly supports a "similar work" claim without implying hands-on
# use of the missing item. Deliberately EXCLUDES LANGUAGE (claiming Python
# experience is "transferable" to a missing Go/Java requirement is a stretch
# that risks misleading), ARCHITECTURE (too abstract to responsibly claim
# analogy), SECURITY (domain-specific enough that analogy claims are
# risky), and OBSERVABILITY (post-release bug fix: deploying containers/IaC
# tools -- Docker/Kubernetes/Terraform -- is not a semantically defensible
# analogy for monitoring/observability experience the candidate doesn't
# actually have; a real Airbnb Payments JD caught this being mapped as
# TRANSFERABLE via shared DEVOPS-category membership before OBSERVABILITY
# was split out) -- a missing item in one of those categories is always
# MISSING rather than TRANSFERABLE (CLAUDE.md acceptance scenario B: "JD
# asks unsupported Go -> Go remains missing").
TRANSFERABLE_ELIGIBLE_CATEGORIES = SKILL_CATEGORIES - frozenset({
    RequirementCategory.LANGUAGE, RequirementCategory.ARCHITECTURE, RequirementCategory.SECURITY,
    RequirementCategory.OBSERVABILITY,
})


class RequirementPriority(str, Enum):
    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"


class EvidenceLevel(str, Enum):
    """CLAUDE.md Phase 14 section 7. Only DIRECT_VERIFIED and appropriately
    framed TRANSFERABLE_VERIFIED may back a resume claim; UNSUPPORTED is
    never inserted."""
    DIRECT_VERIFIED = "DIRECT_VERIFIED"
    TRANSFERABLE_VERIFIED = "TRANSFERABLE_VERIFIED"
    FAMILIAR_ONLY = "FAMILIAR_ONLY"
    UNSUPPORTED = "UNSUPPORTED"


class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    PARTIAL = "PARTIAL"
    TRANSFERABLE = "TRANSFERABLE"
    MISSING = "MISSING"
    UNSUPPORTED = "UNSUPPORTED"


class ResumeVariantStatus(str, Enum):
    NOT_GENERATED = "NOT_GENERATED"
    GENERATING = "GENERATING"
    READY = "READY"
    STALE = "STALE"
    CLAIM_CHECK_FAILED = "CLAIM_CHECK_FAILED"
    ATS_PARSE_FAILED = "ATS_PARSE_FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ATSParseStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class AlignmentLabel(str, Enum):
    """CLAUDE.md Phase 14 section 40: honest, non-numeric fallback label for
    a job whose major required items are missing -- never "optimized
    around" via fabrication."""
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    LOW_ALIGNMENT = "LOW_ALIGNMENT"


@dataclass
class JDRequirementItem:
    text: str
    normalized_value: str
    category: RequirementCategory
    priority: RequirementPriority
    evidence_span: str
    confidence: float = 1.0
    negated: bool = False
    conditional: bool = False
    # Post-release bug fix (real Airbnb Payments JD): "Proficient in at
    # least one major programming language (preferably Java/Kotlin/Python)"
    # is ONE requirement satisfiable by ANY one of its alternatives, never
    # three separate mandatory requirements. Empty for an ordinary
    # single-term requirement.
    alternatives: list[str] = field(default_factory=list)


@dataclass
class JDAnalysisResult:
    job_title: str = ""
    seniority: str = ""
    required_years: float | None = None
    domain_signals: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    education_requirements: list[str] = field(default_factory=list)
    certification_requirements: list[str] = field(default_factory=list)
    sponsorship_language_present: bool = False
    salary_mentioned: bool = False
    requirements: list[JDRequirementItem] = field(default_factory=list)
    analyzer_version: str = ""


@dataclass
class SkillEvidence:
    skill: str
    level: EvidenceLevel
    supporting_bullets: list[str] = field(default_factory=list)
    supporting_sources: list[str] = field(default_factory=list)  # "employer:Acme Corp" / "project:X"
    recency: str = ""


@dataclass
class EvidenceGraph:
    skills: dict[str, SkillEvidence] = field(default_factory=dict)
    domains: list[str] = field(default_factory=list)
    responsibility_evidence: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class RequirementMatch:
    requirement: JDRequirementItem
    status: MatchStatus
    evidence_ids: list[str] = field(default_factory=list)
    explanation: str = ""
