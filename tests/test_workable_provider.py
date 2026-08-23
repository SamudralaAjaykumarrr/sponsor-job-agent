import httpx

from app.providers.workable import WORKABLE_DETAIL_URL, WorkableProvider


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_workable_normalizes_jobs_with_detail_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/jobs/" in url:
            return httpx.Response(200, json={
                "description": "<p>Build APIs. Visa sponsorship available.</p>",
                "requirements": "<p>3+ years Python</p>",
                "benefits": "",
            })
        if request.url.params.get("page") == "1":
            # Real widget API shape (live-verified 2026-08-22 against
            # apply.workable.com/api/v1/widget/accounts/flosum): the boolean
            # is `telecommuting`, and `application_url` (the direct apply
            # form) is distinct from `url` (the listing page).
            return httpx.Response(200, json={"name": "Acme", "jobs": [
                {"title": "Backend Engineer", "shortcode": "ABC123", "employment_type": "full",
                 "telecommuting": True, "city": "Remote", "country": "United States",
                 "published_on": "2026-08-01", "department": "Engineering",
                 "url": "https://apply.workable.com/acme/j/ABC123/",
                 "application_url": "https://apply.workable.com/acme/j/ABC123/apply"}
            ]})
        return httpx.Response(200, json={"jobs": []})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider == "workable"
    assert job.external_job_id == "ABC123"
    assert job.remote_status == "remote"
    assert "Visa sponsorship available" in job.description
    assert job.employment_type_raw == "full"
    assert job.url == "https://apply.workable.com/acme/j/ABC123/apply"


def test_workable_remote_status_via_telecommuting_field():
    """Regression guard: the real field is `telecommuting`, not
    `telecommute` -- a fixture using the wrong key previously hid this bug
    (structured remote detection silently never fired on real data)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={"description": "", "requirements": "", "benefits": ""})
        return httpx.Response(200, json={"jobs": [
            {"title": "Remote Job", "shortcode": "R1", "telecommuting": True, "city": "Austin"},
        ]})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].remote_status == "remote"


def test_workable_detail_url_is_the_working_v2_shape_not_the_broken_v1_widget_one():
    """Regression guard: the v1/widget detail URL 404s for every real
    account/shortcode (live-verified 2026-08-22) -- description/
    requirements/benefits would have silently stayed empty for every real
    Workable job forever, caught only because _fetch_detail's own except
    swallowed the failure with no visible symptom. Must stay the v2 shape."""
    assert WORKABLE_DETAIL_URL == "https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}"
    assert "widget" not in WORKABLE_DETAIL_URL


def test_workable_workplace_field_preferred_over_telecommuting_for_remote_status():
    """Real v2 detail endpoint field (live-verified 2026-08-22, flosum):
    `workplace` is Workable's own structured work-arrangement enum and
    should win over the coarser list-level `telecommuting` boolean when
    both are present, since only `workplace` can express hybrid/onsite."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={
                "description": "", "requirements": "", "benefits": "", "workplace": "hybrid",
            })
        return httpx.Response(200, json={"jobs": [
            {"title": "T", "shortcode": "S1", "telecommuting": True, "city": "Austin"},
        ]})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].remote_status == "hybrid"


def test_workable_unrecognized_workplace_value_falls_back_never_guessed():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={
                "description": "", "requirements": "", "benefits": "", "workplace": "some-future-value",
            })
        return httpx.Response(200, json={"jobs": [
            {"title": "T", "shortcode": "S1", "telecommuting": True, "city": "Austin"},
        ]})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].remote_status == "remote"  # falls back to telecommuting, never the unrecognized string


def test_workable_url_falls_back_to_listing_page_without_application_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={"description": "", "requirements": "", "benefits": ""})
        return httpx.Response(200, json={"jobs": [
            {"title": "T", "shortcode": "S1", "url": "https://apply.workable.com/acme/j/S1/"},
        ]})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert jobs[0].url == "https://apply.workable.com/acme/j/S1/"


def test_workable_pagination_stops_on_empty_page():
    calls = {"pages_requested": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={"description": "", "requirements": "", "benefits": ""})
        page = int(request.url.params.get("page", "1"))
        calls["pages_requested"].append(page)
        if page == 1:
            return httpx.Response(200, json={"jobs": [
                {"title": "Job A", "shortcode": "A1", "city": "Remote"}
            ]})
        return httpx.Response(200, json={"jobs": []})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=50)
    assert len(jobs) == 1
    assert calls["pages_requested"] == [1, 2]  # stopped after first empty page


def test_workable_pagination_protects_against_repeated_page():
    """A provider re-serving the same job on every page (broken/malicious
    pagination) must not cause an infinite loop -- MAX_PAGES_PER_PROVIDER
    bounds it, and repeated shortcodes are deduped within one fetch."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={"description": "", "requirements": "", "benefits": ""})
        return httpx.Response(200, json={"jobs": [{"title": "Same Job", "shortcode": "DUPE", "city": "Remote"}]})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=50)
    assert len(jobs) == 1  # deduped despite endless repeated pages


def test_workable_isolates_account_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "badaccount" in str(request.url):
            return httpx.Response(500, text="server error")
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={"description": "d", "requirements": "", "benefits": ""})
        return httpx.Response(200, json={"jobs": [{"title": "T", "shortcode": "S1", "city": "Remote"}]})

    provider = WorkableProvider(["badaccount", "goodaccount"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1


def test_workable_detail_fetch_failure_isolated_keeps_list_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(500, text="detail unavailable")
        return httpx.Response(200, json={"jobs": [{"title": "T", "shortcode": "S1", "city": "Remote"}]})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    assert len(jobs) == 1
    assert jobs[0].description == ""  # detail failed -- description empty, not fabricated


def test_workable_malformed_job_isolated():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={"description": "d", "requirements": "", "benefits": ""})
        return httpx.Response(200, json={"jobs": [
            {"title": None, "shortcode": None},  # will produce empty external_job_id, still processed
            {"title": "Good", "shortcode": "GOOD1", "city": "Remote"},
        ]})

    provider = WorkableProvider(["acme"], client=_client(handler))
    jobs = provider.fetch_jobs(max_jobs=10)
    titles = {j.title for j in jobs}
    assert "Good" in titles
