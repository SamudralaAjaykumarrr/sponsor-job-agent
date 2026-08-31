"""Browser-Verified Answer Canonical Readiness Integration V1: fast, no-
browser tests for the core evidence/matching/staleness logic and its
integration into the API-schema-driven executor pipeline
(app.applications.providers_greenhouse.map_fields/fill_draft). Real-browser
end-to-end coverage (the record_verified_custom_answer() -> live re-
resolution path) lives in tests/test_browser_verified_evidence_e2e.py.
No test in this file performs any submission or network call."""

import httpx
import pytest

from app.applications import verified_field_evidence as vfe
from app.applications.mapping import match_field_with_application_fields
from app.applications.models import ApplicationField, FieldCategory, FieldConfidence
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.applications.schema import build_application_fields
from app.models import Job

QUESTION_LABEL = "Which internal Acme initiative most closely matches your background?"

GREENHOUSE_PAYLOAD = {
    "id": 12345,
    "title": "Backend Software Engineer",
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Email", "required": True,
         "fields": [{"name": "email", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True,
         "fields": [{"name": "resume", "type": "input_file", "values": []}]},
        {"label": QUESTION_LABEL, "required": True,
         "fields": [{"name": "question_99001", "type": "input_text", "values": []}]},
    ],
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _greenhouse_job(job_id: int = 200, **overrides) -> Job:
    base = dict(
        id=job_id, title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Build APIs.", provider="greenhouse", external_job_id="12345", company_identifier="acme",
        jd_sponsorship_fingerprint="jd-fp-v1",
    )
    base.update(overrides)
    return Job(**base)


def _record(job: Job, *, expected_answer: str = "Platform Modernization", execution_id: str = "exec-1") -> str:
    return vfe.record_verified_answer(
        execution_id=execution_id, job_id=job.id, provider="greenhouse", session_id="sess-1",
        question_label=QUESTION_LABEL, field_type="text", required=True,
        expected_answer=expected_answer, actual_displayed_value=expected_answer,
        structural_form_fingerprint="form-fp-v1", job=job,
    )


# --- 1: a provider-specific browser-verified required answer satisfies
# readiness (map_fields resolves it, fill_draft fills it, validate() no
# longer reports it unresolved). ---

def test_evidence_resolves_previously_unmapped_required_question(tmp_env, sample_profile):
    job = _greenhouse_job()
    _record(job)
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    overrides = vfe.build_application_field_overrides("exec-1", job).fields
    mapping = provider.map_fields(form, fields + overrides)
    draft = provider.fill_draft(form, mapping)

    assert "question_99001" not in draft.unresolved_field_ids
    assert "question_99001" in draft.filled_field_ids
    matched = next(m for m in mapping.mapped if m.form_field.name == "question_99001")
    assert matched.fill_value == "Platform Modernization"
    validation = provider.validate(job, form, draft)
    assert validation.ok is True


# --- 8: required unanswered field remains unresolved (no evidence at all). -

def test_no_evidence_still_leaves_field_unresolved(tmp_env, sample_profile):
    job = _greenhouse_job()
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    mapping = provider.map_fields(form, fields)
    draft = provider.fill_draft(form, mapping)
    assert "question_99001" in draft.unresolved_field_ids
    assert provider.validate(job, form, draft).ok is False


# --- 3: wrong question identity does not satisfy readiness (label mismatch,
# never fuzzy/positional). ---

def test_evidence_for_a_different_question_does_not_resolve_this_one(tmp_env, sample_profile):
    job = _greenhouse_job()
    vfe.record_verified_answer(
        execution_id="exec-1", job_id=job.id, provider="greenhouse", session_id="sess-1",
        question_label="An entirely different question that does not exist on this form",
        field_type="text", required=True, expected_answer="X", actual_displayed_value="X",
        structural_form_fingerprint="form-fp-v1", job=job,
    )
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    overrides = vfe.build_application_field_overrides("exec-1", job).fields
    mapping = provider.map_fields(form, fields + overrides)
    draft = provider.fill_draft(form, mapping)
    assert "question_99001" in draft.unresolved_field_ids


# --- 4: wrong application (different execution_id) does not satisfy
# readiness -- evidence is scoped per execution. ---

def test_evidence_recorded_for_a_different_execution_does_not_apply(tmp_env):
    job = _greenhouse_job()
    _record(job, execution_id="exec-OTHER")
    overrides = vfe.build_application_field_overrides("exec-1", job).fields
    assert overrides == []


# --- 5: form fingerprint drift is tracked but the browser pipeline's OWN
# live re-fill (not this table) is the actual per-field re-verification --
# this test proves the evidence row itself still reports correctly and is
# NOT job/JD-stale merely because the browser-side structural fingerprint
# differs (the table stores it for audit; is_stale() intentionally keys off
# job identity + JD content, the same durable signals app.applications.
# approval/resume_integrity already use, not the browser DOM fingerprint,
# which legitimately changes on ordinary conditional-question re-renders). -

def test_structural_form_fingerprint_alone_does_not_gate_staleness(tmp_env):
    job = _greenhouse_job()
    _record(job)
    row = vfe.list_evidence_for_execution("exec-1")[0]
    assert row["structural_form_fingerprint"] == "form-fp-v1"
    assert vfe.is_stale(row, job) is False


# --- 6: changed expected answer (a fresh, different verified answer)
# invalidates the OLD evidence via "latest wins", never blends the two. ---

def test_re_verifying_with_a_different_answer_supersedes_the_old_row(tmp_env):
    job = _greenhouse_job()
    _record(job, expected_answer="Platform Modernization")
    _record(job, expected_answer="Growth Engineering")
    overrides = vfe.build_application_field_overrides("exec-1", job).fields
    assert len(overrides) == 1
    assert overrides[0].verified_value == "Growth Engineering"
    history = vfe.list_evidence_for_execution("exec-1")
    assert len(history) == 2  # append-only -- the old row is never deleted


# --- job identity / JD-content drift invalidates evidence -----------------

def test_job_identity_change_invalidates_evidence(tmp_env):
    job = _greenhouse_job()
    _record(job)
    different_job = _greenhouse_job(external_job_id="99999")
    overrides = vfe.build_application_field_overrides("exec-1", different_job).fields
    assert overrides == []


def test_jd_content_change_invalidates_evidence(tmp_env):
    job = _greenhouse_job()
    _record(job)
    changed_job = _greenhouse_job(jd_sponsorship_fingerprint="jd-fp-v2-materially-different")
    overrides = vfe.build_application_field_overrides("exec-1", changed_job).fields
    assert overrides == []


# --- 7: optional blank remains valid when policy/user instruction permits
# blank (unaffected by this feature entirely -- no evidence, optional field,
# never reported unresolved). ---

def test_optional_field_without_evidence_is_never_reported_unresolved(tmp_env, sample_profile):
    payload = {**GREENHOUSE_PAYLOAD, "questions": GREENHOUSE_PAYLOAD["questions"] + [
        {"label": "Optional custom question nobody answered", "required": False,
         "fields": [{"name": "question_optional", "type": "input_text", "values": []}]},
    ]}
    job = _greenhouse_job()
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=payload)))
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    mapping = provider.map_fields(form, fields)
    draft = provider.fill_draft(form, mapping)
    assert "question_optional" not in draft.unresolved_field_ids
    assert "question_optional" not in draft.filled_field_ids


# --- match_field_with_application_fields: exact-label-only, never fuzzy ---

def test_match_field_with_application_fields_requires_exact_label():
    af = ApplicationField(
        field_id="verified:abc", label="Have you ever worked for Acme?", category=FieldCategory.CUSTOM_TEXT,
        normalized_type="select", required=True, verified_value="No", confidence=FieldConfidence.EXACT,
        auto_fill_allowed=True,
    )
    assert match_field_with_application_fields("Have you ever worked for Acme?", "", [af]) == \
        ("verified:abc", FieldConfidence.EXACT)
    # A materially different label must NOT match, even if similar.
    assert match_field_with_application_fields("Have you ever worked for Acme Corp before?", "", [af]) == \
        (None, FieldConfidence.LOW)


def test_match_field_with_application_fields_never_overrides_a_real_alias():
    af = ApplicationField(
        field_id="verified:zzz", label="Email", category=FieldCategory.CUSTOM_TEXT,
        normalized_type="text", required=True, verified_value="wrong@example.com",
        confidence=FieldConfidence.EXACT, auto_fill_allowed=True,
    )
    field_id, confidence = match_field_with_application_fields("Email", "", [af])
    assert field_id == "email"  # the real canonical alias always wins
    assert confidence == FieldConfidence.EXACT


def test_match_field_with_application_fields_backward_compatible_with_no_overrides():
    assert match_field_with_application_fields("Email", "", []) == ("email", FieldConfidence.EXACT)
    assert match_field_with_application_fields("Some totally unknown question", "", []) == (None, FieldConfidence.LOW)
