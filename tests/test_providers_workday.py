"""Workday + Ashby Provider Execution V1: WorkdayApplicationProvider.

Every payload here is shaped exactly like the real, live-verified
walmart.wd504.myworkdayjobs.com response captured during this build's own
development (see app/applications/providers_workday.py's module docstring)
-- no live network call in the normal test suite."""

import httpx

from app.applications.providers_workday import WorkdayApplicationProvider, canonical_identity
from app.models import Job

CANDIDATE_URL = (
    "https://walmart.wd504.myworkdayjobs.com/WalmartExternal"
    "/job/Sherbrooke-QC/Associ-des-rayons-de-la-mode_R-2623121"
)


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer", company="Walmart", location="Sherbrooke, QC",
        description="Build things.", provider="workday", company_identifier="walmart",
        external_job_id="R-2623121", canonical_url=CANDIDATE_URL, url=CANDIDATE_URL,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_canonical_identity_derived_from_real_workday_url():
    identity = canonical_identity(_job())
    assert identity.recognized is True
    assert identity.tenant == "walmart"
    assert identity.site == "WalmartExternal"
    assert identity.host == "walmart.wd504.myworkdayjobs.com"
    assert identity.requisition_id == "R-2623121"
    assert identity.external_path == "/job/Sherbrooke-QC/Associ-des-rayons-de-la-mode_R-2623121"


def test_canonical_identity_rejects_non_workday_provider():
    identity = canonical_identity(_job(provider="greenhouse"))
    assert identity.recognized is False


def test_canonical_identity_rejects_unparseable_url():
    identity = canonical_identity(_job(canonical_url="https://example.com/careers", url=""))
    assert identity.recognized is False


def test_capabilities_never_claim_submission_or_form_discovery():
    caps = WorkdayApplicationProvider.get_capabilities()
    assert caps.submission_supported is False
    assert caps.form_discovery_supported is False
    assert caps.automation_policy.value == "ASSIST_ONLY"
    assert caps.support_level.value == "UNSUPPORTED"
    assert caps.live_validated is True


def test_discover_form_always_returns_none():
    provider = WorkdayApplicationProvider()
    assert provider.discover_form(_job()) is None


def test_apply_url_is_the_real_candidate_facing_page():
    provider = WorkdayApplicationProvider()
    assert provider.apply_url(_job()) == CANDIDATE_URL


def test_check_job_still_active_true_when_can_apply():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "walmart.wd504.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/job/" in str(request.url)
        return httpx.Response(200, json={"jobPostingInfo": {"canApply": True, "jobReqId": "R-2623121"}})

    provider = WorkdayApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is True
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_false_when_can_apply_explicitly_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobPostingInfo": {"canApply": False}})

    provider = WorkdayApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "CLOSED"


def test_check_job_still_active_true_when_can_apply_absent():
    """A tenant that omits canApply entirely still resolves the detail
    endpoint successfully -- never treated as inactive from absence alone."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobPostingInfo": {"jobReqId": "R-2623121"}})

    provider = WorkdayApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is True


def test_check_job_still_active_false_on_404_and_reason_removed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    provider = WorkdayApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "REMOVED"


def test_check_job_still_active_reason_expired_on_410():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": "gone"})

    provider = WorkdayApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "EXPIRED"


def test_check_job_still_active_none_on_temporary_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    provider = WorkdayApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_none_on_refusal_never_treated_as_gone():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    provider = WorkdayApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_none_for_unrecognized_identity():
    provider = WorkdayApplicationProvider()
    assert provider.check_job_still_active(_job(canonical_url="", url="")) is None


def test_validate_is_always_assist_only():
    provider = WorkdayApplicationProvider()
    job = _job()
    from app.applications.models import DraftResult, MappingResult

    result = provider.validate(job, None, DraftResult(mapping=MappingResult()))
    assert result.ok is False
    assert result.policy.value == "ASSIST_ONLY"


def test_submit_always_refuses():
    provider = WorkdayApplicationProvider()
    result = provider.submit(_job(), None, None)
    assert result.success is False
