import httpx

from app.providers.workday import WorkdayProvider

BASE = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_workday_normalizes_jobs_with_detail_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {
                "jobDescription": "<p>Build things. Sponsorship available.</p>",
            }})
        body = request.read()
        return httpx.Response(200, json={
            "total": 1,
            "jobPostings": [{
                "title": "Backend Engineer", "externalPath": "/job/Remote/Backend-Engineer_R-1234",
                "locationsText": "Remote - USA", "postedOn": "Posted 3 Days Ago",
                "jobPostingId": "R-1234",
            }],
        })

    provider = WorkdayProvider([BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider == "workday"
    assert job.external_job_id == "R-1234"
    assert job.company == "Acme"
    assert job.published_at is None  # postedOn is relative text, never fabricated as a timestamp
    assert "Sponsorship available" in job.description


def test_workday_pagination_across_offsets():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": ""}})
        import json as _json
        body = _json.loads(request.read())
        offset = body["offset"]
        total = 3
        postings = []
        for i in range(offset, min(offset + 20, total)):
            postings.append({"title": f"Job {i}", "externalPath": f"/job/{i}", "jobPostingId": str(i)})
        return httpx.Response(200, json={"total": total, "jobPostings": postings})

    provider = WorkdayProvider([BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 3


def test_workday_tenant_failure_is_clean_not_fabricated():
    """Scenario G: unsupported/limited Workday tenant -- a fetch failure
    (e.g. bot-protection front-end blocking the request) must fail cleanly
    with no jobs, not raise out of the provider or invent results."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    provider = WorkdayProvider([BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs == []


def test_workday_malformed_response_isolated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    provider = WorkdayProvider([BASE], client=_client(handler))
    assert provider.fetch_jobs(max_jobs=10) == []


def test_workday_isolates_tenant_errors_across_multiple_tenants():
    bad = "https://bad.wd3.myworkdayjobs.com/wday/cxs/bad/External"

    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in str(request.url):
            return httpx.Response(500, text="down")
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": ""}})
        return httpx.Response(200, json={"total": 1, "jobPostings": [
            {"title": "T", "externalPath": "/job/1", "jobPostingId": "1"}
        ]})

    provider = WorkdayProvider([bad, BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1


def test_workday_capabilities_marked_partial():
    assert WorkdayProvider.capabilities.support_level.value == "PARTIAL"
    assert WorkdayProvider.capabilities.structured_published_at_supported is False


def test_workday_job_url_is_real_candidate_page_not_cxs_api_base():
    """Regression for a real bug (live-caught 2026-08-22 against a real
    Workday tenant): the CXS API base (.../wday/cxs/{tenant}/{site}) and the
    candidate-facing application page (.../{ site}) are DIFFERENT paths on
    the same host. Appending externalPath to the API base -- this
    provider's previous behavior -- coincidentally also resolves, but to
    the raw jobPostingInfo JSON detail endpoint, not the real HTML
    application form, which would have silently broken every downstream
    browser-assist/application step for every Workday job."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": ""}})
        return httpx.Response(200, json={
            "total": 1,
            "jobPostings": [{
                "title": "Backend Engineer", "externalPath": "/job/Remote/Backend-Engineer_R-1234",
                "jobPostingId": "R-1234",
            }],
        })

    provider = WorkdayProvider([BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.url == "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Backend-Engineer_R-1234"
    assert job.source_url == job.url
    assert "/wday/cxs/" not in job.url


def test_workday_detail_fields_populate_country_job_req_id_can_apply():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {
                "jobDescription": "<p>Build things.</p>",
                "timeType": "Full time",
                "jobReqId": "R-2619657",
                "country": {"descriptor": "Canada", "id": "abc"},
                "canApply": True,
                "externalUrl": "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Backend-Engineer_R-1234",
            }})
        return httpx.Response(200, json={
            "total": 1,
            "jobPostings": [{
                "title": "Backend Engineer", "externalPath": "/job/Remote/Backend-Engineer_R-1234",
                "jobPostingId": "R-1234",
            }],
        })

    provider = WorkdayProvider([BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.country == "Canada"
    assert job.employment_type_raw == "Full time"
    assert job.provider_metadata["job_req_id"] == "R-2619657"
    assert job.provider_metadata["can_apply"] is True
    # external_job_id stays derived from the list response, never job_req_id
    assert job.external_job_id == "R-1234"
    assert "reported_external_url" not in job.provider_metadata  # matched constructed url, no mismatch


def test_workday_detail_omitting_new_fields_leaves_them_unset():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": ""}})
        return httpx.Response(200, json={
            "total": 1,
            "jobPostings": [{"title": "T", "externalPath": "/job/1", "jobPostingId": "1"}],
        })

    provider = WorkdayProvider([BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    job = jobs[0]
    assert job.country is None
    assert "job_req_id" not in job.provider_metadata
    assert "can_apply" not in job.provider_metadata


def test_workday_external_url_mismatch_is_flagged_not_silently_trusted():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {
                "jobDescription": "",
                "externalUrl": "https://acme.wd5.myworkdayjobs.com/SomeOtherSite/job/Remote/Backend-Engineer_R-1234",
            }})
        return httpx.Response(200, json={
            "total": 1,
            "jobPostings": [{"title": "T", "externalPath": "/job/Remote/Backend-Engineer_R-1234", "jobPostingId": "1"}],
        })

    provider = WorkdayProvider([BASE], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    job = jobs[0]
    # constructed URL stays authoritative (never silently swapped)...
    assert job.url == "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Backend-Engineer_R-1234"
    # ...but the divergence is recorded, never swallowed
    assert job.provider_metadata["reported_external_url"] == (
        "https://acme.wd5.myworkdayjobs.com/SomeOtherSite/job/Remote/Backend-Engineer_R-1234"
    )


def test_workday_candidate_base_falls_back_when_base_url_shape_unrecognized():
    """A base_url that doesn't match the documented CXS shape (.../wday/cxs/
    {tenant}/{site}) is never guessed at -- falls back to the previous
    (API-base) behavior unchanged rather than fabricating a site name."""
    from app.providers.workday import _candidate_base

    assert _candidate_base("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External") == \
        "https://acme.wd5.myworkdayjobs.com/External"
    odd_base = "https://acme.example.com/some/other/shape"
    assert _candidate_base(odd_base) == odd_base
