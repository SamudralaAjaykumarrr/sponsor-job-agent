"""JD requirement <-> candidate evidence matching (CLAUDE.md Phase 14
section 9, acceptance scenarios A-E). Produces one MATCHED/PARTIAL/
TRANSFERABLE/MISSING/UNSUPPORTED verdict per extracted JD requirement, with
supporting evidence ids -- this is the single source both quality diagnostics
and resume content selection read from."""

from app.candidate.schema import CandidateProfile
from app.resume_optimizer.evidence import transferable_evidence_for_category
from app.resume_optimizer.models import (
    EvidenceGraph,
    EvidenceLevel,
    JDRequirementItem,
    MatchStatus,
    RequirementCategory,
    RequirementMatch,
    SKILL_CATEGORIES,
    TRANSFERABLE_ELIGIBLE_CATEGORIES,
)


def _match_alternative_requirement(req: JDRequirementItem, graph: EvidenceGraph) -> RequirementMatch:
    """Post-release bug fix (real Airbnb Payments JD): "Proficient in at
    least one major programming language (preferably Java/Kotlin/Python)" is
    ONE requirement satisfied by ANY verified alternative -- never three
    separate MISSING items just because only one alternative is verified."""
    alts = req.alternatives
    direct = [a for a in alts if (e := graph.skills.get(a)) and e.level == EvidenceLevel.DIRECT_VERIFIED]
    if direct:
        chosen = direct[0]
        evidence = graph.skills[chosen]
        return RequirementMatch(
            requirement=req, status=MatchStatus.MATCHED,
            evidence_ids=[f"skill:{chosen}"],
            explanation=(
                f"Alternative requirement ({' / '.join(alts)}) satisfied via directly verified "
                f"'{evidence.skill}' ({', '.join(evidence.supporting_sources) or 'candidate profile'})."
            ),
        )
    familiar = [a for a in alts if (e := graph.skills.get(a)) and e.level == EvidenceLevel.FAMILIAR_ONLY]
    if familiar:
        return RequirementMatch(
            requirement=req, status=MatchStatus.PARTIAL,
            evidence_ids=[f"skill:{a}" for a in familiar],
            explanation=(
                f"Alternative requirement ({' / '.join(alts)}): only familiar-level evidence "
                f"for {', '.join(familiar)}, no specific employment/project bullet backs it."
            ),
        )
    transferable = transferable_evidence_for_category(graph, req.category)
    if transferable and req.category in TRANSFERABLE_ELIGIBLE_CATEGORIES:
        names = ", ".join(sorted({t.skill for t in transferable})[:3])
        return RequirementMatch(
            requirement=req, status=MatchStatus.TRANSFERABLE,
            evidence_ids=[f"skill:{t.skill.lower()}" for t in transferable],
            explanation=(
                f"No direct evidence for alternative requirement ({' / '.join(alts)}); transferable "
                f"experience via verified {req.category.value.lower()} work with {names} "
                f"(never claimed as hands-on {' / '.join(alts)})."
            ),
        )
    return RequirementMatch(
        requirement=req, status=MatchStatus.MISSING,
        explanation=f"No verified evidence of any alternative in ({' / '.join(alts)}) in candidate profile.",
    )


def _match_skill_requirement(req: JDRequirementItem, graph: EvidenceGraph) -> RequirementMatch:
    if req.alternatives:
        return _match_alternative_requirement(req, graph)
    skill_lower = req.normalized_value.lower().strip()
    evidence = graph.skills.get(skill_lower)

    if evidence and evidence.level == EvidenceLevel.DIRECT_VERIFIED:
        return RequirementMatch(
            requirement=req, status=MatchStatus.MATCHED,
            evidence_ids=[f"skill:{skill_lower}"],
            explanation=f"Directly verified via {', '.join(evidence.supporting_sources) or 'candidate profile'}.",
        )
    if evidence and evidence.level == EvidenceLevel.FAMILIAR_ONLY:
        return RequirementMatch(
            requirement=req, status=MatchStatus.PARTIAL,
            evidence_ids=[f"skill:{skill_lower}"],
            explanation="Listed as a familiar skill but no specific employment/project bullet backs it.",
        )

    # CLAUDE.md section 8: never claim hands-on use of the missing tech --
    # only note genuinely analogous verified experience, and only for
    # categories where that analogy is honestly defensible.
    transferable = transferable_evidence_for_category(graph, req.category)
    if transferable and req.category in TRANSFERABLE_ELIGIBLE_CATEGORIES:
        names = ", ".join(sorted({t.skill for t in transferable})[:3])
        return RequirementMatch(
            requirement=req, status=MatchStatus.TRANSFERABLE,
            evidence_ids=[f"skill:{t.skill.lower()}" for t in transferable],
            explanation=(
                f"No direct '{req.text}' evidence; transferable experience via verified "
                f"{req.category.value.lower()} work with {names} (never claimed as hands-on {req.text})."
            ),
        )

    return RequirementMatch(
        requirement=req, status=MatchStatus.MISSING,
        explanation=f"No verified evidence of '{req.text}' in candidate profile.",
    )


# Post-release bug fix (real Airbnb Payments JD, section 4): a bare
# RESPONSIBILITY_SIGNALS word like "testing" previously only ever matched
# literal bullet TEXT containing that exact word -- it never consulted the
# candidate's own verified TESTING-category SKILL evidence (e.g. "Unit
# Testing", "Integration Testing"). A real candidate's bullets commonly use
# a morphological variant ("wrote unit tests", "integration test suites")
# rather than the bare noun, so genuine verified testing evidence was
# reported as MISSING. Maps a responsibility signal to genuinely equivalent
# verified-skill terms; only ever surfaces evidence that is already
# DIRECT_VERIFIED (skills_used membership or a real supporting bullet) --
# never fabricated.
_RESPONSIBILITY_SKILL_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "testing": ("unit testing", "integration testing", "automated testing", "test automation", "tdd", "pytest", "junit"),
}


def _match_responsibility(req: JDRequirementItem, graph: EvidenceGraph) -> RequirementMatch:
    matches = graph.responsibility_evidence.get(req.normalized_value, [])
    if matches:
        return RequirementMatch(
            requirement=req, status=MatchStatus.MATCHED,
            evidence_ids=[f"responsibility:{req.normalized_value}"],
            explanation=f"Verified bullet evidence: \"{matches[0]}\"",
        )
    for equiv_skill in _RESPONSIBILITY_SKILL_EQUIVALENTS.get(req.normalized_value, ()):
        evidence = graph.skills.get(equiv_skill)
        if evidence and evidence.level == EvidenceLevel.DIRECT_VERIFIED:
            return RequirementMatch(
                requirement=req, status=MatchStatus.MATCHED,
                evidence_ids=[f"skill:{equiv_skill}"],
                explanation=(
                    f"Verified via directly verified skill '{evidence.skill}' "
                    f"({', '.join(evidence.supporting_sources) or 'candidate profile'})."
                ),
            )
    return RequirementMatch(
        requirement=req, status=MatchStatus.MISSING,
        explanation=f"No verified bullet or equivalent skill evidence demonstrates '{req.text}'.",
    )


def _match_education(req: JDRequirementItem, profile: CandidateProfile) -> RequirementMatch:
    rank = {"phd": 3, "master's degree": 2, "bachelor's degree": 1, "computer science degree": 1}
    required_rank = rank.get(req.normalized_value.lower(), 1)
    for ed in profile.education:
        degree = f"{ed.degree} {ed.field_of_study}".lower()
        candidate_rank = 0
        if "phd" in degree or "ph.d" in degree:
            candidate_rank = 3
        elif "master" in degree or degree.strip().startswith("m.s") or " ms " in f" {degree} ":
            candidate_rank = 2
        elif "bachelor" in degree or degree.strip().startswith("b.s") or "b.s." in degree:
            candidate_rank = 1
        if candidate_rank >= required_rank:
            return RequirementMatch(
                requirement=req, status=MatchStatus.MATCHED,
                evidence_ids=[f"education:{ed.school}"],
                explanation=f"Verified education: {ed.degree} in {ed.field_of_study}, {ed.school}.",
            )
    return RequirementMatch(
        requirement=req, status=MatchStatus.MISSING,
        explanation=f"JD asks for {req.text}; no verified education meets this in candidate profile.",
    )


def _match_certification(req: JDRequirementItem) -> RequirementMatch:
    # CandidateProfile has no verified certifications field -- CLAUDE.md
    # section 17 requires this to always surface as missing rather than be
    # fabricated or silently ignored.
    return RequirementMatch(
        requirement=req, status=MatchStatus.MISSING,
        explanation=f"JD asks for '{req.text}'; no verified certification exists in candidate profile.",
    )


def _match_years(req: JDRequirementItem, profile: CandidateProfile) -> RequirementMatch:
    required_years = float(req.normalized_value)
    candidate_years = profile.standard_answers.years_of_experience
    if candidate_years is None:
        return RequirementMatch(
            requirement=req, status=MatchStatus.MISSING,
            explanation="Candidate years_of_experience is NEEDS_USER_INPUT -- cannot compare.",
        )
    if candidate_years >= required_years:
        return RequirementMatch(
            requirement=req, status=MatchStatus.MATCHED,
            evidence_ids=["years_of_experience"],
            explanation=f"Candidate has {candidate_years:g} verified years >= required {required_years:g}.",
        )
    return RequirementMatch(
        requirement=req, status=MatchStatus.PARTIAL,
        evidence_ids=["years_of_experience"],
        explanation=(
            f"JD asks for {required_years:g}+ years; candidate has {candidate_years:g} verified years. "
            "Gap shown -- years are never altered to satisfy a JD."
        ),
    )


def match_requirements(
    requirements: list[JDRequirementItem], graph: EvidenceGraph, profile: CandidateProfile,
) -> list[RequirementMatch]:
    results: list[RequirementMatch] = []
    for req in requirements:
        if req.category in SKILL_CATEGORIES:
            results.append(_match_skill_requirement(req, graph))
        elif req.category == RequirementCategory.RESPONSIBILITY:
            results.append(_match_responsibility(req, graph))
        elif req.category == RequirementCategory.EDUCATION:
            results.append(_match_education(req, profile))
        elif req.category == RequirementCategory.CERTIFICATION:
            results.append(_match_certification(req))
        elif req.category == RequirementCategory.YEARS_EXPERIENCE:
            results.append(_match_years(req, profile))
        else:
            results.append(RequirementMatch(requirement=req, status=MatchStatus.MISSING, explanation="Unclassified requirement."))
    return results
