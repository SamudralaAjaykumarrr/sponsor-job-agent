"""Candidate-data -> generic application field mapping (CLAUDE.md Phase 8
sections 8-13). candidate_data/profile.json (via app.candidate.profile) is
the ONLY candidate truth source -- nothing here infers a value the profile
doesn't already contain."""

from app.applications.models import ApplicationField, FieldCategory, FieldConfidence, SENSITIVE_CATEGORIES
from app.candidate.schema import CandidateProfile
from app.config import NEEDS_USER_INPUT

# Values that, when offered as a choice on a real form, are the safe default
# for a demographic/voluntary question the candidate hasn't stated an answer
# to (CLAUDE.md Phase 8 section 11) -- never fabricated, only ever used to
# recognize a matching offered choice.
DECLINE_TO_SELF_IDENTIFY_PHRASES = [
    "decline to self identify", "prefer not to say",
    # Every caller normalizes a candidate choice string via
    # `c.lower().replace("'", "")` before matching against this list, which
    # DELETES the apostrophe rather than replacing it with a space (so
    # "don't" becomes "dont", not "don t") -- both forms are listed here
    # defensively so this stays correct regardless of which normalization a
    # given caller applies.
    "i dont wish to answer", "i don t wish to answer",
    "i do not wish to answer",
    "i dont want to answer", "i don t want to answer",
    # CLAUDE.md Phase 11 section 24: additional common real-EEOC-form
    # variants, added conservatively -- each phrase contains a distinctive
    # verb+negation combination ("prefer/rather/wish/choose" + "not" +
    # "disclose/answer/say") deliberately unlikely to appear as a substring
    # of an ordinary, real demographic answer choice (never a bare word like
    # "prefer" or "disclose" alone, which WOULD risk overmatching).
    "prefer not to disclose", "rather not say", "rather not disclose",
    "do not wish to disclose", "dont wish to disclose",
    "choose not to answer", "choose not to disclose",
]


def _has_value(v) -> bool:
    return v is not None and v != "" and v != NEEDS_USER_INPUT


def _field(
    field_id: str, label: str, category: FieldCategory, normalized_type: str, *,
    required: bool, value_source: str, verified_value,
    reason: str = "",
) -> ApplicationField:
    has_value = _has_value(verified_value)
    sensitive = category in SENSITIVE_CATEGORIES
    if not has_value:
        return ApplicationField(
            field_id=field_id, label=label, category=category, normalized_type=normalized_type,
            required=required, value_source=value_source, verified_value=None,
            confidence=FieldConfidence.LOW, needs_user_input=True, sensitive=sensitive,
            auto_fill_allowed=False, reason=reason or "no verified value in candidate profile",
        )
    confidence = FieldConfidence.EXACT
    # Sensitive categories require an explicit stricter human read even when
    # the value is present -- section 15 "sensitive/legal fields require
    # stricter threshold" -- auto_fill is allowed here (the candidate DID
    # provide a real, truthful answer in their profile) but confidence is
    # capped at HIGH rather than EXACT so downstream review surfaces it.
    if sensitive:
        confidence = FieldConfidence.HIGH
    return ApplicationField(
        field_id=field_id, label=label, category=category, normalized_type=normalized_type,
        required=required, value_source=value_source, verified_value=str(verified_value),
        confidence=confidence, needs_user_input=False, sensitive=sensitive,
        auto_fill_allowed=True, reason=reason or "verified candidate profile value",
    )


def build_application_fields(profile: CandidateProfile, resume_path: str = "", cover_letter_path: str = "") -> list[ApplicationField]:
    """Builds the full canonical field set from the verified candidate
    profile. Callers (app.applications.executor) then run
    app.applications.mapping.match_field() per real form field and look up
    the resulting canonical id in this list."""
    c = profile.contact
    wa = profile.work_authorization
    prefs = profile.preferences
    sa = profile.standard_answers

    fields: list[ApplicationField] = []

    fields.append(_field("full_name", "Full Name", FieldCategory.CONTACT, "text",
                          required=True, value_source="contact.full_name", verified_value=c.full_name))
    if _has_value(c.full_name) and " " in c.full_name.strip():
        first, _, last = c.full_name.strip().partition(" ")
        fields.append(_field("first_name", "First Name", FieldCategory.CONTACT, "text",
                              required=True, value_source="contact.full_name", verified_value=first))
        fields.append(_field("last_name", "Last Name", FieldCategory.CONTACT, "text",
                              required=True, value_source="contact.full_name", verified_value=last))
    else:
        fields.append(_field("first_name", "First Name", FieldCategory.CONTACT, "text",
                              required=True, value_source="contact.full_name", verified_value=None))
        fields.append(_field("last_name", "Last Name", FieldCategory.CONTACT, "text",
                              required=True, value_source="contact.full_name", verified_value=None))

    fields.append(_field("email", "Email", FieldCategory.CONTACT, "text",
                          required=True, value_source="contact.email", verified_value=c.email))
    fields.append(_field("phone", "Phone", FieldCategory.CONTACT, "text",
                          required=False, value_source="contact.phone", verified_value=c.phone))
    fields.append(_field("city", "City", FieldCategory.LOCATION, "text",
                          required=False, value_source="contact.city", verified_value=c.city))
    fields.append(_field("state", "State", FieldCategory.LOCATION, "text",
                          required=False, value_source="contact.state", verified_value=c.state))
    location = f"{c.city}, {c.state}" if _has_value(c.city) and _has_value(c.state) else None
    fields.append(_field("location", "Location", FieldCategory.LOCATION, "text",
                          required=False, value_source="contact.city+state", verified_value=location))
    fields.append(_field("linkedin_url", "LinkedIn Profile", FieldCategory.CONTACT, "text",
                          required=False, value_source="contact.linkedin_url", verified_value=c.linkedin_url))
    fields.append(_field("github_url", "GitHub Profile", FieldCategory.CONTACT, "text",
                          required=False, value_source="contact.github_url", verified_value=c.github_url))
    fields.append(_field("portfolio_url", "Portfolio", FieldCategory.CONTACT, "text",
                          required=False, value_source="contact.portfolio_url", verified_value=c.portfolio_url))

    fields.append(_field("resume_file", "Resume/CV", FieldCategory.FILE_UPLOAD, "file",
                          required=True, value_source="generated_resume", verified_value=resume_path or None))
    fields.append(_field("cover_letter_file", "Cover Letter", FieldCategory.FILE_UPLOAD, "file",
                          required=False, value_source="generated_cover_letter",
                          verified_value=cover_letter_path or None))

    if profile.employment:
        latest = profile.employment[0]
        fields.append(_field("current_employer", "Current Employer", FieldCategory.EMPLOYMENT, "text",
                              required=False, value_source="employment[0].company", verified_value=latest.company))
        fields.append(_field("current_title", "Current Title", FieldCategory.EMPLOYMENT, "text",
                              required=False, value_source="employment[0].title", verified_value=latest.title))
    else:
        fields.append(_field("current_employer", "Current Employer", FieldCategory.EMPLOYMENT, "text",
                              required=False, value_source="employment[0].company", verified_value=None))
        fields.append(_field("current_title", "Current Title", FieldCategory.EMPLOYMENT, "text",
                              required=False, value_source="employment[0].title", verified_value=None))

    fields.append(_field("years_experience", "Years of Experience", FieldCategory.EXPERIENCE, "text",
                          required=False, value_source="standard_answers.years_of_experience",
                          verified_value=sa.years_of_experience))

    if profile.education:
        ed = profile.education[0]
        fields.append(_field("education_school", "School", FieldCategory.EDUCATION, "text",
                              required=False, value_source="education[0].school", verified_value=ed.school))
        fields.append(_field("education_degree", "Degree", FieldCategory.EDUCATION, "text",
                              required=False, value_source="education[0].degree", verified_value=ed.degree))
    else:
        fields.append(_field("education_school", "School", FieldCategory.EDUCATION, "text",
                              required=False, value_source="education[0].school", verified_value=None))
        fields.append(_field("education_degree", "Degree", FieldCategory.EDUCATION, "text",
                              required=False, value_source="education[0].degree", verified_value=None))

    relocate_value = sa.willing_to_relocate if sa.willing_to_relocate is not None else prefs.relocation_open
    relocate_str = None if relocate_value is None else ("Yes" if relocate_value else "No")
    fields.append(_field("willing_to_relocate", "Willing to Relocate", FieldCategory.RELOCATION, "boolean",
                          required=False, value_source="standard_answers.willing_to_relocate",
                          verified_value=relocate_str))

    fields.append(_field("salary_expectation", "Desired Salary", FieldCategory.SALARY, "text",
                          required=False, value_source="preferences.salary_min_usd",
                          verified_value=prefs.salary_min_usd))

    fields.append(_field("notice_period", "Notice Period", FieldCategory.NOTICE_PERIOD, "text",
                          required=False, value_source="standard_answers.notice_period",
                          verified_value=sa.notice_period))

    fields.append(_field("work_authorization_status", "Work Authorization Status",
                          FieldCategory.WORK_AUTHORIZATION, "text",
                          required=True, value_source="work_authorization.current_status",
                          verified_value=wa.current_status))

    # --- CLAUDE.md Phase 8 section 10: sponsorship answers, truthful and
    # never hidden/misrepresented. ---
    sponsorship_answer = None
    if wa.requires_sponsorship is not None:
        sponsorship_answer = "Yes" if wa.requires_sponsorship else "No"
    fields.append(_field(
        "future_sponsorship_required", "Will you now or in the future require sponsorship?",
        FieldCategory.SPONSORSHIP, "boolean", required=True,
        value_source="work_authorization.requires_sponsorship", verified_value=sponsorship_answer,
        reason="candidate's stated current/future sponsorship need -- must never be misrepresented",
    ))
    fields.append(_field("sponsorship_type", "Sponsorship Type Needed", FieldCategory.SPONSORSHIP, "text",
                          required=False, value_source="work_authorization.sponsorship_type_needed",
                          verified_value=wa.sponsorship_type_needed))

    # --- CLAUDE.md Phase 8 section 11: demographic/voluntary -- never
    # inferred, only ever what the candidate explicitly put in their profile. ---
    fields.append(_field("veteran_status", "Veteran Status", FieldCategory.DEMOGRAPHICS, "select",
                          required=False, value_source="standard_answers.veteran_status",
                          verified_value=sa.veteran_status))
    fields.append(_field("disability_status", "Disability Status", FieldCategory.DEMOGRAPHICS, "select",
                          required=False, value_source="standard_answers.disability_status",
                          verified_value=sa.disability_status))
    fields.append(_field("gender", "Gender", FieldCategory.DEMOGRAPHICS, "select",
                          required=False, value_source="standard_answers.gender", verified_value=sa.gender))
    fields.append(_field("race_ethnicity", "Race/Ethnicity", FieldCategory.DEMOGRAPHICS, "select",
                          required=False, value_source="standard_answers.race_ethnicity",
                          verified_value=sa.race_ethnicity))

    # --- CLAUDE.md Phase 8 section 12: legal/attestation -- NEVER guessed.
    # Not present anywhere in the candidate profile schema by design, so
    # these always land as needs_user_input=True. ---
    for fid, label in (
        ("criminal_history", "Criminal History Disclosure"),
        ("security_clearance", "Security Clearance"),
        ("export_control", "Export Control / ITAR"),
        ("non_compete", "Non-Compete / Employment Agreement Restrictions"),
        ("government_employment", "Prior Government Employment"),
        ("conflict_of_interest", "Conflict of Interest"),
        ("background_check_consent", "Background Check Consent"),
        ("drug_testing", "Drug Testing Consent"),
        ("signature", "Signature / Certification"),
    ):
        fields.append(_field(
            fid, label, FieldCategory.LEGAL_ATTESTATION if fid != "signature" else FieldCategory.SIGNATURE,
            "text", required=False, value_source="", verified_value=None,
            reason="legal/attestation questions are never guessed -- always NEEDS_USER_ACTION",
        ))

    return fields


def find_field(fields: list[ApplicationField], field_id: str) -> ApplicationField | None:
    for f in fields:
        if f.field_id == field_id:
            return f
    return None
