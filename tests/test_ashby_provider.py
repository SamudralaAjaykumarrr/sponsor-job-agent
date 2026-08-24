import httpx

from app.providers.ashby import AshbyProvider


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ashby_normalizes_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.ashbyhq.com" in str(request.url)
        return httpx.Response(200, json={
            "organizationName": "Acme Corp",
            "jobs": [
                {
                    "id": "job-1",
                    "title": "Backend Software Engineer",
                    "location": "Remote (US)",
                    "isRemote": True,
                    "descriptionPlain": "Build APIs in Python. Visa sponsorship available.",
                    "employmentType": "FullTime",
                    "publishedAt": "2026-08-20T10:00:00Z",
                    "department": "Engineering",
                    "team": "Platform",
                    "applyUrl": "https://jobs.ashbyhq.com/acme/job-1/apply",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/job-1",
                }
            ],
        })

    provider = AshbyProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider == "ashby"
    assert job.company == "Acme Corp"
    assert job.external_job_id == "job-1"
    assert job.remote_status == "remote"
    assert job.department == "Engineering"
    assert job.team == "Platform"
    assert "Visa sponsorship available" in job.description
    assert job.url == "https://jobs.ashbyhq.com/acme/job-1/apply"


def test_ashby_isolates_board_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "badboard" in str(request.url):
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"organizationName": "Good Co", "jobs": [
            {"id": "1", "title": "T", "location": "Remote"}
        ]})

    provider = AshbyProvider(["badboard", "goodboard"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].external_job_id == "1"


def test_ashby_malformed_job_isolated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"organizationName": "Acme", "jobs": [
            {"id": "bad", "title": None, "address": "not-a-dict"},  # malformed
            {"id": "good", "title": "Good Job", "location": "Remote"},
        ]})

    provider = AshbyProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    # "bad" has address as a string not dict -> .get() would crash -> isolated
    ids = {j.external_job_id for j in jobs}
    assert "good" in ids


def test_ashby_respects_max_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"organizationName": "Acme", "jobs": [
            {"id": str(i), "title": f"T{i}", "location": "Remote"} for i in range(5)
        ]})

    provider = AshbyProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=2)
    assert len(jobs) == 2


def test_ashby_salary_picks_the_salary_component_not_the_first_component():
    # Real-world shape (verified live against api.ashbyhq.com): summaryComponents
    # commonly lists a non-salary component (equity/bonus, minValue=None) BEFORE
    # the actual Salary component. Picking index [0] unconditionally used to
    # silently report no salary even when one was genuinely posted.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"organizationName": "Acme", "jobs": [{
            "id": "job-1", "title": "T", "location": "Remote",
            "compensation": {"summaryComponents": [
                {"compensationType": "EquityPercentage", "interval": "NONE", "currencyCode": None,
                 "minValue": None, "maxValue": None},
                {"compensationType": "Bonus", "interval": "1 YEAR", "currencyCode": "EUR",
                 "minValue": None, "maxValue": None},
                {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "EUR",
                 "minValue": 110000, "maxValue": 185000},
            ]},
        }]})

    provider = AshbyProvider(["acme"], client=_client(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_min == 110000
    assert job.salary_max == 185000
    assert job.salary_currency == "EUR"
    assert job.salary_period == "year"


def test_ashby_salary_none_when_no_salary_component_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"organizationName": "Acme", "jobs": [{
            "id": "job-1", "title": "T", "location": "Remote",
            "compensation": {"summaryComponents": [
                {"compensationType": "EquityPercentage", "interval": "NONE", "currencyCode": None,
                 "minValue": None, "maxValue": None},
            ]},
        }]})

    provider = AshbyProvider(["acme"], client=_client(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_min is None
    assert job.salary_max is None


def test_ashby_salary_none_when_compensation_missing_entirely():
    # Schema-drift resilience: most Ashby tenants don't expose compensation at all.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"organizationName": "Acme", "jobs": [
            {"id": "job-1", "title": "T", "location": "Remote"},
        ]})

    provider = AshbyProvider(["acme"], client=_client(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_min is None
    assert job.salary_max is None


def test_ashby_capabilities_are_full():
    assert AshbyProvider.capabilities.support_level.value == "FULL"
    assert AshbyProvider.capabilities.discovery_supported is True
