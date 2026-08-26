"""Workday + Ashby Provider Execution V1: AshbyApplicationProvider.

Every payload here is shaped exactly like the real, live-verified
api.ashbyhq.com/posting-api/job-board/ashby response captured during this
build's own development (see app/applications/providers_ashby.py's module
docstring) -- no live network call in the normal test suite."""

import httpx

from app.applications.providers_ashby import AshbyApplicationProvider, canonical_identity
from app.models import Job

JOB_ID = "7458d4e9-da2e-47bd-98cb-adfda43d42b2"
CANDIDATE_URL = f"https://jobs.ashbyhq.com/ashby/{JOB_ID}"
APPLY_URL = f"https://jobs.ashbyhq.com/ashby/{JOB_ID}/application"


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _job(**overrides) -> Job:
    defaults = dict(
        title="Engineering Manager", company="Ashby", location="Remote", description="Build things.",
        provider="ashby", company_identifier="ashby", external_job_id=JOB_ID,
        canonical_url=CANDIDATE_URL, url=CANDIDATE_URL,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _board_payload(*, present: bool = True, apply_url: str = APPLY_URL) -> dict:
    jobs = []
    if present:
        jobs.append({"id": JOB_ID, "title": "Engineering Manager", "applyUrl": apply_url,
                     "jobUrl": CANDIDATE_URL})
    jobs.append({"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "title": "Other Role"})
    return {"organizationName": "Ashby", "jobs": jobs}


def test_canonical_identity_derived_from_job_row():
    identity = canonical_identity(_job())
    assert identity.recognized is True
    assert identity.board_name == "ashby"
    assert identity.job_id == JOB_ID
    assert identity.canonical_url == CANDIDATE_URL


def test_canonical_identity_parsed_from_url_when_row_fields_missing():
    identity = canonical_identity(_job(company_identifier="", external_job_id=""))
    assert identity.recognized is True
    assert identity.board_name == "ashby"
    assert identity.job_id == JOB_ID


def test_canonical_identity_rejects_non_uuid_id():
    identity = canonical_identity(_job(external_job_id="not-a-uuid", canonical_url="", url=""))
    assert identity.recognized is False


def test_canonical_identity_rejects_non_ashby_provider():
    identity = canonical_identity(_job(provider="lever"))
    assert identity.recognized is False


def test_capabilities_never_claim_submission_or_form_discovery():
    caps = AshbyApplicationProvider.get_capabilities()
    assert caps.submission_supported is False
    assert caps.form_discovery_supported is False
    assert caps.automation_policy.value == "ASSIST_ONLY"
    assert caps.support_level.value == "UNSUPPORTED"
    assert caps.live_validated is True


def test_discover_form_always_returns_none():
    provider = AshbyApplicationProvider()
    assert provider.discover_form(_job()) is None


def test_apply_url_prefers_the_apis_own_apply_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.ashbyhq.com/posting-api/job-board/ashby" in str(request.url)
        return httpx.Response(200, json=_board_payload())

    provider = AshbyApplicationProvider(client=_client_returning(handler))
    assert provider.apply_url(_job()) == APPLY_URL


def test_apply_url_falls_back_to_canonical_when_board_fetch_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    provider = AshbyApplicationProvider(client=_client_returning(handler))
    assert provider.apply_url(_job()) == CANDIDATE_URL


def test_check_job_still_active_true_when_listed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_board_payload(present=True))

    provider = AshbyApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is True
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_false_when_absent_from_board():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_board_payload(present=False))

    provider = AshbyApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is False
    assert provider.classify_job_inactive_reason(_job()) == "REMOVED"


def test_check_job_still_active_none_on_fetch_failure():
    """A board-level failure is never mistaken for 'this one job is gone'."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    provider = AshbyApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None
    assert provider.classify_job_inactive_reason(_job()) is None


def test_check_job_still_active_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    provider = AshbyApplicationProvider(client=_client_returning(handler))
    assert provider.check_job_still_active(_job()) is None


def test_check_job_still_active_none_for_unrecognized_identity():
    provider = AshbyApplicationProvider()
    assert provider.check_job_still_active(_job(company_identifier="", external_job_id="", canonical_url="",
                                                 url="")) is None


def test_validate_is_always_assist_only():
    from app.applications.models import DraftResult, MappingResult

    provider = AshbyApplicationProvider()
    result = provider.validate(_job(), None, DraftResult(mapping=MappingResult()))
    assert result.ok is False
    assert result.policy.value == "ASSIST_ONLY"


def test_submit_always_refuses():
    provider = AshbyApplicationProvider()
    result = provider.submit(_job(), None, None)
    assert result.success is False
