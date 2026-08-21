from typing import List, Optional

from pydantic import BaseModel, Field

from app.config import NEEDS_USER_INPUT


class Contact(BaseModel):
    full_name: str = NEEDS_USER_INPUT
    email: str = NEEDS_USER_INPUT
    phone: str = NEEDS_USER_INPUT
    city: str = NEEDS_USER_INPUT
    state: str = NEEDS_USER_INPUT
    linkedin_url: str = NEEDS_USER_INPUT
    github_url: str = NEEDS_USER_INPUT
    portfolio_url: str = NEEDS_USER_INPUT


class EmploymentEntry(BaseModel):
    company: str = NEEDS_USER_INPUT
    title: str = NEEDS_USER_INPUT
    start_date: str = NEEDS_USER_INPUT
    end_date: str = NEEDS_USER_INPUT  # "Present" allowed
    location: str = NEEDS_USER_INPUT
    verified_bullets: List[str] = Field(default_factory=list)
    skills_used: List[str] = Field(default_factory=list)


class ProjectEntry(BaseModel):
    name: str = NEEDS_USER_INPUT
    description: str = NEEDS_USER_INPUT
    verified_bullets: List[str] = Field(default_factory=list)
    skills_used: List[str] = Field(default_factory=list)
    url: str = ""


class EducationEntry(BaseModel):
    school: str = NEEDS_USER_INPUT
    degree: str = NEEDS_USER_INPUT
    field_of_study: str = NEEDS_USER_INPUT
    graduation_date: str = NEEDS_USER_INPUT


class WorkAuthorization(BaseModel):
    current_status: str = NEEDS_USER_INPUT  # e.g. F-1 OPT, H-1B, etc.
    requires_sponsorship: Optional[bool] = None  # None -> NEEDS_USER_INPUT semantics
    sponsorship_type_needed: str = NEEDS_USER_INPUT  # e.g. "H-1B"
    years_us_experience: Optional[float] = None


class Preferences(BaseModel):
    relocation_open: Optional[bool] = None
    preferred_locations: List[str] = Field(default_factory=list)
    salary_min_usd: Optional[int] = None
    salary_preference_notes: str = ""
    work_arrangement_priority: List[str] = Field(
        default_factory=lambda: ["REMOTE", "HYBRID", "ONSITE"]
    )


class StandardAnswers(BaseModel):
    years_of_experience: Optional[float] = None
    notice_period: str = NEEDS_USER_INPUT
    willing_to_relocate: Optional[bool] = None
    requires_sponsorship_answer: str = NEEDS_USER_INPUT
    veteran_status: str = NEEDS_USER_INPUT
    disability_status: str = NEEDS_USER_INPUT
    race_ethnicity: str = NEEDS_USER_INPUT
    gender: str = NEEDS_USER_INPUT


class CandidateProfile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    employment: List[EmploymentEntry] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    projects: List[ProjectEntry] = Field(default_factory=list)
    education: List[EducationEntry] = Field(default_factory=list)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    preferences: Preferences = Field(default_factory=Preferences)
    standard_answers: StandardAnswers = Field(default_factory=StandardAnswers)
