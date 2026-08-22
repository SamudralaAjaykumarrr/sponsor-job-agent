"""CLAUDE.md Phase 8 section 24: Greenhouse application-form adapter. Uses a
fixture payload shaped exactly like the real, live-verified
boards-api.greenhouse.io ?questions=true response (see
app/applications/providers_greenhouse.py's module docstring) -- no live
network call in the normal test suite."""

import httpx

from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.applications.schema import build_application_fields
from app.models import Job

# Trimmed real-shaped fixture, modeled on a live gitlab.greenhouse.io response
# captured during this phase's own development.
FIXTURE_PAYLOAD = {
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Last Name", "required": True,
         "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
        {"label": "Email", "required": True,
         "fields": [{"name": "email", "type": "input_text", "values": []}]},
        {"label": "Phone", "required": False,
         "fields": [{"name": "phone", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True,
         "fields": [{"name": "resume", "type": "input_file", "values": []},
                    {"name": "resume_text", "type": "textarea", "values": []}]},
        {"label": "Will you now or in the future require sponsorship for a visa to remain in your current location?",
         "required": True,
         "fields": [{"name": "question_1", "type": "multi_value_single_select", "values": [
             {"label": "No", "value": 1}, {"label": "Yes", "value": 2},
         ]}]},
        {"label": "DisabilityStatus", "required": False,
         "fields": [{"name": "disability_status", "type": "multi_value_single_select", "values": [
             {"label": "I do not want to answer", "value": "3"},
             {"label": "No, I do not have a disability and have not had one in the past", "value": "2"},
             {"label": "Yes, I have a disability, or have had one in the past", "value": "1"},
         ]}]},
    ],
}


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _job() -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Build APIs.", provider="greenhouse", external_job_id="12345", company_identifier="acme",
    )


def test_greenhouse_discover_form_uses_real_shaped_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "boards-api.greenhouse.io/v1/boards/acme/jobs/12345" in str(request.url)
        assert request.url.params.get("questions") == "true"
        return httpx.Response(200, json=FIXTURE_PAYLOAD)

    provider = GreenhouseApplicationProvider(client=_client_returning(handler))
    form = provider.discover_form(_job())
    assert form is not None
    assert len(form.fields) == 8  # 7 questions; the Resume/CV question yields 2 fields (file + text)
    assert form.fingerprint


def test_greenhouse_capabilities_never_claim_submission():
    caps = GreenhouseApplicationProvider.get_capabilities()
    assert caps.submission_supported is False
    assert caps.automation_policy.value == "ASSIST_ONLY"
    assert caps.live_validated is True


def test_greenhouse_map_and_fill_resolves_known_fields(sample_profile):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FIXTURE_PAYLOAD)

    provider = GreenhouseApplicationProvider(client=_client_returning(handler))
    job = _job()
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    mapping = provider.map_fields(form, fields)
    draft = provider.fill_draft(form, mapping)

    filled_names = set(draft.filled_field_ids)
    assert "first_name" in filled_names
    assert "email" in filled_names
    assert "resume" in filled_names
    # Sponsorship question resolves truthfully from the candidate's real answer.
    sponsorship_mapped = next(m for m in mapping.mapped if m.form_field.name == "question_1")
    assert sponsorship_mapped.application_field is not None
    assert sponsorship_mapped.application_field.field_id == "future_sponsorship_required"

    validation = provider.validate(job, form, draft)
    assert validation.policy.value == "ASSIST_ONLY"
