import re

from app.candidate.schema import CandidateProfile
from app.resume.generator import ResumeContent
from app.resume_optimizer.jd_analysis import DOMAIN_SIGNALS, SKILL_VOCAB, domain_signal_present
from app.resume_optimizer.role_classification import SAFE_LANGUAGE_QUALIFIERS, SAFE_ROLE_FAMILY_NAMES

# JD intelligence v3 correction: a resume's TARGET ROLE / headline line
# (app.resume_optimizer.role_classification.build_target_role) is a
# forward-looking "what I'm applying for" statement, NOT a claim of prior
# employment -- it is therefore validated as its own, SEPARATE claim type
# below, never required to equal a verified `EmploymentEntry.title` (see
# that module's docstring for the full rationale). No seniority word may
# ever appear: build_target_role's hardcoded role families are deliberately
# seniority-neutral, so there is no "verified seniority" carve-out here --
# any seniority word at all is unsupported by construction.
_SENIORITY_TOKENS = frozenset({"staff", "principal", "architect", "director", "vp", "vice", "senior", "sr", "lead"})
_SKILL_VOCAB_PHRASES = tuple(sorted((p for p, _c in SKILL_VOCAB), key=len, reverse=True))


def _validate_target_role(target_role: str, profile: CandidateProfile) -> list[str]:
    """Independently re-validates a resume's target-role/headline string
    against the verified profile -- defense in depth, not a re-run of
    app.resume_optimizer.role_classification.build_target_role's own logic.
    Checks, in order: role family supported, technology qualifier supported,
    seniority supported (never present at all), domain qualifier supported,
    and a final catch-all scan for any other unsupported skill leakage."""
    violations: list[str] = []
    if not target_role:
        return violations

    if "," in target_role:
        family_and_tech, domain_part = (part.strip() for part in target_role.split(",", 1))
    else:
        family_and_tech, domain_part = target_role.strip(), ""

    words = family_and_tech.lower().split()
    tech_words = [words[0]] if words and words[0] in SAFE_LANGUAGE_QUALIFIERS else []
    family_phrase = " ".join(words[len(tech_words):])

    if family_phrase not in SAFE_ROLE_FAMILY_NAMES:
        violations.append(f"Unsupported target role family: '{family_phrase}'")

    verified_skills_lower = {s.lower() for s in profile.skills}
    for tech in tech_words:
        if tech not in verified_skills_lower:
            violations.append(f"Unverified technology qualifier in target role: '{tech}'")

    for w in words:
        if w in _SENIORITY_TOKENS:
            violations.append(f"Unsupported seniority claim in target role: '{w}'")

    if domain_part:
        domain_text = " ".join(
            [b for e in profile.employment for b in e.verified_bullets]
            + [b for p in profile.projects for b in p.verified_bullets]
            + [p.description for p in profile.projects]
            + [e.title for e in profile.employment]
        ).lower()
        domain_lower = domain_part.lower()
        matched_domain = any(d.lower() == domain_lower and domain_signal_present(domain_text, d) for d in DOMAIN_SIGNALS)
        if not matched_domain:
            violations.append(f"Unverified domain qualifier in target role: '{domain_part}'")

    for phrase in _SKILL_VOCAB_PHRASES:
        if phrase in tech_words:
            continue
        if re.search(rf"\b{re.escape(phrase)}\b", family_and_tech.lower()) and phrase not in verified_skills_lower:
            violations.append(f"Unverified skill leaked into target role: '{phrase}'")

    return violations


def check_resume_claims(resume: ResumeContent, profile: CandidateProfile) -> list[str]:
    """Verifies every claim on the generated resume traces back to the verified
    candidate profile. Returns a list of violation messages (empty == safe)."""
    violations: list[str] = []

    allowed_skills = {s.lower() for s in profile.skills}
    for s in resume.skills_ordered:
        if s.lower() not in allowed_skills:
            violations.append(f"Unverified skill claimed on resume: '{s}'")

    allowed_bullets = set()
    for e in profile.employment:
        allowed_bullets.update(e.verified_bullets)
    for p in profile.projects:
        allowed_bullets.update(p.verified_bullets)

    for e in resume.experience:
        for b in e.bullets:
            if b not in allowed_bullets:
                violations.append(f"Unverified experience bullet claimed: '{b}'")

    for p in resume.projects:
        for b in p.bullets:
            if b not in allowed_bullets:
                violations.append(f"Unverified project bullet claimed: '{b}'")

    verified_employment = {(e.company, e.title) for e in profile.employment}
    for e in resume.experience:
        if (e.company, e.title) not in verified_employment:
            violations.append(f"Unverified employment entry claimed: '{e.company}' / '{e.title}'")

    # JD intelligence v3: the target-role/headline line is validated as its
    # own, separate claim type -- see _validate_target_role's docstring for
    # why it is never compared against verified_employment above.
    violations.extend(_validate_target_role(resume.target_role, profile))

    verified_projects = {p.name for p in profile.projects}
    for p in resume.projects:
        if p.name not in verified_projects:
            violations.append(f"Unverified project claimed: '{p.name}'")

    verified_education = {(ed.school, ed.degree) for ed in profile.education}
    for ed in resume.education:
        if (ed.school, ed.degree) not in verified_education:
            violations.append(f"Unverified education entry claimed: '{ed.school}' / '{ed.degree}'")

    return violations
