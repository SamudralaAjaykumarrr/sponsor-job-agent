import json

import httpx

from app.providers.greenhouse import GreenhouseProvider
from app.providers.lever import LeverProvider


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_greenhouse_provider_normalizes_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "boards-api.greenhouse.io" in str(request.url)
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 12345,
                        "title": "Backend Software Engineer",
                        "content": "<p>Build <b>APIs</b> in Python.</p>",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
                        "updated_at": "2026-08-21T10:00:00Z",
                        "location": {"name": "Remote (US)"},
                    }
                ]
            },
        )

    provider = GreenhouseProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider == "greenhouse"
    assert job.external_job_id == "12345"
    assert job.title == "Backend Software Engineer"
    assert "Build APIs in Python." == job.description
    assert job.location == "Remote (US)"
    assert job.published_at == "2026-08-21T10:00:00Z"


def test_greenhouse_provider_isolates_board_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "badboard" in str(request.url):
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "T", "content": "", "location": {"name": "Remote"}}]})

    provider = GreenhouseProvider(["badboard", "goodboard"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].external_job_id == "1"


def test_greenhouse_provider_respects_max_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jobs": [{"id": i, "title": f"T{i}", "content": "", "location": {"name": "Remote"}} for i in range(5)]},
        )

    provider = GreenhouseProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=2)
    assert len(jobs) == 2


def test_lever_provider_normalizes_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.lever.co" in str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "abc-123",
                    "text": "Python Developer",
                    "descriptionPlain": "Build APIs with Python and Django.",
                    "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                    "createdAt": 1755772800000,
                    "categories": {"location": "Remote, US", "commitment": "Full-time"},
                    "salaryRange": {"min": 90000, "max": 130000},
                }
            ],
        )

    provider = LeverProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider == "lever"
    assert job.external_job_id == "abc-123"
    assert job.title == "Python Developer"
    assert job.employment_type_raw == "Full-time"
    assert job.salary_min == 90000
    assert job.salary_max == 130000
    assert job.published_at is not None


def test_lever_provider_isolates_company_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "badco" in str(request.url):
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=[{"id": "1", "text": "T", "categories": {}}])

    provider = LeverProvider(["badco", "goodco"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].external_job_id == "1"
