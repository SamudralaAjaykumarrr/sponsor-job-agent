"""SmartRecruiters + Workable Provider Execution V1: WorkableApplicationProvider.

Every payload here is shaped exactly like the real, live-verified
apply.workable.com/api/v2 response captured during this build's own
development (see app/applications/providers_workable.py's module docstring)
-- no live network call in the normal test suite."""

import httpx

from app.applications.providers_workable import WorkableApplicationProvider, canonical_identity
from app.models import Job

# Live-verified 2026-08-26: a real Workable posting's own `url` omits the
# account segment entirely -- the job row's stored account is what makes
# identity resolution possible here (see the module docstring).
CANDIDATE_URL = "https://apply.workable.com/j/81F531F5F0"
ACCOUNT_QUALIFIED_URL = "https://apply.workable.com/flosum/j/81F531F5F0/"


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _job(**overrides) -> Job:
    defaults = dict(
        title="Account Executive", company="Flosum", location="Remote - US",
        description="Build things.", provider="workable", company_identifier="flosum",
        external_job_id="81F531F5F0", canonical_url=CANDIDATE_URL, url=CANDIDATE_URL,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_canonical_identity_from_the_job_row():
    identity = canonical_identity(_job())
    assert identity.recognized is True
    assert identity.account == "flosum"
    assert identity.shortcode == "81F531F5F0"
    assert identity.canonical_url == "https://apply.workable.com/flosum/j/81F531F5F0/"


def test_canonical_identity_parsed_from_an_account_qualified_url():
    identity = canonical_identity(
        _job(company_identifier="", external_job_id="", canonical_url=ACCOUNT_QUALIFIED_URL, url="")
    )
    assert identity.recognized is True
    assert identity.account == "flosum"
    assert identity.shortcode == "81F531F5F0"


def test_canonical_identity_rejects_a_shortcode_only_url_with_no_stored_account():
    """The real, commonly-observed URL shape omits the account -- this must
    never be guessed."""
    identity = canonical_identity(_job(company_identifier="", external_job_id="", canonical_url=CANDIDATE_URL, url=""))
    assert identity.recognized is False


def test_canonical_identity_rejects_non_workable_provider():
    identity = canonical_identity(_job(provider="greenhouse"))
    assert identity.recognized is False


def test_canonical_identity_rejects_unparseable_url():
    identity = canonical_identity(_job(company_identifier="", external_job_id="",
                                        canonical_url="https://example.com/careers", url=""))
    assert identity.recognized is False


def test_capabilities_never_claim_submission_or_form_discovery():
    caps = WorkableApplicationProvider.get_capabilities()
    assert caps.submission_supported is False
    assert caps.form_discovery_supported is False
    assert caps.automation_policy.value == "ASSIST_ONLY"
    assert caps.support_level.value == "UNSUPPORTED"
    assert caps.live_validated is True


def test_discover_form_always_returns_none():
    provider = WorkableApplicationProvider()
    assert provider.discover_form(_job()) is None


def test_apply_url_is_the_jobs_own_stored_candidate_facing_url():
    provider = WorkableApplicationProvider()
    assert provider.apply_url(_job()) == CANDIDATE_URL


def test_check_job_still_active_true_when_published():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "apply.workable.com/api/v2/accounts/flosum/jobs/81F531F5F0" in str(request.url)
        return httpx.Response(200, json={"state": "published", "shortcode": "81F531F5F0"})

    provider = WorkableApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is True
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_false_when_state_is_not_published():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "closed"})

    provider = WorkableApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "CLOSED"


def test_check_job_still_active_true_when_state_absent():
    """A response that omits `state` entirely still resolves the detail
    endpoint successfully -- never treated as inactive from absence alone."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"shortcode": "81F531F5F0"})

    provider = WorkableApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is True


def test_check_job_still_active_false_on_404_and_reason_removed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Job not found")

    provider = WorkableApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "REMOVED"


def test_check_job_still_active_reason_expired_on_410():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, text="gone")

    provider = WorkableApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "EXPIRED"


def test_check_job_still_active_none_on_temporary_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    provider = WorkableApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_none_on_refusal_never_treated_as_gone():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    provider = WorkableApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_none_for_unrecognized_identity():
    provider = WorkableApplicationProvider()
    assert provider.check_job_still_active(_job(company_identifier="", external_job_id="",
                                                  canonical_url="", url="")) is None


def test_validate_is_always_assist_only():
    provider = WorkableApplicationProvider()
    job = _job()
    from app.applications.models import DraftResult, MappingResult

    result = provider.validate(job, None, DraftResult(mapping=MappingResult()))
    assert result.ok is False
    assert result.policy.value == "ASSIST_ONLY"


def test_submit_always_refuses():
    provider = WorkableApplicationProvider()
    result = provider.submit(_job(), None, None)
    assert result.success is False


def test_provider_registry_selects_the_dedicated_adapter():
    from app.applications.provider_registry import get_application_provider

    provider = get_application_provider(_job())
    assert isinstance(provider, WorkableApplicationProvider)
