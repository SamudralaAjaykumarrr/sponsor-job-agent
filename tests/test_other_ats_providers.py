import httpx

from app.providers.bamboohr import BambooHRProvider
from app.providers.breezy import BreezyProvider
from app.providers.comeet import CometProvider, parse_company_token_pairs
from app.providers.recruitee import RecruiteeProvider
from app.providers.unsupported import (
    ICIMSProvider,
    JazzHRProvider,
    JobviteProvider,
    OracleRecruitingProvider,
    PinpointProvider,
    TeamtailorProvider,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- BambooHR ---------------------------------------------------------------

def test_bamboohr_normalizes_jobs_without_description():
    def handler(request):
        return httpx.Response(200, json={"result": [
            {"id": 1, "jobOpeningName": "Backend Engineer", "locationLabel": "Remote",
             "departmentLabel": "Engineering", "employmentStatusLabel": "Full-Time"}
        ]})

    provider = BambooHRProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].description == ""  # no public JD endpoint -- never fabricated
    assert jobs[0].title == "Backend Engineer"


def test_bamboohr_isolates_subdomain_errors():
    def handler(request):
        if "bad" in str(request.url):
            return httpx.Response(500, text="err")
        return httpx.Response(200, json={"result": [{"id": 1, "jobOpeningName": "T"}]})

    provider = BambooHRProvider(["bad", "good"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1


# --- Recruitee ----------------------------------------------------------------

def test_recruitee_normalizes_jobs_with_full_description():
    def handler(request):
        return httpx.Response(200, json={"offers": [
            {"id": 5, "title": "Python Developer", "description": "<p>Build APIs</p>",
             "requirements": "<p>3 years</p>", "location": "Remote", "city": "Remote",
             "country": "United States", "remote": True, "employment_type": "full_time",
             "published_at": "2026-08-10T00:00:00Z", "department": "Engineering",
             "careers_url": "https://acme.recruitee.com/o/python-developer"}
        ]})

    provider = RecruiteeProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.remote_status == "remote"
    assert "Build APIs" in job.description
    assert job.external_job_id == "5"


def test_recruitee_malformed_payload_isolated():
    def handler(request):
        return httpx.Response(200, text="not json")

    provider = RecruiteeProvider(["acme"], client=_client(handler))
    assert provider.fetch_jobs(max_jobs=10) == []


# --- Breezy ---------------------------------------------------------------

def test_breezy_normalizes_jobs():
    def handler(request):
        return httpx.Response(200, json=[
            {"_id": "abc", "name": "SRE", "location": {"name": "Remote", "is_remote": True},
             "description": "<p>Own infra. Sponsorship available.</p>", "type": "Full-Time",
             "published_date": "2026-08-01", "department": "Infra",
             "url": "https://acme.breezy.hr/p/abc"}
        ])

    provider = BreezyProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].remote_status == "remote"
    assert "Sponsorship available" in jobs[0].description


def test_breezy_isolates_subdomain_errors():
    def handler(request):
        if "bad" in str(request.url):
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=[{"_id": "1", "name": "T", "location": {}}])

    provider = BreezyProvider(["bad", "good"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1


# --- Comeet (EXPERIMENTAL) ---------------------------------------------------

def test_comeet_parses_company_token_pairs():
    configs = parse_company_token_pairs(["acme:tok123", "bad-entry-no-colon", "beta:tok456"])
    assert len(configs) == 2
    assert configs[0].company == "acme" and configs[0].token == "tok123"


def test_comeet_fetches_with_configured_token():
    def handler(request):
        assert request.url.params.get("token") == "tok123"
        return httpx.Response(200, json={"positions": [
            {"uid": "u1", "name": "Engineer", "location": {"name": "Remote", "country": "US"},
             "details": "<p>Build things</p>", "url_public_page": "https://comeet.com/jobs/acme/u1"}
        ]})

    provider = CometProvider(["acme:tok123"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].external_job_id == "u1"


def test_comeet_skips_unconfigured_and_isolates_failures():
    def handler(request):
        return httpx.Response(500, text="down")

    provider = CometProvider(["acme:tok123"], client=_client(handler))
    assert provider.fetch_jobs(max_jobs=10) == []


def test_comeet_is_marked_experimental():
    assert CometProvider.capabilities.support_level.value == "EXPERIMENTAL"


# --- Unsupported stubs: never fabricate, always return [] -------------------

def test_unsupported_providers_never_return_jobs():
    for cls in (TeamtailorProvider, JobviteProvider, PinpointProvider, JazzHRProvider,
                ICIMSProvider, OracleRecruitingProvider):
        provider = cls(["some-tenant"])
        assert provider.fetch_jobs(max_jobs=10) == []
        assert cls.capabilities.discovery_supported is False


def test_unsupported_provider_does_not_raise_on_repeated_calls():
    provider = JobviteProvider(["acme"])
    assert provider.fetch_jobs(5) == []
    assert provider.fetch_jobs(5) == []  # warns once, still returns cleanly
