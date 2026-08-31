"""Deterministic field-mapping engine (CLAUDE.md Phase 8 section 14).

Matches an ATS form field's label/name/type against the canonical
application field vocabulary (app.applications.schema.CANONICAL_FIELD_IDS)
using: exact normalized-label match against a known alias list (EXACT),
normalized-name/id match (HIGH), then a conservative token-overlap heuristic
(MEDIUM) -- and nothing weaker than that. Section 14's "do not use unsafe
fuzzy matching for legal fields" is enforced by ALIASES being the ONLY path
that can ever match a LEGAL_ATTESTATION/DEMOGRAPHICS/VOLUNTARY_DISCLOSURE
field -- the token-overlap fallback never applies to those categories.
"""

import re

from app.applications.models import FieldCategory, FieldConfidence


def normalize_label(label: str) -> str:
    label = (label or "").lower()
    label = re.sub(r"[^a-z0-9\s]", " ", label)
    return re.sub(r"\s+", " ", label).strip()


# canonical_field_id -> (category, [alias label phrases...])
# Every alias here is an EXACT-match candidate (after normalize_label). Order
# doesn't matter -- lookup is by exact string in _ALIAS_INDEX built below.
FIELD_ALIASES: dict[str, tuple[FieldCategory, list[str]]] = {
    "first_name": (FieldCategory.CONTACT, [
        "first name", "given name", "legal first name", "first"]),
    "last_name": (FieldCategory.CONTACT, [
        "last name", "family name", "surname", "legal last name", "last"]),
    "full_name": (FieldCategory.CONTACT, ["full name", "name", "legal name"]),
    "email": (FieldCategory.CONTACT, ["email", "email address", "e mail"]),
    "phone": (FieldCategory.CONTACT, ["phone", "phone number", "mobile phone", "mobile number", "telephone"]),
    "city": (FieldCategory.LOCATION, ["city", "current city"]),
    "state": (FieldCategory.LOCATION, ["state", "state province", "province"]),
    "location": (FieldCategory.LOCATION, ["location", "current location"]),
    "linkedin_url": (FieldCategory.CONTACT, ["linkedin", "linkedin profile", "linkedin url"]),
    "github_url": (FieldCategory.CONTACT, ["github", "github profile", "github url"]),
    "portfolio_url": (FieldCategory.CONTACT, ["portfolio", "portfolio url", "website", "personal website"]),
    "resume_file": (FieldCategory.FILE_UPLOAD, ["resume", "resume cv", "cv", "resume upload"]),
    "cover_letter_file": (FieldCategory.FILE_UPLOAD, ["cover letter", "cover letter upload"]),
    "current_employer": (FieldCategory.EMPLOYMENT, ["current company", "current employer", "employer"]),
    "current_title": (FieldCategory.EMPLOYMENT, ["current title", "current job title", "job title"]),
    "years_experience": (FieldCategory.EXPERIENCE, [
        "years of experience", "total years of experience", "years experience"]),
    "education_school": (FieldCategory.EDUCATION, ["school", "university", "college"]),
    "education_degree": (FieldCategory.EDUCATION, ["degree", "highest degree"]),
    "willing_to_relocate": (FieldCategory.RELOCATION, [
        "willing to relocate", "are you willing to relocate", "open to relocation"]),
    "salary_expectation": (FieldCategory.SALARY, [
        "desired salary", "expected salary", "salary expectation", "compensation expectation"]),
    "notice_period": (FieldCategory.NOTICE_PERIOD, ["notice period", "availability", "start date availability"]),
    "work_authorization_status": (FieldCategory.WORK_AUTHORIZATION, [
        "are you legally authorized to work in the united states",
        "are you legally authorized to work in the us",
        "work authorization", "current work authorization"]),
    "future_sponsorship_required": (FieldCategory.SPONSORSHIP, [
        "will you now or in the future require sponsorship",
        "do you now or in future need employment sponsorship",
        "will you require sponsorship",
        "do you require visa sponsorship",
        "will you now or in the future require sponsorship for a visa to remain in your current location",
        "do you now or will you in the future require sponsorship for employment visa status",
    ]),
    "sponsorship_type": (FieldCategory.SPONSORSHIP, [
        "what type of visa sponsorship would you require", "visa type", "type of visa sponsorship",
        "sponsorship type",
    ]),
    "veteran_status": (FieldCategory.DEMOGRAPHICS, ["veteran status", "protected veteran status"]),
    "disability_status": (FieldCategory.DEMOGRAPHICS, ["disability status", "disabilitystatus"]),
    "gender": (FieldCategory.DEMOGRAPHICS, ["gender", "gender identity"]),
    "race_ethnicity": (FieldCategory.DEMOGRAPHICS, ["race", "ethnicity", "race ethnicity"]),
    "criminal_history": (FieldCategory.LEGAL_ATTESTATION, [
        "have you ever been convicted of a felony", "criminal history", "background check consent"]),
    "security_clearance": (FieldCategory.LEGAL_ATTESTATION, [
        "do you hold a security clearance", "security clearance"]),
    "export_control": (FieldCategory.LEGAL_ATTESTATION, ["export control", "itar"]),
    "non_compete": (FieldCategory.LEGAL_ATTESTATION, [
        "are you subject to any employment agreements and or post employment restrictions with your current employer or a past employer",
        "non compete agreement", "non-compete"]),
    "government_employment": (FieldCategory.LEGAL_ATTESTATION, [
        "have you been previously employed by a government agency", "government employment"]),
    "conflict_of_interest": (FieldCategory.LEGAL_ATTESTATION, ["conflict of interest"]),
    "background_check_consent": (FieldCategory.CONSENT, ["i consent to a background check", "background check consent"]),
    "drug_testing": (FieldCategory.LEGAL_ATTESTATION, ["drug test", "drug testing consent"]),
    "signature": (FieldCategory.SIGNATURE, ["signature", "typed signature", "electronic signature"]),
    "eeo_consent": (FieldCategory.CONSENT, ["i acknowledge", "voluntary self identification"]),
}

_ALIAS_INDEX: dict[str, str] = {}
for _fid, (_cat, _aliases) in FIELD_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_INDEX[normalize_label(_alias)] = _fid

# Legal/demographic/voluntary-disclosure/signature field ids -- ONLY these
# may ever be matched, and only via the EXACT alias path (never MEDIUM
# token-overlap). See module docstring.
_STRICT_FIELD_IDS = {
    fid for fid, (cat, _) in FIELD_ALIASES.items()
    if cat in (FieldCategory.LEGAL_ATTESTATION, FieldCategory.DEMOGRAPHICS,
               FieldCategory.VOLUNTARY_DISCLOSURE, FieldCategory.SIGNATURE, FieldCategory.CONSENT)
}


def _token_overlap_candidate(normalized_label: str) -> tuple[str, FieldCategory] | None:
    """MEDIUM-confidence fallback: every alias token for a candidate field
    must appear in the form label's token set (never the reverse -- a short
    alias like "phone" must not spuriously match a long unrelated label that
    happens to contain the word "phone" as a minor clause). Never applied to
    _STRICT_FIELD_IDS."""
    label_tokens = set(normalized_label.split())
    if not label_tokens:
        return None
    best: tuple[str, FieldCategory, int] | None = None
    for fid, (cat, aliases) in FIELD_ALIASES.items():
        if fid in _STRICT_FIELD_IDS:
            continue
        for alias in aliases:
            alias_tokens = set(normalize_label(alias).split())
            if not alias_tokens:
                continue
            if alias_tokens.issubset(label_tokens):
                score = len(alias_tokens)
                if best is None or score > best[2]:
                    best = (fid, cat, score)
    if best is None:
        return None
    return best[0], best[1]


def match_field(label: str, name: str = "") -> tuple[str | None, FieldConfidence]:
    """Returns (canonical_field_id, confidence) or (None, LOW) if no safe
    match is found."""
    norm_label = normalize_label(label)
    norm_name = normalize_label(name.replace("_", " ")) if name else ""

    if norm_label in _ALIAS_INDEX:
        return _ALIAS_INDEX[norm_label], FieldConfidence.EXACT

    if norm_name and norm_name in _ALIAS_INDEX:
        return _ALIAS_INDEX[norm_name], FieldConfidence.HIGH

    candidate = _token_overlap_candidate(norm_label)
    if candidate:
        return candidate[0], FieldConfidence.MEDIUM

    return None, FieldConfidence.LOW


def match_field_with_application_fields(label: str, name: str, application_fields: list) -> tuple[str | None, FieldConfidence]:
    """Browser-Verified Answer Canonical Readiness Integration V1: the
    single shared resolution step both app.applications.browser_runtime's
    fill pass and every ApplicationProvider.map_fields() implementation
    call, instead of bare match_field(), so a provider-specific question
    with NO entry in the fixed FIELD_ALIASES vocabulary can still be
    resolved -- but ONLY via an EXACT normalized-label match against an
    `application_fields` entry's own `.label` (never positional, never a
    fuzzy/token-overlap guess). This is how a live, human-verified answer
    (app.applications.verified_field_evidence.build_application_field_
    overrides()) reaches the SAME resolved/filled bookkeeping a generic
    profile-mapped field already gets, without weakening match_field()'s
    own existing, well-tested behavior at all -- match_field() itself is
    completely unchanged; this is a strictly additive wrapper.

    An EXACT canonical alias (e.g. "Email") always wins outright, checked
    before anything else -- a deliberate, well-tested mapping must never
    be shadowed. A `browser_verified_field_evidence`-sourced override is
    checked NEXT, before match_field()'s own weaker HIGH-confidence
    name-alias and MEDIUM-confidence token-overlap fallbacks -- a real
    live case caught exactly why this ordering matters: Robinhood's "Are
    you legally work authorized to work in the US?" (choices strictly
    Yes/No) token-overlap-matched the generic `work_authorization_status`
    canonical field (MEDIUM confidence) purely on shared words, whose
    verified_value is the candidate's raw status string ("F-1 OPT") --
    not one of the form's actual Yes/No choices, so the field stayed
    unresolved even with a confirmed, human-verified "Yes" answer sitting
    unused in the evidence table. A confirmed answer for THIS EXACT
    question is more specific and more trustworthy than a same-topic
    guess assuming a different answer shape; only a genuine EXACT
    canonical alias may still override it. Only entries this module
    itself marks as evidence-sourced (value_source ==
    "browser_verified_field_evidence") get this elevated priority --
    an ordinary generically-mapped application_fields entry that happens
    to share label text is untouched, falling through to match_field()'s
    own unchanged behavior exactly as before."""
    norm_label = normalize_label(label)
    if norm_label in _ALIAS_INDEX:
        return _ALIAS_INDEX[norm_label], FieldConfidence.EXACT
    for af in application_fields or []:
        af_label = getattr(af, "label", None)
        if af_label and normalize_label(af_label) == norm_label \
                and getattr(af, "value_source", "") == "browser_verified_field_evidence":
            return af.field_id, FieldConfidence.EXACT
    return match_field(label, name)
