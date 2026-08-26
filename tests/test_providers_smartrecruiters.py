"""SmartRecruiters + Workable Provider Execution V1: SmartRecruitersApplicationProvider.

Every payload here is shaped exactly like the real, live-verified
api.smartrecruiters.com response captured during this build's own
development (see app/applications/providers_smartrecruiters.py's module
docstring) -- no live network call in the normal test suite."""

import httpx

from app.applications.providers_smartrecruiters import SmartRecruitersApplicationProvider, canonical_identity
from app.models import Job

CANDIDATE_URL = "https://jobs.smartrecruiters.com/acme/744000143115219-senior-backend-engineer"


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer", company="Acme", location="Remote - US",
        description="Build things.", provider="smartrecruiters", company_identifier="acme",
        external_job_id="744000143115219", canonical_url=CANDIDATE_URL, url=CANDIDATE_URL,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_canonical_identity_from_the_job_row():
    identity = canonical_identity(_job())
    assert identity.recognized is True
    assert identity.company == "acme"
    assert identity.posting_id == "744000143115219"
    assert identity.canonical_url == "https://jobs.smartrecruiters.com/acme/744000143115219"


def test_canonical_identity_parsed_from_a_real_posting_url():
    identity = canonical_identity(_job(company_identifier="", external_job_id=""))
    assert identity.recognized is True
    assert identity.company == "acme"
    assert identity.posting_id == "744000143115219"


def test_canonical_identity_rejects_non_smartrecruiters_provider():
    identity = canonical_identity(_job(provider="greenhouse"))
    assert identity.recognized is False


def test_canonical_identity_rejects_unparseable_url():
    identity = canonical_identity(_job(company_identifier="", external_job_id="",
                                        canonical_url="https://example.com/careers", url=""))
    assert identity.recognized is False


def test_canonical_identity_rejects_non_numeric_posting_id():
    identity = canonical_identity(_job(external_job_id="not-a-number", canonical_url="", url=""))
    assert identity.recognized is False


def test_capabilities_never_claim_submission_or_form_discovery():
    caps = SmartRecruitersApplicationProvider.get_capabilities()
    assert caps.submission_supported is False
    assert caps.form_discovery_supported is False
    assert caps.automation_policy.value == "ASSIST_ONLY"
    assert caps.support_level.value == "UNSUPPORTED"
    assert caps.live_validated is True


def test_discover_form_always_returns_none():
    provider = SmartRecruitersApplicationProvider()
    assert provider.discover_form(_job()) is None


def test_apply_url_prefers_the_published_apply_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"applyUrl": "https://jobs.smartrecruiters.com/acme/744...?oga=true"})

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.apply_url(_job()) == "https://jobs.smartrecruiters.com/acme/744...?oga=true"


def test_apply_url_falls_back_to_the_canonical_url_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.apply_url(_job()) == "https://jobs.smartrecruiters.com/acme/744000143115219"


def test_check_job_still_active_true_when_active():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.smartrecruiters.com/v1/companies/acme/postings/744000143115219" in str(request.url)
        return httpx.Response(200, json={"active": True})

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is True
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_false_when_active_explicitly_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"active": False})

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "CLOSED"


def test_check_job_still_active_true_when_active_absent():
    """A posting that omits `active` entirely still resolves the detail
    endpoint successfully -- never treated as inactive from absence alone."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "744000143115219"})

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is True


def test_check_job_still_active_false_on_404_and_reason_removed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"httpCode": 404, "code": "RESOURCE_NOT_FOUND"})

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "REMOVED"


def test_check_job_still_active_reason_expired_on_410():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": "gone"})

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "EXPIRED"


def test_check_job_still_active_none_on_temporary_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_none_on_refusal_never_treated_as_gone():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    provider = SmartRecruitersApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_none_for_unrecognized_identity():
    provider = SmartRecruitersApplicationProvider()
    assert provider.check_job_still_active(_job(company_identifier="", external_job_id="",
                                                  canonical_url="", url="")) is None


def test_validate_is_always_assist_only():
    provider = SmartRecruitersApplicationProvider()
    job = _job()
    from app.applications.models import DraftResult, MappingResult

    result = provider.validate(job, None, DraftResult(mapping=MappingResult()))
    assert result.ok is False
    assert result.policy.value == "ASSIST_ONLY"


def test_submit_always_refuses():
    provider = SmartRecruitersApplicationProvider()
    result = provider.submit(_job(), None, None)
    assert result.success is False


def test_provider_registry_selects_the_dedicated_adapter():
    from app.applications.provider_registry import get_application_provider

    provider = get_application_provider(_job())
    assert isinstance(provider, SmartRecruitersApplicationProvider)
