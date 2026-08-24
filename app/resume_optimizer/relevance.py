"""One-page-optimization ranking model (JD intelligence v3): a single,
auditable `RelevanceModel` (term -> weight) built once per job from JD
analysis + evidence matches + role classification, then reused by both
initial bullet/skill/experience SELECTION (this module) and overflow
REMOVAL ordering (app.resume_optimizer.one_page) -- so both phases rank
content by the same signal instead of two different, driftable heuristics.

Ranks by: required relevance, responsibility relevance, domain relevance,
recency, impact (quantified-outcome language already present in a VERIFIED
bullet -- a selection signal, never rewritten/added text), keyword
usefulness, and non-redundancy (`select_bullets`' greedy marginal-coverage
selection). This module only ever SELECTS AMONG or WEIGHTS already-verified
`CandidateProfile` text -- it never edits a bullet's wording and never
invents a term that isn't already backed by a JD requirement match or a
generic ATS keyword hit, so it can never introduce an unsupported claim
(app.resume.claim_checker.check_resume_claims stays the enforcement
backstop regardless)."""

import re
from dataclasses import dataclass, field

from app.matching.skills import extract_jd_keywords
from app.resume_optimizer.models import (
    EvidenceGraph,
    JDAnalysisResult,
    MatchStatus,
    RequirementCategory,
    RequirementMatch,
    RequirementPriority,
)
from app.resume_optimizer.role_classification import RoleClassification

_IMPACT_VERB_PATTERN = re.compile(
    r"\b(reduced|increased|improved|decreased|cut|grew|saved|accelerated|optimi[sz]ed|"
    r"scaled|automated|eliminated|boosted|streamlined|migrated|launched|shipped)\b",
    re.IGNORECASE,
)


def _impact_bonus(text: str) -> float:
    """Selection-only heuristic over already-verified bullet text -- never
    alters the text itself. Rewards a bullet that already reads as a
    quantified/impact-shaped claim (digits, %, $ figures, or a
    result-oriented verb) so genuinely stronger verified bullets are kept
    over generic ones during both initial selection and overflow removal."""
    bonus = 0.0
    if re.search(r"\d", text):
        bonus += 0.5
    if "%" in text:
        bonus += 0.5
    if re.search(r"\$\s?\d", text):
        bonus += 0.3
    if _IMPACT_VERB_PATTERN.search(text):
        bonus += 0.5
    return bonus


@dataclass
class RelevanceModel:
    weights: dict[str, float] = field(default_factory=dict)  # lowercase term -> weight

    def term_hits(self, text: str) -> set[str]:
        b = text.lower()
        return {t for t in self.weights if t and t in b}

    def score(self, text: str) -> float:
        hits = self.term_hits(text)
        return sum(self.weights[t] for t in hits) + _impact_bonus(text)


_REQUIRED_BASE = 3.0
_PREFERRED_BASE = 1.5
_RESPONSIBILITY_BONUS = 1.0
_DOMAIN_BASE = 1.5
_DOMAIN_ROLE_BOOST = 1.5
_KEYWORD_BASE = 0.5


def build_relevance_model(
    jd_analysis: JDAnalysisResult,
    matches: list[RequirementMatch],
    graph: EvidenceGraph,
    role: RoleClassification,
    job_description: str = "",
) -> RelevanceModel:
    weights: dict[str, float] = {}

    def bump(term: str, value: float) -> None:
        t = term.lower().strip()
        if not t:
            return
        weights[t] = max(weights.get(t, 0.0), value)

    for m in matches:
        if m.status not in (MatchStatus.MATCHED, MatchStatus.TRANSFERABLE, MatchStatus.PARTIAL):
            continue
        base = _REQUIRED_BASE if m.requirement.priority == RequirementPriority.REQUIRED else _PREFERRED_BASE
        if m.requirement.category == RequirementCategory.RESPONSIBILITY:
            base += _RESPONSIBILITY_BONUS
        boost = role.category_boosts.get(m.requirement.category, 1.0)
        weight = base * boost
        terms = m.requirement.alternatives or [m.requirement.normalized_value]
        for term in terms:
            bump(term, weight)

    domain_terms = set(jd_analysis.domain_signals) & set(graph.domains)
    for d in domain_terms:
        role_boost = _DOMAIN_ROLE_BOOST if d in role.domain_boost_terms else 1.0
        bump(d, _DOMAIN_BASE * role_boost)

    if job_description:
        for kw in extract_jd_keywords(f"{jd_analysis.job_title}\n{job_description}"):
            if kw not in weights:
                bump(kw, _KEYWORD_BASE)

    return RelevanceModel(weights=weights)


def select_bullets(bullets: list[str], model: RelevanceModel, cap: int) -> list[str]:
    """Greedy marginal-coverage selection: each pick maximizes its own
    relevance score PLUS the number of not-yet-covered relevance terms it
    introduces, so a second bullet that only restates a term the first
    already covers loses out to a lower-raw-score bullet that covers a
    genuinely different requirement -- non-redundancy, not just a top-K sort.
    Falls back to the bullets' own original order when nothing scores above
    zero (an unweighted/GENERAL-archetype JD), matching the prior
    unweighted behavior exactly."""
    if not bullets:
        return []
    cap = min(cap, len(bullets))
    remaining = list(dict.fromkeys(bullets))  # de-dup while preserving order, defensive
    covered: set[str] = set()
    chosen: list[str] = []
    while remaining and len(chosen) < cap:
        best_bullet = None
        best_gain = None
        best_hits: set[str] = set()
        for b in remaining:
            hits = model.term_hits(b)
            novelty = len(hits - covered)
            gain = model.score(b) + 0.75 * novelty
            if best_gain is None or gain > best_gain:
                best_gain, best_bullet, best_hits = gain, b, hits
        chosen.append(best_bullet)
        covered |= best_hits
        remaining.remove(best_bullet)
    chosen_set = set(chosen)
    ordered = [b for b in bullets if b in chosen_set]
    return ordered or bullets[:1]


def score_experience_entry(bullets: list[str], skills_used: list[str], model: RelevanceModel, recency_bonus: float) -> float:
    bullets_score = sum(model.score(b) for b in bullets)
    skills_score = sum(model.weights.get(s.lower().strip(), 0.0) for s in skills_used)
    return bullets_score + skills_score + recency_bonus * 2.0


def score_project(bullets: list[str], skills_used: list[str], model: RelevanceModel) -> float:
    return sum(model.score(b) for b in bullets) + sum(model.weights.get(s.lower().strip(), 0.0) for s in skills_used)


def score_skill(skill: str, model: RelevanceModel) -> float:
    return model.weights.get(skill.lower().strip(), 0.0)
