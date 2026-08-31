"""Normalized, provider-neutral application form model (Real Provider
Execution V1).

Before this module there were TWO divergent in-memory representations of "the
fields on a real application form":

  - `app.applications.models.FormSnapshot` / `FormField` -- what a provider
    API adapter (today: Greenhouse's public `?questions=true` Job Board API)
    returns.
  - the raw `dict`s `app.applications.browser_runtime._detect_fields()`
    produces from a real rendered DOM (the only path that reaches Lever,
    Ashby, Workable, ... which expose no public question schema).

Both are genuine, complementary sources -- but nothing downstream could talk
about "the form" without knowing which one it was holding. This module is the
single normalized shape both are projected into, so the pre-submit manifest,
the dashboard, the doctor and any future provider adapter all read one model.

What this module is NOT:
  - It is not a second field-matching engine. Every projection here runs the
    SAME `app.applications.mapping.match_field` + `app.applications.schema
    .find_field` pair every provider adapter and the browser runtime already
    use (CLAUDE.md Phase 8 section 6's "just delegate to the shared engine").
  - It is not a filling policy. `safe_answer_available` REPORTS whether the
    existing rules would allow an automatic answer; it never widens them, and
    no code path fills a field because this module said so.
  - It never invents a field, a choice, a value or a requirement flag that
    its source did not actually expose -- an absent `required` flag stays
    False, an absent label stays "" (CLAUDE.md's standing "never fabricate a
    field a provider doesn't expose" rule).

High-risk classification (the brief's HIGH-RISK QUESTIONS list) is computed
here as an explicit, recorded property rather than by widening
`app.applications.models.SENSITIVE_CATEGORIES` -- widening that frozenset
would silently change what the executor and the browser runtime FILL, which
is a truthfulness-critical behavior this feature has no mandate to alter. A
high-risk field with a genuinely authoritative verified answer in the
candidate profile (e.g. salary expectation, relocation) is still reported
high-risk WITH `authoritatively_known=True`, so a reviewer sees it without
the field being needlessly blocked -- matching the brief's own "where not
already authoritatively known" qualifier.
"""

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Optional

from app.applications.mapping import match_field_with_application_fields
from app.applications.models import (
    ApplicationField,
    FieldCategory,
    FieldConfidence,
    FormSnapshot,
    SENSITIVE_CATEGORIES,
)
from app.applications.schema import find_field


class FormFieldSource(str, Enum):
    """Where this normalized field's structural evidence genuinely came
    from. Never a guess -- a caller always knows which projection it used."""
    PROVIDER_API = "PROVIDER_API"        # a provider's own published question schema
    BROWSER_DOM = "BROWSER_DOM"          # a real rendered page, scanned by browser_runtime
    MOCK_FIXTURE = "MOCK_FIXTURE"        # the deterministic in-process mock ATS


class NormalizedInputType(str, Enum):
    """The INPUT mechanic (how a value is entered), deliberately distinct
    from `semantic_type` (what the question is asking about)."""
    TEXT = "TEXT"
    TEXTAREA = "TEXTAREA"
    SELECT = "SELECT"
    RADIO = "RADIO"
    CHECKBOX = "CHECKBOX"
    FILE = "FILE"
    BOOLEAN = "BOOLEAN"
    UNKNOWN = "UNKNOWN"


class HighRiskClass(str, Enum):
    """The brief's HIGH-RISK QUESTIONS list, as a closed vocabulary. NONE
    means "no elevated risk identified", never "checked and safe to
    fabricate"."""
    NONE = "NONE"
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    SPONSORSHIP = "SPONSORSHIP"
    SALARY_EXPECTATION = "SALARY_EXPECTATION"
    RELOCATION_COMMITMENT = "RELOCATION_COMMITMENT"
    LEGAL_ATTESTATION = "LEGAL_ATTESTATION"
    BACKGROUND_OR_SECURITY = "BACKGROUND_OR_SECURITY"
    VOLUNTARY_DISCLOSURE = "VOLUNTARY_DISCLOSURE"
    CUSTOM_EMPLOYER_QUESTION = "CUSTOM_EMPLOYER_QUESTION"
    CONFLICT_OF_INTEREST = "CONFLICT_OF_INTEREST"
    NON_COMPETE = "NON_COMPETE"
    CERTIFICATION_CLAIM = "CERTIFICATION_CLAIM"
    YEARS_OF_EXPERIENCE_CLAIM = "YEARS_OF_EXPERIENCE_CLAIM"
    SIGNATURE = "SIGNATURE"


# Canonical field ids whose subject matter is inherently high-risk, mapped to
# the specific risk class. Keyed on the canonical id (not free text) so this
# never drifts from `app.applications.schema.build_application_fields`'s own
# vocabulary.
_FIELD_ID_RISK: dict[str, HighRiskClass] = {
    "work_authorization_status": HighRiskClass.WORK_AUTHORIZATION,
    "future_sponsorship_required": HighRiskClass.SPONSORSHIP,
    "sponsorship_type": HighRiskClass.SPONSORSHIP,
    "salary_expectation": HighRiskClass.SALARY_EXPECTATION,
    "willing_to_relocate": HighRiskClass.RELOCATION_COMMITMENT,
    "criminal_history": HighRiskClass.BACKGROUND_OR_SECURITY,
    "security_clearance": HighRiskClass.BACKGROUND_OR_SECURITY,
    "background_check_consent": HighRiskClass.BACKGROUND_OR_SECURITY,
    "drug_testing": HighRiskClass.BACKGROUND_OR_SECURITY,
    "export_control": HighRiskClass.LEGAL_ATTESTATION,
    "government_employment": HighRiskClass.LEGAL_ATTESTATION,
    "non_compete": HighRiskClass.NON_COMPETE,
    "conflict_of_interest": HighRiskClass.CONFLICT_OF_INTEREST,
    "signature": HighRiskClass.SIGNATURE,
    "years_experience": HighRiskClass.YEARS_OF_EXPERIENCE_CLAIM,
}

_CATEGORY_RISK: dict[FieldCategory, HighRiskClass] = {
    FieldCategory.WORK_AUTHORIZATION: HighRiskClass.WORK_AUTHORIZATION,
    FieldCategory.SPONSORSHIP: HighRiskClass.SPONSORSHIP,
    FieldCategory.SALARY: HighRiskClass.SALARY_EXPECTATION,
    FieldCategory.RELOCATION: HighRiskClass.RELOCATION_COMMITMENT,
    FieldCategory.LEGAL_ATTESTATION: HighRiskClass.LEGAL_ATTESTATION,
    FieldCategory.DEMOGRAPHICS: HighRiskClass.VOLUNTARY_DISCLOSURE,
    FieldCategory.VOLUNTARY_DISCLOSURE: HighRiskClass.VOLUNTARY_DISCLOSURE,
    FieldCategory.SIGNATURE: HighRiskClass.SIGNATURE,
    FieldCategory.CUSTOM_TEXT: HighRiskClass.CUSTOM_EMPLOYER_QUESTION,
}

# Label substrings that identify a risk class the canonical field vocabulary
# has no id for at all -- notably a CERTIFICATION claim, which the candidate
# profile deliberately does not model, so it can never be answered from
# verified data and must always reach a human. Kept deliberately narrow and
# specific (never a bare word like "license", which appears in unrelated
# "driver's license" address questions).
_LABEL_RISK: tuple[tuple[str, HighRiskClass], ...] = (
    ("certification", HighRiskClass.CERTIFICATION_CLAIM),
    ("certified in", HighRiskClass.CERTIFICATION_CLAIM),
    ("non-compete", HighRiskClass.NON_COMPETE),
    ("noncompete", HighRiskClass.NON_COMPETE),
    ("conflict of interest", HighRiskClass.CONFLICT_OF_INTEREST),
    ("years of experience", HighRiskClass.YEARS_OF_EXPERIENCE_CLAIM),
)

_BROWSER_TYPE_MAP: dict[str, NormalizedInputType] = {
    "text": NormalizedInputType.TEXT, "email": NormalizedInputType.TEXT,
    "tel": NormalizedInputType.TEXT, "url": NormalizedInputType.TEXT,
    "number": NormalizedInputType.TEXT, "search": NormalizedInputType.TEXT,
    "textarea": NormalizedInputType.TEXTAREA, "select": NormalizedInputType.SELECT,
    "select-one": NormalizedInputType.SELECT, "radio": NormalizedInputType.RADIO,
    "checkbox": NormalizedInputType.CHECKBOX, "file": NormalizedInputType.FILE,
}

_API_TYPE_MAP: dict[str, NormalizedInputType] = {
    "input_text": NormalizedInputType.TEXT, "textarea": NormalizedInputType.TEXTAREA,
    "input_file": NormalizedInputType.FILE, "multi_value_single_select": NormalizedInputType.SELECT,
    "multi_value_multi_select": NormalizedInputType.CHECKBOX, "boolean": NormalizedInputType.BOOLEAN,
    "select": NormalizedInputType.SELECT, "file": NormalizedInputType.FILE, "text": NormalizedInputType.TEXT,
}


@dataclass(frozen=True)
class HighRiskAssessment:
    risk: HighRiskClass = HighRiskClass.NONE
    authoritatively_known: bool = False
    reason: str = ""

    @property
    def high_risk(self) -> bool:
        return self.risk != HighRiskClass.NONE

    @property
    def requires_user_input(self) -> bool:
        """The brief's rule: pause for the user on a high-risk question
        "where not already authoritatively known". A high-risk field the
        candidate has genuinely answered in their verified profile does not
        need a fresh decision; one they haven't always does."""
        return self.high_risk and not self.authoritatively_known


def classify_high_risk(app_field: Optional[ApplicationField], label: str = "") -> HighRiskAssessment:
    """Deterministic, pure classification. `app_field` is the canonical
    ApplicationField this form field mapped to (None when the question is a
    genuinely unrecognized employer-specific one)."""
    text = (label or "").strip().lower()

    if app_field is None:
        # An unmapped free-text question is, by definition, an employer-
        # specific question this project has no verified answer for. Check
        # the narrow label table first so a certification/non-compete
        # question is reported as itself rather than the generic bucket.
        for needle, risk in _LABEL_RISK:
            if needle in text:
                return HighRiskAssessment(risk, False, f"unmapped question whose label mentions '{needle}'")
        return HighRiskAssessment(
            HighRiskClass.CUSTOM_EMPLOYER_QUESTION, False,
            "question did not map to any verified candidate field -- never answered from inference",
        )

    # The canonical field's OWN classification always wins: a field this
    # project models explicitly (e.g. `signature`, whose label happens to
    # read "Signature / Certification") must be reported as what it actually
    # is, not relabelled by an incidental word in its label. A real test
    # caught exactly that collision.
    risk = _FIELD_ID_RISK.get(app_field.field_id) or _CATEGORY_RISK.get(app_field.category, HighRiskClass.NONE)
    if risk == HighRiskClass.NONE:
        # Only when the canonical vocabulary has nothing to say does the
        # narrow label table apply -- notably a CERTIFICATION claim, which
        # the candidate profile models nowhere, so any answer would be
        # fabricated even if the question mapped to something generic.
        for needle, label_risk in _LABEL_RISK:
            if needle in text:
                if label_risk == HighRiskClass.CERTIFICATION_CLAIM:
                    return HighRiskAssessment(
                        label_risk, False, "certification claims are never derivable from the profile",
                    )
                risk = label_risk
                break
    if risk == HighRiskClass.NONE:
        return HighRiskAssessment(HighRiskClass.NONE, True, "")

    known = bool(app_field.verified_value) and not app_field.needs_user_input
    # A SENSITIVE category (demographics/voluntary/legal/signature) is
    # deliberately never treated as "authoritatively known enough to proceed
    # unattended" purely from a stored value -- the existing executor rules
    # already cap its confidence at HIGH for exactly this reason.
    if app_field.category in SENSITIVE_CATEGORIES:
        known = False
    reason = (
        "answer is present in the verified candidate profile"
        if known else "no verified candidate answer -- must never be guessed"
    )
    return HighRiskAssessment(risk, known, reason)


@dataclass
class NormalizedFormField:
    """One field on a real application form, in provider-neutral shape.

    `provider_field_id` is the provider's/DOM's own identifier exactly as
    observed; `canonical_field_id` is this project's own semantic id (from
    `app.applications.mapping.match_field`) or "" when the question genuinely
    did not map to anything verified."""
    provider_field_id: str
    label: str
    input_type: NormalizedInputType
    source: FormFieldSource
    semantic_type: str = ""                 # the canonical FieldCategory value, or ""
    canonical_field_id: str = ""
    required: bool = False
    choices: list[str] = dataclass_field(default_factory=list)
    current_value: Optional[str] = None     # the value that WOULD be submitted, when safely available
    value_source: str = ""                  # e.g. "candidate_profile.contact.email"
    confidence: FieldConfidence = FieldConfidence.LOW
    safe_answer_available: bool = False
    high_risk: bool = False
    high_risk_class: HighRiskClass = HighRiskClass.NONE
    high_risk_reason: str = ""
    needs_user_input: bool = False
    evidence: str = ""                      # short, human-readable provenance note

    def as_dict(self) -> dict:
        return {
            "provider_field_id": self.provider_field_id, "label": self.label,
            "input_type": self.input_type.value, "source": self.source.value,
            "semantic_type": self.semantic_type, "canonical_field_id": self.canonical_field_id,
            "required": self.required, "choices": list(self.choices),
            "current_value": self.current_value, "value_source": self.value_source,
            "confidence": self.confidence.value, "safe_answer_available": self.safe_answer_available,
            "high_risk": self.high_risk, "high_risk_class": self.high_risk_class.value,
            "high_risk_reason": self.high_risk_reason, "needs_user_input": self.needs_user_input,
            "evidence": self.evidence,
        }


@dataclass
class NormalizedForm:
    provider: str
    source: FormFieldSource
    fields: list[NormalizedFormField] = dataclass_field(default_factory=list)
    tenant_identifier: str = ""
    external_job_id: str = ""
    fingerprint: str = ""
    captcha_present: bool = False
    auth_required: bool = False
    mfa_required: bool = False
    total_steps: int = 1

    # --- derived views used by the pre-submit manifest / dashboard --------

    def unanswered_required(self) -> list[NormalizedFormField]:
        return [f for f in self.fields if f.required and not f.safe_answer_available]

    def high_risk_fields(self) -> list[NormalizedFormField]:
        return [f for f in self.fields if f.high_risk]

    def needs_user_input_fields(self) -> list[NormalizedFormField]:
        return [f for f in self.fields if f.needs_user_input]

    def mapped_fields(self) -> list[NormalizedFormField]:
        return [f for f in self.fields if f.canonical_field_id]

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "source": self.source.value,
            "tenant_identifier": self.tenant_identifier, "external_job_id": self.external_job_id,
            "fingerprint": self.fingerprint, "captcha_present": self.captcha_present,
            "auth_required": self.auth_required, "mfa_required": self.mfa_required,
            "total_steps": self.total_steps,
            "field_count": len(self.fields),
            "unanswered_required_count": len(self.unanswered_required()),
            "high_risk_count": len(self.high_risk_fields()),
            "fields": [f.as_dict() for f in self.fields],
        }


def _normalize_one(
    *, provider_field_id: str, label: str, input_type: NormalizedInputType, required: bool,
    choices: list[str], source: FormFieldSource, application_fields: list[ApplicationField],
    evidence: str,
) -> NormalizedFormField:
    canonical_id, confidence = match_field_with_application_fields(label or "", provider_field_id or "", application_fields)
    app_field = find_field(application_fields, canonical_id) if canonical_id else None
    assessment = classify_high_risk(app_field, label)

    current_value: Optional[str] = None
    value_source = ""
    semantic_type = ""
    safe = False
    needs_input = True

    if app_field is not None:
        semantic_type = app_field.category.value
        value_source = app_field.value_source
        needs_input = app_field.needs_user_input
        # `safe_answer_available` REPORTS the existing rules, never a looser
        # notion of "safe": an auto-fill-allowed field, of at least MEDIUM
        # match confidence, with a verified value, that is not itself a
        # sensitive category and whose offered choices (if any) genuinely
        # contain that value.
        safe = (
            app_field.auto_fill_allowed
            and app_field.verified_value is not None
            and confidence != FieldConfidence.LOW
            and app_field.category not in SENSITIVE_CATEGORIES
        )
        if safe and choices and input_type != NormalizedInputType.FILE:
            safe = any(str(app_field.verified_value).strip().lower() == str(c).strip().lower() for c in choices)
        if safe:
            current_value = app_field.verified_value

    return NormalizedFormField(
        provider_field_id=provider_field_id, label=label, input_type=input_type, source=source,
        semantic_type=semantic_type, canonical_field_id=canonical_id or "", required=required,
        choices=list(choices or []), current_value=current_value, value_source=value_source,
        confidence=confidence, safe_answer_available=safe,
        high_risk=assessment.high_risk, high_risk_class=assessment.risk,
        high_risk_reason=assessment.reason, needs_user_input=needs_input, evidence=evidence,
    )


def normalize_form_snapshot(
    snapshot: FormSnapshot, application_fields: list[ApplicationField], *,
    source: FormFieldSource = FormFieldSource.PROVIDER_API,
) -> NormalizedForm:
    """Projects a provider-API `FormSnapshot` (today: Greenhouse's published
    `?questions=true` schema, and the deterministic mock ATS) into the
    normalized model. Every structural fact comes straight from the
    snapshot -- nothing is inferred."""
    evidence = (
        "provider's own published application-question schema"
        if source == FormFieldSource.PROVIDER_API else "deterministic in-process fixture"
    )
    fields = [
        _normalize_one(
            provider_field_id=ff.name or "", label=ff.label or "",
            input_type=_API_TYPE_MAP.get((ff.field_type or "").lower(), NormalizedInputType.UNKNOWN),
            required=bool(ff.required), choices=list(ff.choices or []), source=source,
            application_fields=application_fields, evidence=evidence,
        )
        for ff in snapshot.fields
    ]
    return NormalizedForm(
        provider=snapshot.provider, source=source, fields=fields,
        tenant_identifier=snapshot.tenant_identifier, external_job_id=snapshot.external_job_id,
        fingerprint=snapshot.fingerprint, captcha_present=snapshot.captcha_present,
        auth_required=snapshot.auth_required, mfa_required=snapshot.mfa_required,
        total_steps=snapshot.total_steps,
    )


def normalize_browser_fields(
    raw_fields: list[dict], application_fields: list[ApplicationField], *, provider: str = "",
    fingerprint: str = "", captcha_present: bool = False, auth_required: bool = False,
    mfa_required: bool = False,
) -> NormalizedForm:
    """Projects `app.applications.browser_runtime._detect_fields()`'s raw DOM
    dicts into the SAME normalized model. This is the only path that reaches
    a provider with no published question schema (Lever, Ashby, Workable,
    ...) -- which is precisely why the normalized model must not be
    API-shaped."""
    fields = []
    for rf in raw_fields or []:
        provider_field_id = rf.get("name") or rf.get("id") or ""
        fields.append(_normalize_one(
            provider_field_id=provider_field_id, label=rf.get("label") or "",
            input_type=_BROWSER_TYPE_MAP.get((rf.get("type") or "").lower(), NormalizedInputType.UNKNOWN),
            required=bool(rf.get("required")), choices=list(rf.get("choices") or []),
            source=FormFieldSource.BROWSER_DOM, application_fields=application_fields,
            evidence="observed in the live rendered DOM of the real application page",
        ))
    return NormalizedForm(
        provider=provider, source=FormFieldSource.BROWSER_DOM, fields=fields, fingerprint=fingerprint,
        captcha_present=captcha_present, auth_required=auth_required, mfa_required=mfa_required,
    )
