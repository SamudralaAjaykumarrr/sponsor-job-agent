import httpx

from app.providers.smartrecruiters import SmartRecruitersProvider


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_smartrecruiters_normalizes_jobs_with_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={"jobAd": {"sections": {
                "jobDescription": {"text": "<p>Build APIs. Visa sponsorship available.</p>"},
                "qualifications": {"text": "<p>3+ years</p>"},
            }}})
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json={
                "totalFound": 1,
                "content": [{
                    "id": "p1", "name": "Backend Engineer",
                    "company": {"name": "Acme Corp"},
                    "location": {"city": "Austin", "region": "TX", "country": "us", "remote": True},
                    "department": {"label": "Engineering"},
                    "typeOfEmployment": {"label": "Full-time"},
                    "postingUrl": "https://jobs.smartrecruiters.com/Acme/p1",
                    "releasedDate": "2026-08-15T00:00:00.000Z",
                }],
            })
        return httpx.Response(200, json={"totalFound": 1, "content": []})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider == "smartrecruiters"
    assert job.external_job_id == "p1"
    assert job.remote_status == "remote"
    assert job.company == "Acme Corp"
    assert "Visa sponsorship available" in job.description


def test_smartrecruiters_pagination_across_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/postings/" in url and not url.rstrip("/").endswith("postings"):
            return httpx.Response(200, json={"jobAd": {"sections": {}}})
        offset = int(request.url.params.get("offset", "0"))
        limit = int(request.url.params.get("limit", "100"))
        total = 3
        content = []
        for i in range(offset, min(offset + limit, total)):
            content.append({"id": f"p{i}", "name": f"Job {i}", "location": {}})
        return httpx.Response(200, json={"totalFound": total, "content": content})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 3


def test_smartrecruiters_isolates_company_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "badco" in str(request.url):
            return httpx.Response(503, text="unavailable")
        if "/postings/" in str(request.url) and not str(request.url).rstrip("/").endswith("postings"):
            return httpx.Response(200, json={"jobAd": {"sections": {}}})
        return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "g1", "name": "T", "location": {}}]})

    provider = SmartRecruitersProvider(["badco", "goodco"], client=_client(handler), timeout=1.0)
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].external_job_id == "g1"


def test_smartrecruiters_malformed_payload_isolated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs == []


def test_smartrecruiters_respects_max_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/postings/" in str(request.url) and not str(request.url).rstrip("/").endswith("postings"):
            return httpx.Response(200, json={"jobAd": {"sections": {}}})
        offset = int(request.url.params.get("offset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"totalFound": 10, "content": []})
        content = [{"id": str(i), "name": f"T{i}", "location": {}} for i in range(10)]
        return httpx.Response(200, json={"totalFound": 10, "content": content})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=3)
    assert len(jobs) == 3
