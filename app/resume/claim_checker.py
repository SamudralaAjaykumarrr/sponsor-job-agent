from app.candidate.schema import CandidateProfile
from app.resume.generator import ResumeContent


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

    verified_projects = {p.name for p in profile.projects}
    for p in resume.projects:
        if p.name not in verified_projects:
            violations.append(f"Unverified project claimed: '{p.name}'")

    verified_education = {(ed.school, ed.degree) for ed in profile.education}
    for ed in resume.education:
        if (ed.school, ed.degree) not in verified_education:
            violations.append(f"Unverified education entry claimed: '{ed.school}' / '{ed.degree}'")

    return violations
