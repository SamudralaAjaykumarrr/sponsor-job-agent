from dataclasses import dataclass, field

from app.candidate.schema import CandidateProfile
from app.matching.skills import extract_jd_keywords, extract_skill_ids


@dataclass
class ExperienceBlock:
    company: str
    title: str
    start_date: str
    end_date: str
    location: str
    bullets: list[str]


@dataclass
class ProjectBlock:
    name: str
    description: str
    bullets: list[str]
    url: str


@dataclass
class EducationBlock:
    school: str
    degree: str
    field_of_study: str
    graduation_date: str


@dataclass
class ResumeContent:
    full_name: str
    email: str
    phone: str
    location: str
    linkedin_url: str
    github_url: str
    portfolio_url: str
    summary: str
    skills_ordered: list[str]
    experience: list[ExperienceBlock]
    projects: list[ProjectBlock]
    education: list[EducationBlock]
    gap_skills: list[str] = field(default_factory=list)
    # Resume structure (JD intelligence v3): a short TARGET ROLE / headline
    # line under the name/contact block, rendered outside (before)
    # Professional Experience so it can never be confused with an
    # employment entry -- a forward-looking "what I'm applying for" line,
    # NOT a claim of prior employment (app.resume_optimizer
    # .role_classification.build_target_role; independently re-validated by
    # app.resume.claim_checker.check_resume_claims's own
    # `_validate_target_role`, a SEPARATE contract from the EXPERIENCE
    # section's verified-employment-title validation). "" omits the section
    # entirely (see the writers).
    target_role: str = ""


def _relevance(skills_used: list[str], jd_keyword_ids: set[str]) -> int:
    used_ids: set[str] = set()
    for s in skills_used:
        used_ids |= extract_skill_ids(s)
    return len(used_ids & jd_keyword_ids)


def generate_resume_content(
    profile: CandidateProfile, job_title: str, job_description: str
) -> ResumeContent:
    jd_keywords = extract_jd_keywords(f"{job_title}\n{job_description}")
    jd_keyword_ids = set(jd_keywords)

    # Real bug fix (canary-candidate integrity re-screen): this used to be a
    # hand-rolled `k in s.lower() or s.lower() in k` substring check --
    # exactly the same false-positive-prone pattern app.matching.skills's
    # match_candidate_skills had ("go" inside "Django", "java" inside
    # "JavaScript"). Now normalizes every candidate skill string onto the
    # SAME canonical id space as the JD keywords (extract_skill_ids) and
    # compares by set intersection, never substring containment.
    candidate_skill_ids: dict[str, set[str]] = {s: extract_skill_ids(s) for s in profile.skills}
    all_candidate_ids: set[str] = set().union(*candidate_skill_ids.values()) if candidate_skill_ids else set()

    matched_skills = [s for s in profile.skills if candidate_skill_ids[s] & jd_keyword_ids]
    rest_skills = [s for s in profile.skills if s not in matched_skills]
    skills_ordered = matched_skills + rest_skills

    gap_skills = [k for k in jd_keywords if k not in all_candidate_ids]

    experience_sorted = sorted(
        profile.employment, key=lambda e: _relevance(e.skills_used, jd_keyword_ids), reverse=True
    )
    experience = []
    for e in experience_sorted:
        bullets = [b for b in e.verified_bullets if extract_skill_ids(b) & jd_keyword_ids]
        if not bullets:
            bullets = list(e.verified_bullets)
        experience.append(
            ExperienceBlock(
                company=e.company, title=e.title, start_date=e.start_date,
                end_date=e.end_date, location=e.location, bullets=bullets,
            )
        )

    projects_sorted = sorted(
        profile.projects, key=lambda p: _relevance(p.skills_used, jd_keyword_ids), reverse=True
    )
    projects = []
    for p in projects_sorted[:3]:
        bullets = [b for b in p.verified_bullets if extract_skill_ids(b) & jd_keyword_ids]
        if not bullets:
            bullets = list(p.verified_bullets)
        projects.append(ProjectBlock(name=p.name, description=p.description, bullets=bullets, url=p.url))

    education = [
        EducationBlock(
            school=ed.school, degree=ed.degree,
            field_of_study=ed.field_of_study, graduation_date=ed.graduation_date,
        )
        for ed in profile.education
    ]

    years = profile.standard_answers.years_of_experience
    years_str = f"{years} years" if years is not None else "NEEDS_USER_INPUT"
    top_skills_str = ", ".join(matched_skills[:5]) if matched_skills else "NEEDS_USER_INPUT"
    summary = (
        f"Software engineer with {years_str} of experience; "
        f"core strengths include {top_skills_str}."
    )

    return ResumeContent(
        full_name=profile.contact.full_name,
        email=profile.contact.email,
        phone=profile.contact.phone,
        location=f"{profile.contact.city}, {profile.contact.state}",
        linkedin_url=profile.contact.linkedin_url,
        github_url=profile.contact.github_url,
        portfolio_url=profile.contact.portfolio_url,
        summary=summary,
        skills_ordered=skills_ordered,
        experience=experience,
        projects=projects,
        education=education,
        gap_skills=gap_skills,
    )
