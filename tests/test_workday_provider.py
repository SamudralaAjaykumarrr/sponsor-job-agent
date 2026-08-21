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
