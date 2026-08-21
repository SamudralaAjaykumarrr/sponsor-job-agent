from app.candidate.schema import CandidateProfile
from app.config import NEEDS_USER_INPUT


def _bool_or_needs_input(value) -> str:
    if value is None:
        return NEEDS_USER_INPUT
    return "Yes" if value else "No"


def generate_application_answers(profile: CandidateProfile, job_title: str, company: str) -> dict:
    sa = profile.standard_answers
    wa = profile.work_authorization
    prefs = profile.preferences

    answers = {
        "job_title": job_title,
        "company": company,
        "full_name": profile.contact.full_name,
        "email": profile.contact.email,
        "phone": profile.contact.phone,
        "years_of_experience": (
            sa.years_of_experience if sa.years_of_experience is not None else NEEDS_USER_INPUT
        ),
        "do_you_require_sponsorship": (
            sa.requires_sponsorship_answer
            if sa.requires_sponsorship_answer != NEEDS_USER_INPUT
            else _bool_or_needs_input(wa.requires_sponsorship)
        ),
        "current_work_authorization_status": wa.current_status,
        "sponsorship_type_needed": wa.sponsorship_type_needed,
        "notice_period": sa.notice_period,
        "willing_to_relocate": _bool_or_needs_input(
            sa.willing_to_relocate if sa.willing_to_relocate is not None else prefs.relocation_open
        ),
        "salary_expectation_usd": (
            prefs.salary_min_usd if prefs.salary_min_usd is not None else NEEDS_USER_INPUT
        ),
        "veteran_status": sa.veteran_status,
        "disability_status": sa.disability_status,
        "gender": sa.gender,
        "race_ethnicity": sa.race_ethnicity,
        "linkedin_url": profile.contact.linkedin_url,
        "github_url": profile.contact.github_url,
        "portfolio_url": profile.contact.portfolio_url,
    }
    return answers
