"""Candidate evidence graph (CLAUDE.md Phase 14 section 6-8). Built entirely
from `app.candidate.schema.CandidateProfile` -- never from the JD. Maps each
verified skill to the employment/project evidence that backs it, and
classifies the strength of that evidence so the optimizer/matcher can never
claim more than what's actually verified."""

from app.candidate.schema import CandidateProfile
from app.resume_optimizer.jd_analysis import SKILL_VOCAB, domain_signal_present
from app.resume_optimizer.models import EvidenceGraph, EvidenceLevel, RequirementCategory, SkillEvidence

_SKILL_CATEGORY = {phrase: category for phrase, category in SKILL_VOCAB}


def _category_of(skill: str) -> RequirementCategory:
    return _SKILL_CATEGORY.get(skill.lower().strip(), RequirementCategory.OTHER)


def build_evidence_graph(profile: CandidateProfile) -> EvidenceGraph:
    graph = EvidenceGraph()

    verified_skills = [s for s in profile.skills if s and s != "NEEDS_USER_INPUT"]
    for skill in verified_skills:
        skill_lower = skill.lower().strip()
        bullets: list[str] = []
        sources: list[str] = []
        for e in profile.employment:
            if any(skill_lower == u.lower().strip() for u in e.skills_used) or any(
                skill_lower in b.lower() for b in e.verified_bullets
            ):
                sources.append(f"employer:{e.company}")
                bullets.extend(b for b in e.verified_bullets if skill_lower in b.lower())
        for p in profile.projects:
            if any(skill_lower == u.lower().strip() for u in p.skills_used) or any(
                skill_lower in b.lower() for b in p.verified_bullets
            ):
                sources.append(f"project:{p.name}")
                bullets.extend(b for b in p.verified_bullets if skill_lower in b.lower())

        # CLAUDE.md section 7: DIRECT_VERIFIED requires the skill to be both
        # a listed verified skill AND actually tied to real employment/project
        # evidence (skills_used or a bullet mentioning it) -- a bare listed
        # skill with zero supporting evidence is FAMILIAR_ONLY, never
        # inflated to DIRECT_VERIFIED.
        level = EvidenceLevel.DIRECT_VERIFIED if sources else EvidenceLevel.FAMILIAR_ONLY
        graph.skills[skill_lower] = SkillEvidence(
            skill=skill, level=level, supporting_bullets=sorted(set(bullets)), supporting_sources=sorted(set(sources)),
        )

    # Responsibility evidence: which verified bullets speak to each
    # responsibility signal (CLAUDE.md section 12).
    from app.resume_optimizer.jd_analysis import RESPONSIBILITY_SIGNALS

    all_bullets: list[tuple[str, str]] = []
    for e in profile.employment:
        all_bullets.extend((b, f"employer:{e.company}") for b in e.verified_bullets)
    for p in profile.projects:
        all_bullets.extend((b, f"project:{p.name}") for b in p.verified_bullets)

    for signal in RESPONSIBILITY_SIGNALS:
        matches = [b for b, _src in all_bullets if signal in b.lower()]
        if matches:
            graph.responsibility_evidence[signal] = matches

    # Domain evidence: any verified bullet/description text mentioning a
    # domain signal (CLAUDE.md section 13) -- purely informational, never a
    # blocker.
    from app.resume_optimizer.jd_analysis import DOMAIN_SIGNALS

    domain_text = " ".join(
        [b for b, _s in all_bullets] + [p.description for p in profile.projects] + [e.title for e in profile.employment]
    ).lower()
    graph.domains = [d for d in DOMAIN_SIGNALS if domain_signal_present(domain_text, d)]

    return graph


def transferable_evidence_for_category(graph: EvidenceGraph, category: RequirementCategory) -> list[SkillEvidence]:
    """CLAUDE.md section 8: candidates for TRANSFERABLE framing -- other
    DIRECT_VERIFIED skills in the same category (e.g. another backend
    framework) that can honestly be described as analogous experience,
    without ever claiming hands-on use of the specific missing technology."""
    out = []
    for skill_lower, evidence in graph.skills.items():
        if evidence.level != EvidenceLevel.DIRECT_VERIFIED:
            continue
        if _category_of(skill_lower) == category and category != RequirementCategory.OTHER:
            out.append(evidence)
    return out
