"""Role-aware selection (JD intelligence v3): classifies which role
archetype a JD is shaped like (backend / payments / platform /
infrastructure / full-stack / QA-SDET / cloud / AI-backend / general) and
builds a truthful TARGET ROLE / resume-headline string.

CLAUDE.md correction (post-review): a target-role/headline line is NOT a
claim of prior employment -- it's a forward-looking "what I'm applying for"
line, the same way a resume's stated career objective always has been. It is
therefore a SEPARATE, INDEPENDENT truthfulness contract from verified
employment titles (`CandidateProfile.employment[*].title`, which
`app.resume.claim_checker` validates completely unchanged) -- this module
never reads, blends with, or rewrites an employment title, and
`app.resume.claim_checker` never requires the target role to equal one.

`build_target_role` composes the headline from three independently-gated
parts, each of which can ONLY ever surface verified/JD-truthful content:
  - a fixed, hardcoded ROLE FAMILY name per archetype (never a raw JD
    phrase, so it can never carry unverified technology or inflated
    seniority -- see `_ARCHETYPE_ROLE_FAMILY`; deliberately contains no
    seniority word at all, so there is nothing for an unsupported
    "Staff"/"Senior" claim to leak through in the first place)
  - an OPTIONAL single-language TECHNOLOGY QUALIFIER, only when that exact
    language is BOTH named in the JD title AND `EvidenceLevel
    .DIRECT_VERIFIED` in the candidate's evidence graph (e.g. "Python
    Backend Engineer" for a JD titled ".. Python .. Engineer" with verified
    Python -- never "Java Backend Engineer" for an unverified-Java JD)
  - an OPTIONAL DOMAIN QUALIFIER, only when the JD's own extracted
    `domain_signals` and the candidate's own evidence-graph `domains` both
    contain it (e.g. "Software Engineer, Payments")

`app.resume.claim_checker.check_resume_claims` independently re-validates
every one of these three parts against the verified profile (defense in
depth, matching this codebase's existing pattern of never trusting a single
enforcement point) -- see that module's `_validate_target_role`."""

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from app.resume_optimizer.jd_analysis import DOMAIN_SIGNALS, SKILL_VOCAB
from app.resume_optimizer.models import EvidenceGraph, EvidenceLevel, JDAnalysisResult, RequirementCategory

_SKILL_CATEGORY: dict[str, RequirementCategory] = {phrase: category for phrase, category in SKILL_VOCAB}


class RoleArchetype(str, Enum):
    BACKEND = "BACKEND"
    PAYMENTS = "PAYMENTS"
    PLATFORM = "PLATFORM"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    FULL_STACK = "FULL_STACK"
    QA_SDET = "QA_SDET"
    CLOUD = "CLOUD"
    AI_BACKEND = "AI_BACKEND"
    GENERAL = "GENERAL"


@dataclass
class RoleClassification:
    archetype: RoleArchetype
    # Multiplier applied to relevance weight for terms in this category
    # (app.resume_optimizer.relevance) -- 1.0 = no change. Never used to
    # insert new content, only to reweight selection among verified content
    # already produced by app.resume_optimizer.matching.
    category_boosts: dict[RequirementCategory, float] = field(default_factory=dict)
    domain_boost_terms: set[str] = field(default_factory=set)
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "archetype": self.archetype.value,
            "category_boosts": {k.value: v for k, v in self.category_boosts.items()},
            "domain_boost_terms": sorted(self.domain_boost_terms),
            "detail": self.detail,
        }


_PAYMENTS_DOMAINS = {"payments", "fintech", "banking", "financial services", "fraud"}


def classify_role(job_title: str, jd_analysis: JDAnalysisResult, graph: EvidenceGraph) -> RoleClassification:
    """Pure function over JD analysis + candidate evidence graph -- never
    re-reads raw JD/profile text itself, so it stays trivially consistent
    with whatever app.resume_optimizer.jd_analysis/evidence already decided."""
    title_lower = (job_title or "").lower()
    domains = set(jd_analysis.domain_signals)
    cat_counts = Counter(r.category for r in jd_analysis.requirements)

    payments_hit = domains & _PAYMENTS_DOMAINS
    if payments_hit or "payment" in title_lower:
        return RoleClassification(
            RoleArchetype.PAYMENTS,
            category_boosts={RequirementCategory.BACKEND: 1.3, RequirementCategory.DATABASE: 1.2, RequirementCategory.SECURITY: 1.2},
            domain_boost_terms=payments_hit or {"payments"},
            detail="payments/fintech domain signal in JD title or domain_signals",
        )

    if (
        any(tok in title_lower for tok in ("sdet", "qa engineer", "qa automation", "quality assurance", "test engineer"))
        or cat_counts.get(RequirementCategory.TESTING, 0) >= 2
    ):
        return RoleClassification(
            RoleArchetype.QA_SDET,
            category_boosts={RequirementCategory.TESTING: 1.5},
            detail="QA/SDET-shaped title or 2+ TESTING-category requirements",
        )

    if "platform" in title_lower:
        return RoleClassification(
            RoleArchetype.PLATFORM,
            category_boosts={RequirementCategory.DEVOPS: 1.3, RequirementCategory.CLOUD: 1.2, RequirementCategory.ARCHITECTURE: 1.2},
            detail="platform-shaped title",
        )

    if any(tok in title_lower for tok in ("infrastructure", "site reliability", "sre")):
        return RoleClassification(
            RoleArchetype.INFRASTRUCTURE,
            category_boosts={RequirementCategory.DEVOPS: 1.4, RequirementCategory.OBSERVABILITY: 1.3},
            detail="infrastructure/SRE-shaped title",
        )

    if "cloud" in title_lower or (cat_counts.get(RequirementCategory.CLOUD, 0) >= 2 and cat_counts.get(RequirementCategory.DEVOPS, 0) >= 1):
        return RoleClassification(
            RoleArchetype.CLOUD,
            category_boosts={RequirementCategory.CLOUD: 1.4},
            detail="cloud-shaped title or CLOUD+DEVOPS-heavy requirements",
        )

    if (
        any(tok in title_lower for tok in ("full stack", "full-stack", "fullstack"))
        or (cat_counts.get(RequirementCategory.FRONTEND, 0) >= 1 and cat_counts.get(RequirementCategory.BACKEND, 0) >= 1)
    ):
        return RoleClassification(
            RoleArchetype.FULL_STACK,
            category_boosts={RequirementCategory.FRONTEND: 1.2, RequirementCategory.BACKEND: 1.2},
            detail="full-stack-shaped title or FRONTEND+BACKEND requirements both present",
        )

    # AI/backend: only when the JD genuinely asks for it AND the candidate
    # has genuinely verified DATA_ML evidence -- "AI/backend when evidence
    # supports it" (CLAUDE.md JD intelligence v3). Never inferred from the
    # JD alone, which would risk implying ML experience the candidate
    # doesn't have.
    ai_evidence = any(
        e.level == EvidenceLevel.DIRECT_VERIFIED and _SKILL_CATEGORY.get(e.skill.lower().strip()) == RequirementCategory.DATA_ML
        for e in graph.skills.values()
    )
    if cat_counts.get(RequirementCategory.DATA_ML, 0) >= 1 and ai_evidence:
        return RoleClassification(
            RoleArchetype.AI_BACKEND,
            category_boosts={RequirementCategory.DATA_ML: 1.4, RequirementCategory.BACKEND: 1.1},
            detail="JD asks for AI/ML AND candidate has verified DATA_ML evidence",
        )

    if any(tok in title_lower for tok in ("backend", "back-end", "back end", "api", "application engineer", "software engineer", "python")):
        return RoleClassification(
            RoleArchetype.BACKEND,
            category_boosts={RequirementCategory.BACKEND: 1.2},
            detail="backend-shaped title (default technical-role framing)",
        )

    return RoleClassification(RoleArchetype.GENERAL, detail="no specific role archetype signal detected")


# Fixed, hardcoded role-family names -- one per archetype, deliberately
# containing no technology or seniority word, so nothing here can ever be an
# "unsupported skill/seniority leakage" (verified against SKILL_VOCAB by
# construction; see the module docstring).
_ARCHETYPE_ROLE_FAMILY: dict[RoleArchetype, str] = {
    RoleArchetype.BACKEND: "Backend Software Engineer",
    RoleArchetype.PAYMENTS: "Software Engineer",
    RoleArchetype.PLATFORM: "Platform Engineer",
    RoleArchetype.INFRASTRUCTURE: "Infrastructure Engineer",
    RoleArchetype.FULL_STACK: "Software Engineer",
    RoleArchetype.QA_SDET: "QA Automation Engineer",
    RoleArchetype.CLOUD: "Cloud Software Engineer",
    RoleArchetype.AI_BACKEND: "Backend Software Engineer",
    RoleArchetype.GENERAL: "Software Engineer",
}

# Public (not underscore-prefixed) so app.resume.claim_checker can validate
# an already-built target role's family phrase against the SAME authoritative
# set this module builds from -- an independent CHECK on the result, not a
# hand-copied duplicate list that could drift.
SAFE_ROLE_FAMILY_NAMES: frozenset[str] = frozenset(v.lower() for v in _ARCHETYPE_ROLE_FAMILY.values())

# Technology-qualifier candidates: LANGUAGE-category only (a single
# "<Language> " prefix reads naturally on a headline; framework/tool/cloud
# words do not, and are already reflected via the role family/domain
# qualifier instead). Order is the priority when a JD title names more than
# one -- deterministic, and only the first BOTH-named-and-verified language
# is ever used (never more than one qualifier word).
_LANGUAGE_QUALIFIER_ORDER = ("python", "java", "kotlin", "javascript", "typescript", "golang", "go", "c++", "c#")
_LANGUAGE_DISPLAY: dict[str, str] = {
    "python": "Python", "java": "Java", "kotlin": "Kotlin", "javascript": "JavaScript",
    "typescript": "TypeScript", "golang": "Go", "go": "Go", "c++": "C++", "c#": "C#",
}
# Public for app.resume.claim_checker's independent re-validation (same
# no-hand-copied-duplicate-list rationale as SAFE_ROLE_FAMILY_NAMES above).
SAFE_LANGUAGE_QUALIFIERS: frozenset[str] = frozenset(_LANGUAGE_QUALIFIER_ORDER)


def _select_tech_qualifier(job_title: str, graph: EvidenceGraph) -> str:
    """Only a language BOTH named in the JD title AND `DIRECT_VERIFIED` in
    the candidate's own evidence graph -- e.g. a JD titled "Java Backend
    Engineer" for a Python-only candidate yields "" here (Java is named but
    not verified; Python is verified but not named in a Java JD's title),
    so the resulting headline is always "Backend Software Engineer", never
    fabricated technology."""
    title_lower = (job_title or "").lower()
    for lang in _LANGUAGE_QUALIFIER_ORDER:
        if not re.search(rf"\b{re.escape(lang)}\b", title_lower):
            continue
        evidence = graph.skills.get(lang)
        if evidence and evidence.level == EvidenceLevel.DIRECT_VERIFIED:
            return _LANGUAGE_DISPLAY.get(lang, lang.title())
    return ""


def _select_domain_qualifier(jd_analysis: JDAnalysisResult, graph: EvidenceGraph) -> str:
    """Only a domain BOTH extracted from the JD (`jd_analysis.domain_signals`)
    AND present in the candidate's own verified evidence (`graph.domains`,
    built purely from profile text -- see app.resume_optimizer.evidence)."""
    jd_domains = set(jd_analysis.domain_signals)
    candidate_domains = set(graph.domains)
    for d in DOMAIN_SIGNALS:  # deterministic order
        if d in jd_domains and d in candidate_domains:
            return d.title()
    return ""


def build_target_role(job_title: str, jd_analysis: JDAnalysisResult, graph: EvidenceGraph, role: RoleClassification) -> str:
    """Builds the resume's TARGET ROLE / headline string -- always a
    composition of the three independently-gated, truthful parts described
    in the module docstring. Deterministic: identical inputs always produce
    an identical string (pure function, no randomness/ordering ambiguity)."""
    family = _ARCHETYPE_ROLE_FAMILY.get(role.archetype, "Software Engineer")
    tech = _select_tech_qualifier(job_title, graph)
    headline = f"{tech} {family}".strip() if tech else family
    domain = _select_domain_qualifier(jd_analysis, graph)
    if domain:
        headline = f"{headline}, {domain}"
    return headline
