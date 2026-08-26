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


def test_greenhouse_provider_strips_html_entity_encoded_content():
    # Real bug caught live during pumpcareers canary prep: a real board's
    # `?content=true` response HTML-entity-encodes its markup
    # (`&lt;h3&gt;...`), not literal tags. `_strip_html` must unescape
    # BEFORE stripping tags, or the entities decode back into raw,
    # un-stripped tags afterward.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 4754286008,
                        "title": "Backend Engineer",
                        "content": "&lt;h3&gt;&lt;strong&gt;About&lt;/strong&gt;&lt;/h3&gt;\n"
                                   "&lt;p&gt;Build APIs &amp; ship fast.&lt;/p&gt;",
                        "absolute_url": "https://job-boards.greenhouse.io/pumpcareers/jobs/4754286008",
                        "updated_at": "2026-08-21T10:00:00Z",
                        "location": {"name": "San Francisco, CA"},
                    }
                ]
            },
        )

    provider = GreenhouseProvider(["pumpcareers"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].description == "About Build APIs & ship fast."
    assert "<" not in jobs[0].description
    assert "&lt;" not in jobs[0].description


def test_greenhouse_provider_isolates_board_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "badboard" in str(request.url):
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "T", "content": "", "location": {"name": "Remote"}}]})

    provider = GreenhouseProvider(["badboard", "goodboard"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].external_job_id == "1"


def test_greenhouse_provider_prefers_first_published_over_updated_at():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": 1, "title": "T", "content": "", "location": {"name": "Remote"},
            "first_published": "2026-01-01T00:00:00Z", "updated_at": "2026-08-21T10:00:00Z",
        }]})

    provider = GreenhouseProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].published_at == "2026-01-01T00:00:00Z"


def test_greenhouse_provider_falls_back_to_updated_at_when_first_published_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": 1, "title": "T", "content": "", "location": {"name": "Remote"},
            "updated_at": "2026-08-21T10:00:00Z",
        }]})

    provider = GreenhouseProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].published_at == "2026-08-21T10:00:00Z"


def test_greenhouse_provider_uses_real_company_name_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": 1, "title": "T", "content": "", "location": {"name": "Remote"},
            "company_name": "GitLab",
        }]})

    provider = GreenhouseProvider(["gitlab"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].company == "GitLab"


def test_greenhouse_provider_falls_back_to_token_when_company_name_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": 1, "title": "T", "content": "", "location": {"name": "Remote"},
        }]})

    provider = GreenhouseProvider(["acme-corp"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].company == "Acme Corp"


def test_greenhouse_provider_extracts_employment_type_from_named_metadata_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": 1, "title": "T", "content": "", "location": {"name": "Remote"},
            "metadata": [
                {"id": 1, "name": "Quota Coverage Type", "value": "Account Executive", "value_type": "single_select"},
                {"id": 2, "name": "Employment Type", "value": "Full-time", "value_type": "single_select"},
            ],
        }]})

    provider = GreenhouseProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].employment_type_raw == "Full-time"


def test_greenhouse_provider_leaves_employment_type_blank_when_no_matching_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": 1, "title": "T", "content": "", "location": {"name": "Remote"},
            "metadata": [{"id": 1, "name": "Quota Coverage Type", "value": "AE", "value_type": "single_select"}],
        }]})

    provider = GreenhouseProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].employment_type_raw == ""


def test_greenhouse_provider_extracts_requisition_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": 1, "title": "T", "content": "", "location": {"name": "Remote"},
            "requisition_id": "6263", "internal_job_id": 6396658002,
        }]})

    provider = GreenhouseProvider(["acme"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].provider_metadata["requisition_id"] == "6263"
    assert jobs[0].provider_metadata["internal_job_id"] == 6396658002


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


def test_lever_provider_includes_lists_and_additional_content_in_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "abc-123", "text": "Python Developer",
            "descriptionPlain": "Welcome to the role.",
            "lists": [
                {"text": "Qualifications", "content": "<li>be smart</li><li>be very smart</li>"},
                {"text": "Duties", "content": "<li>work hard</li>"},
            ],
            "additionalPlain": "Lever is an equal opportunity employer.",
            "categories": {},
        }])

    provider = LeverProvider(["acme"], client=_client_returning(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert "Welcome to the role." in job.description
    assert "Qualifications: be smart be very smart" in job.description
    assert "Duties: work hard" in job.description
    assert "Lever is an equal opportunity employer." in job.description


def test_lever_provider_description_still_works_without_lists_or_additional():
    # Schema-drift resilience: a tenant/response missing these newer fields
    # entirely must not lose the base description or crash normalization.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "abc-123", "text": "Python Developer",
            "descriptionPlain": "Just the basics.", "categories": {},
        }])

    provider = LeverProvider(["acme"], client=_client_returning(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.description == "Just the basics."


def test_lever_provider_extracts_salary_period_from_interval():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "abc-123", "text": "T", "categories": {},
            "salaryRange": {"min": 10000, "max": 125000, "currency": "USD", "interval": "per-year-salary"},
        }])

    provider = LeverProvider(["acme"], client=_client_returning(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_period == "year"
    assert job.salary_currency == "USD"


def test_lever_provider_isolates_company_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "badco" in str(request.url):
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=[{"id": "1", "text": "T", "categories": {}}])

    provider = LeverProvider(["badco", "goodco"], client=_client_returning(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].external_job_id == "1"
