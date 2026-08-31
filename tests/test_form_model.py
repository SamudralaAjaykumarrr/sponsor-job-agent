"""Real Provider Execution V1: the normalized, provider-neutral application
FORM MODEL and its high-risk classification.

Every case here is pure/deterministic -- no browser, no network."""

import pytest

from app.applications.form_model import (
    FormFieldSource,
    HighRiskClass,
    NormalizedInputType,
    classify_high_risk,
    normalize_browser_fields,
    normalize_form_snapshot,
)
from app.applications.models import FormField, FormSnapshot
from app.applications.schema import build_application_fields, find_field


@pytest.fixture
def fields(sample_profile):
    return build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")


def _greenhouse_snapshot() -> FormSnapshot:
    """Shaped exactly like the real boards-api.greenhouse.io ?questions=true
    payload (see tests/test_applications_providers_greenhouse.py)."""
    return FormSnapshot(
        provider="greenhouse", tenant_identifier="acme", external_job_id="12345",
        fingerprint="fp-greenhouse", fields=[
            FormField(name="first_name", label="First Name", field_type="input_text", required=True),
            FormField(name="last_name", label="Last Name", field_type="input_text", required=True),
            FormField(name="email", label="Email", field_type="input_text", required=True),
            FormField(name="resume", label="Resume/CV", field_type="input_file", required=True),
            FormField(name="question_1", label="Will you now or in the future require sponsorship?",
                       field_type="multi_value_single_select", required=True, choices=["Yes", "No"]),
            FormField(name="question_9", label="Which internal Acme initiative matches your background?",
                       field_type="input_text", required=True),
            FormField(name="disability_status", label="Disability Status",
                       field_type="multi_value_single_select", required=False,
                       choices=["Yes, I have a disability", "No, I do not have a disability",
                                "I do not want to answer"]),
        ],
    )


def _lever_dom_fields() -> list[dict]:
    """Shaped like Lever's real rendered DOM -- `urls[...]`/`cards[...]`
    input names, which is exactly why matching must key on LABEL text."""
    return [
        {"index": 0, "name": "name", "id": "name", "label": "Full name", "type": "text", "required": True,
         "choices": []},
        {"index": 1, "name": "email", "id": "email", "label": "Email", "type": "text", "required": True,
         "choices": []},
        {"index": 2, "name": "urls[LinkedIn]", "id": "linkedin", "label": "LinkedIn URL", "type": "text",
         "required": False, "choices": []},
        {"index": 3, "name": "resume", "id": "resume", "label": "Resume", "type": "file", "required": True,
         "choices": []},
        {"index": 4, "name": "cards[a1b2c3][field0]", "id": "",
         "label": "Will you now or in the future require sponsorship?", "type": "radio", "required": True,
         "choices": ["Yes", "No"]},
        {"index": 5, "name": "cards[a1b2c3][field9]", "id": "lever_unknown",
         "label": "Describe a time you disagreed with our published engineering values.",
         "type": "textarea", "required": True, "choices": []},
    ]


# --- projection ---------------------------------------------------------------

def test_provider_api_snapshot_projects_into_the_normalized_model(fields):
    form = normalize_form_snapshot(_greenhouse_snapshot(), fields)
    assert form.source == FormFieldSource.PROVIDER_API
    assert form.provider == "greenhouse"
    assert form.fingerprint == "fp-greenhouse"
    assert len(form.fields) == 7
    by_id = {f.provider_field_id: f for f in form.fields}
    assert by_id["email"].input_type == NormalizedInputType.TEXT
    assert by_id["resume"].input_type == NormalizedInputType.FILE
    assert by_id["question_1"].input_type == NormalizedInputType.SELECT
    assert by_id["email"].canonical_field_id == "email"
    assert by_id["email"].current_value == "test.candidate@example.com"
    assert by_id["email"].value_source == "contact.email"
    assert "published application-question schema" in by_id["email"].evidence


def test_browser_dom_fields_project_into_the_same_normalized_model(fields):
    form = normalize_browser_fields(_lever_dom_fields(), fields, provider="lever", fingerprint="fp-lever")
    assert form.source == FormFieldSource.BROWSER_DOM
    by_id = {f.provider_field_id: f for f in form.fields}
    assert by_id["name"].canonical_field_id == "full_name"
    assert by_id["urls[LinkedIn]"].canonical_field_id == "linkedin_url"
    assert by_id["resume"].input_type == NormalizedInputType.FILE
    assert by_id["cards[a1b2c3][field0]"].input_type == NormalizedInputType.RADIO
    assert "live rendered DOM" in by_id["name"].evidence


def test_both_sources_agree_on_the_same_semantic_question(fields):
    """A sponsorship question reached through Greenhouse's API and through
    Lever's DOM must normalize to the same canonical field."""
    api = normalize_form_snapshot(_greenhouse_snapshot(), fields)
    dom = normalize_browser_fields(_lever_dom_fields(), fields, provider="lever")
    api_sponsorship = next(f for f in api.fields if f.provider_field_id == "question_1")
    dom_sponsorship = next(f for f in dom.fields if f.provider_field_id == "cards[a1b2c3][field0]")
    assert api_sponsorship.canonical_field_id == dom_sponsorship.canonical_field_id == "future_sponsorship_required"
    assert api_sponsorship.semantic_type == dom_sponsorship.semantic_type == "SPONSORSHIP"


def test_normalized_field_never_invents_a_requirement_or_choice(fields):
    snapshot = FormSnapshot(provider="greenhouse", tenant_identifier="acme", external_job_id="1", fields=[
        FormField(name="mystery", label="", field_type="input_text", required=False),
    ])
    form = normalize_form_snapshot(snapshot, fields)
    only = form.fields[0]
    assert only.required is False
    assert only.choices == []
    assert only.label == ""
    assert only.current_value is None


# --- safe answer availability -------------------------------------------------

def test_unmapped_employer_question_is_never_safely_answerable(fields):
    form = normalize_form_snapshot(_greenhouse_snapshot(), fields)
    unknown = next(f for f in form.fields if f.provider_field_id == "question_9")
    assert unknown.canonical_field_id == ""
    assert unknown.safe_answer_available is False
    assert unknown.current_value is None
    assert unknown.needs_user_input is True
    assert unknown in form.unanswered_required()


def test_demographic_question_is_never_auto_answered_by_the_model(fields):
    form = normalize_form_snapshot(_greenhouse_snapshot(), fields)
    demo = next(f for f in form.fields if f.provider_field_id == "disability_status")
    assert demo.safe_answer_available is False
    assert demo.current_value is None
    assert demo.high_risk is True
    assert demo.high_risk_class == HighRiskClass.VOLUNTARY_DISCLOSURE


def test_choice_field_whose_verified_value_is_not_offered_is_not_safely_answerable(sample_profile):
    """The candidate's real answer must genuinely be one of the offered
    choices -- otherwise no safe automatic answer exists."""
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    snapshot = FormSnapshot(provider="greenhouse", tenant_identifier="acme", external_job_id="1", fields=[
        FormField(name="q", label="Will you now or in the future require sponsorship?",
                   field_type="multi_value_single_select", required=True,
                   choices=["Absolutely not", "Maybe later"]),
    ])
    form = normalize_form_snapshot(snapshot, fields)
    assert find_field(fields, "future_sponsorship_required").verified_value == "Yes"
    assert form.fields[0].safe_answer_available is False


def test_sponsorship_answer_is_used_truthfully_when_offered(sample_profile):
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    snapshot = FormSnapshot(provider="greenhouse", tenant_identifier="acme", external_job_id="1", fields=[
        FormField(name="q", label="Will you now or in the future require sponsorship?",
                   field_type="multi_value_single_select", required=True, choices=["Yes", "No"]),
    ])
    form = normalize_form_snapshot(snapshot, fields)
    # The fixture candidate genuinely requires sponsorship -- the model must
    # never report the more "convenient" answer.
    assert form.fields[0].current_value == "Yes"
    assert form.fields[0].safe_answer_available is True


# --- high-risk classification -------------------------------------------------

@pytest.mark.parametrize("field_id,expected", [
    ("future_sponsorship_required", HighRiskClass.SPONSORSHIP),
    ("work_authorization_status", HighRiskClass.WORK_AUTHORIZATION),
    ("salary_expectation", HighRiskClass.SALARY_EXPECTATION),
    ("willing_to_relocate", HighRiskClass.RELOCATION_COMMITMENT),
    ("criminal_history", HighRiskClass.BACKGROUND_OR_SECURITY),
    ("security_clearance", HighRiskClass.BACKGROUND_OR_SECURITY),
    ("non_compete", HighRiskClass.NON_COMPETE),
    ("conflict_of_interest", HighRiskClass.CONFLICT_OF_INTEREST),
    ("signature", HighRiskClass.SIGNATURE),
    ("veteran_status", HighRiskClass.VOLUNTARY_DISCLOSURE),
    ("years_experience", HighRiskClass.YEARS_OF_EXPERIENCE_CLAIM),
])
def test_every_brief_named_high_risk_category_is_classified(fields, field_id, expected):
    app_field = find_field(fields, field_id)
    assert app_field is not None
    assert classify_high_risk(app_field, app_field.label).risk == expected


def test_ordinary_contact_field_is_not_high_risk(fields):
    assessment = classify_high_risk(find_field(fields, "email"), "Email")
    assert assessment.high_risk is False
    assert assessment.requires_user_input is False


def test_unmapped_question_is_high_risk_as_a_custom_employer_question():
    assessment = classify_high_risk(None, "What is your favourite Acme product line?")
    assert assessment.risk == HighRiskClass.CUSTOM_EMPLOYER_QUESTION
    assert assessment.requires_user_input is True


def test_certification_question_is_never_answerable_from_the_profile(fields):
    """The candidate profile models no certifications at all, so any answer
    would be fabricated -- high-risk regardless of what it mapped to."""
    assessment = classify_high_risk(None, "List any AWS certification you hold")
    assert assessment.risk == HighRiskClass.CERTIFICATION_CLAIM
    assert assessment.authoritatively_known is False


def test_high_risk_with_a_verified_profile_answer_does_not_require_fresh_input(fields):
    """The brief pauses on a high-risk question "where not already
    authoritatively known" -- salary IS known for this candidate."""
    assessment = classify_high_risk(find_field(fields, "salary_expectation"), "Desired Salary")
    assert assessment.risk == HighRiskClass.SALARY_EXPECTATION
    assert assessment.authoritatively_known is True
    assert assessment.requires_user_input is False


def test_legal_attestation_never_counts_as_authoritatively_known(fields):
    assessment = classify_high_risk(find_field(fields, "criminal_history"), "Criminal History Disclosure")
    assert assessment.authoritatively_known is False
    assert assessment.requires_user_input is True


def test_high_risk_classification_does_not_widen_sensitive_categories():
    """A guard against a tempting shortcut: this feature must not have
    widened SENSITIVE_CATEGORIES, which controls what actually gets
    FILLED."""
    from app.applications.models import FieldCategory, SENSITIVE_CATEGORIES

    assert SENSITIVE_CATEGORIES == frozenset({
        FieldCategory.DEMOGRAPHICS, FieldCategory.VOLUNTARY_DISCLOSURE,
        FieldCategory.LEGAL_ATTESTATION, FieldCategory.SIGNATURE,
    })


# --- derived views ------------------------------------------------------------

def test_derived_views_report_unanswered_required_and_high_risk(fields):
    form = normalize_form_snapshot(_greenhouse_snapshot(), fields)
    labels = {f.provider_field_id for f in form.unanswered_required()}
    assert "question_9" in labels
    assert "email" not in labels
    assert any(f.high_risk for f in form.fields)
    d = form.as_dict()
    assert d["field_count"] == 7
    assert d["unanswered_required_count"] == len(form.unanswered_required())


# --- real live bug (2026-08-30): safe_answer_available must mirror
# browser_runtime._fill_pass's own evidence carve-out, or a downstream
# read-only check (greenhouse_submit_contract's required_fields_complete
# step) disagrees with the browser session's own already-correct readiness
# for the identical execution. ---

def test_generic_sensitive_field_never_safe(fields):
    """A SENSITIVE_CATEGORIES field with only a generic, profile-derived
    mapping stays never-safe, unchanged -- disability_status here has no
    genuine record_verified_custom_answer evidence at all."""
    form = normalize_form_snapshot(_greenhouse_snapshot(), fields)
    disability = next(f for f in form.fields if f.provider_field_id == "disability_status")
    assert disability.semantic_type == "DEMOGRAPHICS"
    assert disability.safe_answer_available is False


def test_evidence_backed_sensitive_field_is_safe(fields):
    """A SENSITIVE_CATEGORIES field carrying GENUINE, individually-verified
    evidence (value_source == "browser_verified_field_evidence", the marker
    set only by record_verified_custom_answer's own live read-back check)
    IS safe -- mirroring _fill_pass's own resolution exactly, so this
    module's report never disagrees with what the browser session itself
    already correctly resolved for the same field. Uses a label with no
    canonical `_ALIAS_INDEX` entry (like the real live case this fix was
    caught against -- "disability status" DOES have one, and an EXACT
    canonical alias always wins over evidence by design, so that label
    would not exercise this path)."""
    from app.applications.models import ApplicationField, FieldCategory, FieldConfidence

    snapshot = FormSnapshot(
        provider="greenhouse", tenant_identifier="acme", external_job_id="12345",
        fingerprint="fp-gender", fields=[
            FormField(name="q_gender", label="What is your gender identity?",
                      field_type="multi_value_single_select", required=True,
                      choices=["Cisgender man", "Cisgender woman"]),
        ],
    )
    evidence_field = ApplicationField(
        field_id="verified:abc123", label="What is your gender identity?", category=FieldCategory.DEMOGRAPHICS,
        normalized_type="select", required=True, choices=[],
        value_source="browser_verified_field_evidence",
        verified_value="Cisgender man", confidence=FieldConfidence.EXACT,
        needs_user_input=False, sensitive=True, auto_fill_allowed=True,
    )
    form = normalize_form_snapshot(snapshot, fields + [evidence_field])
    gender = next(f for f in form.fields if f.provider_field_id == "q_gender")
    assert gender.semantic_type == "DEMOGRAPHICS"
    assert gender.safe_answer_available is True
    assert gender.current_value == "Cisgender man"
    assert form.unanswered_required() == []
