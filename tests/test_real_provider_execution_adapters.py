"""Real Provider Execution V1: the strengthened Greenhouse and Lever
execution adapters.

Every HTTP interaction is an `httpx.MockTransport` against a payload shaped
exactly like each provider's real, documented public read API -- no live
network call, and nothing is ever submitted anywhere.
"""

import httpx
import pytest

from app.applications.providers_greenhouse import (
    FormDiscoveryOutcome,
    GreenhouseApplicationProvider,
)
from app.applications.providers_lever import LeverApplicationProvider
from app.applications.schema import build_application_fields
from app.models import Job

# Trimmed real-shaped Greenhouse payload, modeled on a live
# boards-api.greenhouse.io ?questions=true response.
GREENHOUSE_PAYLOAD = {
    "id": 12345,
    "title": "Backend Software Engineer",
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Last Name", "required": True,
         "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
        {"label": "Email", "required": True,
         "fields": [{"name": "email", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True,
         "fields": [{"name": "resume", "type": "input_file", "values": []}]},
        {"label": "Cover Letter", "required": False,
         "fields": [{"name": "cover_letter", "type": "input_file", "values": []}]},
        {"label": "Will you now or in the future require sponsorship for a visa to remain in your current "
                  "location?", "required": True,
         "fields": [{"name": "question_1", "type": "multi_value_single_select",
                      "values": [{"label": "No", "value": 1}, {"label": "Yes", "value": 2}]}]},
        {"label": "Which internal Acme initiative most closely matches your background?", "required": True,
         "fields": [{"name": "question_99001", "type": "input_text", "values": []}]},
        {"label": "DisabilityStatus", "required": False,
         "fields": [{"name": "disability_status", "type": "multi_value_single_select", "values": [
             {"label": "I do not want to answer", "value": "3"},
             {"label": "No, I do not have a disability and have not had one in the past", "value": "2"},
             {"label": "Yes, I have a disability, or have had one in the past", "value": "1"},
         ]}]},
    ],
}

# Real-shaped Lever per-posting payload -- note it carries hostedUrl/applyUrl
# and genuinely NO question schema of any kind.
LEVER_POSTING_ID = "33538a2f-d27d-4a96-8f05-fa4b0e4d940e"
LEVER_PAYLOAD = {
    "id": LEVER_POSTING_ID,
    "text": "Backend Software Engineer",
    "hostedUrl": f"https://jobs.lever.co/acme/{LEVER_POSTING_ID}",
    "applyUrl": f"https://jobs.lever.co/acme/{LEVER_POSTING_ID}/apply",
    "categories": {"location": "Remote - US", "commitment": "Full-time"},
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _status_client(status: int, body: str = "gone") -> httpx.Client:
    return _client(lambda request: httpx.Response(status, text=body))


def _greenhouse_job(**overrides) -> Job:
    base = dict(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Build APIs.", provider="greenhouse", external_job_id="12345", company_identifier="acme",
    )
    base.update(overrides)
    return Job(**base)


def _lever_job(**overrides) -> Job:
    base = dict(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Build APIs.", provider="lever", external_job_id=LEVER_POSTING_ID,
        company_identifier="acme",
    )
    base.update(overrides)
    return Job(**base)


# =============================================================================
# Greenhouse
# =============================================================================

def test_greenhouse_canonical_identity_from_the_job_row():
    identity = GreenhouseApplicationProvider().canonical_identity(_greenhouse_job())
    assert identity.recognized is True
    assert identity.board_token == "acme"
    assert identity.posting_id == "12345"
    assert identity.canonical_url == "https://boards.greenhouse.io/acme/jobs/12345"


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/gitlab/jobs/7654321",
    "https://job-boards.greenhouse.io/gitlab/jobs/7654321",
])
def test_greenhouse_canonical_identity_parsed_from_both_real_board_host_shapes(url):
    """GitLab's board genuinely migrated from boards.greenhouse.io to
    job-boards.greenhouse.io between phases -- both real shapes must parse."""
    job = _greenhouse_job(external_job_id="", company_identifier="", canonical_url=url, url=url)
    identity = GreenhouseApplicationProvider().canonical_identity(job)
    assert identity.recognized is True
    assert identity.board_token == "gitlab"
    assert identity.posting_id == "7654321"


def test_greenhouse_identity_is_never_fabricated_from_an_unrelated_url():
    job = _greenhouse_job(external_job_id="", company_identifier="",
                           canonical_url="https://careers.example.com/roles/backend", url="")
    identity = GreenhouseApplicationProvider().canonical_identity(job)
    assert identity.recognized is False
    assert identity.board_token == ""
    assert identity.posting_id == ""


def test_greenhouse_form_discovery_uses_the_documented_public_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=GREENHOUSE_PAYLOAD)

    result = GreenhouseApplicationProvider(client=_client(handler)).discover_form_detailed(_greenhouse_job())
    assert result.outcome == FormDiscoveryOutcome.DISCOVERED
    assert "boards-api.greenhouse.io/v1/boards/acme/jobs/12345" in seen["url"]
    assert "questions=true" in seen["url"]
    assert result.form is not None
    assert result.form.fingerprint
    assert {f.name for f in result.form.fields} >= {"first_name", "email", "resume", "question_1"}


@pytest.mark.parametrize("status,expected", [
    (404, FormDiscoveryOutcome.JOB_GONE),
    (410, FormDiscoveryOutcome.JOB_GONE),
    (403, FormDiscoveryOutcome.ACCESS_REFUSED),
    (401, FormDiscoveryOutcome.ACCESS_REFUSED),
])
def test_greenhouse_discovery_distinguishes_permanent_outcomes(status, expected):
    provider = GreenhouseApplicationProvider(client=_status_client(status))
    result = provider.discover_form_detailed(_greenhouse_job())
    assert result.outcome == expected
    assert result.status_code == status
    # The unchanged ApplicationProvider contract still just returns None.
    assert result.form is None


def test_greenhouse_discovery_reports_a_transient_failure_distinctly():
    """A 5xx must never be mistaken for an expired posting."""
    provider = GreenhouseApplicationProvider(client=_status_client(503))
    result = provider.discover_form_detailed(_greenhouse_job())
    assert result.outcome == FormDiscoveryOutcome.TEMPORARY_FAILURE


def test_greenhouse_discovery_reports_a_board_with_no_question_schema():
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json={"id": 1})))
    result = provider.discover_form_detailed(_greenhouse_job())
    assert result.outcome == FormDiscoveryOutcome.NO_QUESTIONS_EXPOSED
    assert result.form is None


def test_greenhouse_expired_posting_is_reported_as_inactive():
    provider = GreenhouseApplicationProvider(client=_status_client(404))
    assert provider.check_job_still_active(_greenhouse_job()) is False
    assert provider.classify_job_inactive_reason(_greenhouse_job()) == "REMOVED"


def test_greenhouse_gone_posting_reports_expired():
    provider = GreenhouseApplicationProvider(client=_status_client(410))
    assert provider.check_job_still_active(_greenhouse_job()) is False
    assert provider.classify_job_inactive_reason(_greenhouse_job()) == "EXPIRED"


@pytest.mark.parametrize("status", [500, 502, 429, 403])
def test_greenhouse_never_reports_inactive_from_a_non_permanent_failure(status):
    """"Not checkable" (None) is the honest answer -- returning False would
    terminate a perfectly live application over one bad moment."""
    provider = GreenhouseApplicationProvider(client=_status_client(status))
    assert provider.check_job_still_active(_greenhouse_job()) is None
    assert provider.classify_job_inactive_reason(_greenhouse_job()) is None


def test_greenhouse_live_posting_is_reported_active():
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    assert provider.check_job_still_active(_greenhouse_job()) is True
    assert provider.classify_job_inactive_reason(_greenhouse_job()) is None


def test_greenhouse_liveness_is_unknown_without_a_canonical_identity():
    provider = GreenhouseApplicationProvider(client=_status_client(404))
    job = _greenhouse_job(external_job_id="", company_identifier="")
    assert provider.check_job_still_active(job) is None


def test_greenhouse_standard_and_custom_known_fields_map_and_fill(sample_profile):
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    job = _greenhouse_job()
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    mapping = provider.map_fields(form, fields)
    draft = provider.fill_draft(form, mapping)

    filled = set(draft.filled_field_ids)
    assert {"first_name", "last_name", "email", "resume"} <= filled
    # The custom-but-KNOWN sponsorship question maps to the candidate's own
    # truthful answer.
    sponsorship = next(m for m in mapping.mapped if m.form_field.name == "question_1")
    assert sponsorship.application_field.field_id == "future_sponsorship_required"
    assert sponsorship.fill_value == "Yes"
    # The demographic question is optional and the candidate's own stated
    # answer ("I do not have a disability") is NOT one of the exact offered
    # choices -- so nothing is filled and nothing is invented. It is also
    # not reported unresolved, because the employer does not require it.
    demographic = next(m for m in mapping.mapped if m.form_field.name == "disability_status")
    assert demographic.fill_value is None
    assert demographic.will_fill is False
    assert "disability_status" not in filled
    assert "disability_status" not in draft.unresolved_field_ids


def test_greenhouse_demographic_question_uses_the_offered_decline_option(sample_profile):
    """When the candidate has stated NO demographic answer, a genuinely
    offered "I do not want to answer" choice is selected -- never a guessed
    demographic value (CLAUDE.md Phase 8 section 11)."""
    profile = sample_profile.model_copy(deep=True)
    profile.standard_answers.disability_status = ""
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    job = _greenhouse_job()
    form = provider.discover_form(job)
    fields = build_application_fields(profile, resume_path="/tmp/resume.pdf")
    mapping = provider.map_fields(form, fields)
    draft = provider.fill_draft(form, mapping)
    demographic = next(m for m in mapping.mapped if m.form_field.name == "disability_status")
    assert demographic.fill_value == "I do not want to answer"
    assert "disability_status" in draft.filled_field_ids


def test_greenhouse_unknown_custom_question_becomes_needs_user_input(sample_profile):
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    job = _greenhouse_job()
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    draft = provider.fill_draft(form, provider.map_fields(form, fields))
    assert "question_99001" in draft.unresolved_field_ids
    validation = provider.validate(job, form, draft)
    assert validation.ok is False
    assert any("question_99001" in d for d in validation.detail)


def test_greenhouse_normalized_form_projects_high_risk_and_unanswered(sample_profile):
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    form = provider.normalized_form(_greenhouse_job(), fields)
    assert form is not None
    assert form.source.value == "PROVIDER_API"
    assert any(f.provider_field_id == "question_99001" for f in form.unanswered_required())
    assert any(f.high_risk for f in form.fields)


def test_greenhouse_validate_is_always_assist_only_even_when_fully_prepared(sample_profile):
    """A fully-resolved draft still never becomes automatable -- there is no
    tested submission interface."""
    payload = {"questions": [
        {"label": "Email", "required": True,
         "fields": [{"name": "email", "type": "input_text", "values": []}]},
    ]}
    provider = GreenhouseApplicationProvider(client=_client(lambda r: httpx.Response(200, json=payload)))
    job = _greenhouse_job()
    form = provider.discover_form(job)
    fields = build_application_fields(sample_profile, resume_path="/tmp/resume.pdf")
    draft = provider.fill_draft(form, provider.map_fields(form, fields))
    validation = provider.validate(job, form, draft)
    assert draft.unresolved_field_ids == []
    assert validation.ok is True
    assert validation.policy.value == "ASSIST_ONLY"
    assert provider.capabilities.submission_supported is False


def test_greenhouse_submit_is_refused_by_the_base_contract():
    provider = GreenhouseApplicationProvider()
    result = provider.submit(_greenhouse_job(), None, None)
    assert result.success is False
    assert result.error_type == "SUBMISSION_INTERFACE_UNSUPPORTED"
    assert provider.verify_confirmation(result).confirmed is False


# =============================================================================
# Lever
# =============================================================================

def test_lever_canonical_identity_from_the_job_row():
    identity = LeverApplicationProvider().canonical_identity(_lever_job())
    assert identity.recognized is True
    assert identity.site == "acme"
    assert identity.posting_id == LEVER_POSTING_ID
    assert identity.canonical_url == f"https://jobs.lever.co/acme/{LEVER_POSTING_ID}"


def test_lever_canonical_identity_parsed_from_a_real_hosted_url():
    job = _lever_job(external_job_id="", company_identifier="",
                      canonical_url=f"https://jobs.lever.co/leverdemo/{LEVER_POSTING_ID}/apply")
    identity = LeverApplicationProvider().canonical_identity(job)
    assert identity.recognized is True
    assert identity.site == "leverdemo"
    assert identity.posting_id == LEVER_POSTING_ID


def test_lever_identity_rejects_a_non_uuid_posting_id():
    """Lever posting ids are genuinely UUIDs -- a numeric placeholder must
    never masquerade as one."""
    identity = LeverApplicationProvider().canonical_identity(_lever_job(external_job_id="12345"))
    assert identity.recognized is False


def test_lever_form_discovery_is_honestly_unsupported():
    """No public Lever interface publishes the field list, so the adapter
    returns None rather than a guessed template."""
    provider = LeverApplicationProvider(client=_client(lambda r: httpx.Response(200, json=LEVER_PAYLOAD)))
    assert provider.discover_form(_lever_job()) is None
    assert provider.capabilities.form_discovery_supported is False


def test_lever_apply_url_prefers_the_published_apply_url():
    provider = LeverApplicationProvider(client=_client(lambda r: httpx.Response(200, json=LEVER_PAYLOAD)))
    assert provider.apply_url(_lever_job()) == LEVER_PAYLOAD["applyUrl"]


def test_lever_apply_url_falls_back_to_the_canonical_url_when_unreachable():
    provider = LeverApplicationProvider(client=_status_client(503))
    assert provider.apply_url(_lever_job()) == f"https://jobs.lever.co/acme/{LEVER_POSTING_ID}"


def test_lever_expired_posting_is_reported_as_inactive():
    provider = LeverApplicationProvider(client=_status_client(404))
    assert provider.check_job_still_active(_lever_job()) is False
    assert provider.classify_job_inactive_reason(_lever_job()) == "REMOVED"


def test_lever_gone_posting_reports_expired():
    provider = LeverApplicationProvider(client=_status_client(410))
    assert provider.check_job_still_active(_lever_job()) is False
    assert provider.classify_job_inactive_reason(_lever_job()) == "EXPIRED"


@pytest.mark.parametrize("status", [500, 502, 429, 403])
def test_lever_never_reports_inactive_from_a_non_permanent_failure(status):
    provider = LeverApplicationProvider(client=_status_client(status))
    assert provider.check_job_still_active(_lever_job()) is None
    assert provider.classify_job_inactive_reason(_lever_job()) is None


def test_lever_live_posting_is_reported_active():
    provider = LeverApplicationProvider(client=_client(lambda r: httpx.Response(200, json=LEVER_PAYLOAD)))
    assert provider.check_job_still_active(_lever_job()) is True


def test_lever_liveness_is_unknown_without_a_confidently_shaped_identity():
    provider = LeverApplicationProvider(client=_status_client(404))
    assert provider.check_job_still_active(_lever_job(external_job_id="not-a-uuid")) is None


def test_lever_validate_and_submit_never_claim_capability():
    provider = LeverApplicationProvider()
    job = _lever_job()
    validation = provider.validate(job, None, None)
    assert validation.ok is False
    assert validation.policy.value == "ASSIST_ONLY"
    submit = provider.submit(job, None, None)
    assert submit.success is False
    assert provider.verify_confirmation(submit).confirmed is False
    assert provider.capabilities.submission_supported is False


def test_lever_still_handles_every_lever_job_for_assist_purposes():
    """Provider SELECTION stays broad (the candidate still gets the apply
    URL) even when the posting id could not be shape-checked -- only the
    API-backed lookups are gated on a canonical identity."""
    provider = LeverApplicationProvider()
    assert provider.detect_application(_lever_job(external_job_id="odd-id")) is True
    assert provider.detect_application(_greenhouse_job()) is False


# =============================================================================
# Cross-provider
# =============================================================================

@pytest.mark.parametrize("provider_cls", [GreenhouseApplicationProvider, LeverApplicationProvider])
def test_neither_adapter_ever_declares_submission_or_confirmation(provider_cls):
    caps = provider_cls.get_capabilities()
    assert caps.submission_supported is False
    assert caps.confirmation_detection_supported is False
    assert caps.confirmation_recheck_supported is False
    assert caps.automation_policy.value == "ASSIST_ONLY"


def test_provider_registry_still_selects_the_dedicated_adapters():
    from app.applications.provider_registry import get_application_provider

    assert get_application_provider(_greenhouse_job()).name == "greenhouse"
    assert get_application_provider(_lever_job()).name == "lever"
