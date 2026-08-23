import httpx

from app.providers.smartrecruiters import SmartRecruitersProvider


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_smartrecruiters_normalizes_jobs_with_detail():
    # Real API shape (live-verified 2026-08-22 against api.smartrecruiters.com):
    # the LIST endpoint's content items never carry `postingUrl`/`applyUrl`/
    # `active` -- only the per-posting DETAIL endpoint does. This fixture
    # intentionally omits them from the list item to guard against
    # regressing to the old bug where url/source_url were silently empty
    # for every real job.
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={
                "jobAd": {"sections": {
                    "jobDescription": {"text": "<p>Build APIs. Visa sponsorship available.</p>"},
                    "qualifications": {"text": "<p>3+ years</p>"},
                }},
                "postingUrl": "https://jobs.smartrecruiters.com/Acme/p1-backend-engineer",
                "active": True,
            })
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json={
                "totalFound": 1,
                "content": [{
                    "id": "p1", "name": "Backend Engineer",
                    "company": {"name": "Acme Corp"},
                    "location": {"city": "Austin", "region": "TX", "country": "us", "remote": True, "hybrid": False},
                    "department": {"label": "Engineering"},
                    "typeOfEmployment": {"label": "Full-time"},
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
    assert job.url == "https://jobs.smartrecruiters.com/Acme/p1-backend-engineer"
    assert job.source_url == job.url
    assert job.provider_metadata["active"] is True


def test_smartrecruiters_hybrid_work_arrangement():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={"jobAd": {"sections": {}}, "postingUrl": "https://x/p1"})
        return httpx.Response(200, json={"totalFound": 1, "content": [{
            "id": "p1", "name": "T", "location": {"city": "NYC", "remote": False, "hybrid": True},
        }]})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].remote_status == "hybrid"


def test_smartrecruiters_compensation_populates_structured_salary():
    """Real detail-endpoint shape (live-verified 2026-08-22 on CERN/
    NBCUniversal3 postings): `compensation` is a genuine, structured field
    -- present on only a minority of postings, absent on most -- never
    fabricated when missing (see the sibling test below)."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={
                "jobAd": {"sections": {}}, "postingUrl": "https://x/p1",
                "compensation": {"min": 95000, "max": 130000, "currency": "USD", "period": "YEARLY"},
            })
        return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "p1", "name": "T", "location": {}}]})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_min == 95000
    assert job.salary_max == 130000
    assert job.salary_currency == "USD"
    assert job.salary_period == "YEARLY"


def test_smartrecruiters_compensation_min_only_never_fabricates_max():
    """A real NBCUniversal3 posting carried `compensation.min` with no
    `max` key at all (USD/YEARLY, comparable) -- salary_max must stay None,
    never guessed/duplicated from min."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={
                "jobAd": {"sections": {}}, "postingUrl": "https://x/p1",
                "compensation": {"min": 55000, "currency": "USD", "period": "YEARLY"},
            })
        return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "p1", "name": "T", "location": {}}]})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_min == 55000
    assert job.salary_max is None


def test_smartrecruiters_non_usd_annual_compensation_never_feeds_the_usd_gate():
    """A real CERN posting carried `{"min": 5929, "currency": "CHF",
    "period": "MONTHLY"}`. app.matching.compensation.evaluate_compensation()
    (used by both app.pipeline and app.applications.eligibility to hard-skip
    a job below MIN_SALARY_USD) has no currency/period conversion anywhere
    in the codebase -- feeding it a raw CHF-monthly number would make a
    perfectly good job look like it pays $5,929/YEAR and get wrongly
    hard-skipped on a pure unit mismatch. salary_min/max must stay unset for
    non-USD/non-annual compensation; the raw figure is still preserved (not
    discarded) in provider_metadata for future currency-aware use."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={
                "jobAd": {"sections": {}}, "postingUrl": "https://x/p1",
                "compensation": {"min": 5929, "currency": "CHF", "period": "MONTHLY"},
            })
        return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "p1", "name": "T", "location": {}}]})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.provider_metadata["raw_compensation"] == {"min": 5929, "currency": "CHF", "period": "MONTHLY"}


def test_smartrecruiters_no_compensation_field_leaves_salary_unset():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={"jobAd": {"sections": {}}, "postingUrl": "https://x/p1"})
        return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "p1", "name": "T", "location": {}}]})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    job = provider.fetch_jobs(max_jobs=10)[0]
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None
    assert job.salary_period is None


def test_smartrecruiters_capabilities_marks_structured_salary_supported():
    assert SmartRecruitersProvider.capabilities.structured_salary_supported is True


def test_smartrecruiters_url_falls_back_to_canonical_shape_when_detail_lacks_it():
    """If the detail response is reachable but genuinely has no postingUrl/
    applyUrl (never observed live, but defensive), the provider falls back
    to SmartRecruiters' own live-verified https://jobs.smartrecruiters.com/
    {company}/{id} redirect shape rather than leaving url empty."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(200, json={"jobAd": {"sections": {}}})
        return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "p1", "name": "T", "location": {}}]})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].url == "https://jobs.smartrecruiters.com/acme/p1"


def test_smartrecruiters_url_empty_when_detail_fetch_fails_entirely():
    """A detail-fetch failure (network error, non-200) must never fabricate
    a URL from a company name alone when there's no confirmed posting_id
    context beyond it -- it still falls back to the same canonical shape
    (posting_id is genuinely known from the list item), never silently
    swallowed to empty."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/postings/p1"):
            return httpx.Response(500, text="unavailable")
        return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "p1", "name": "T", "location": {}}]})

    provider = SmartRecruitersProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].url == "https://jobs.smartrecruiters.com/acme/p1"
    assert jobs[0].description == ""


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
