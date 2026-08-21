import httpx

from app.registry.page_discovery import discover_career_links


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_career_links_finds_greenhouse_link_on_homepage():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url.rstrip("/") == "https://examplecompany.com":
            return httpx.Response(200, text='<html><body><a href="https://boards.greenhouse.io/examplecompany">Careers</a></body></html>')
        return httpx.Response(404)

    result = discover_career_links("examplecompany.com", client=_client_returning(handler))
    assert result.best_match is not None
    assert result.best_match.provider == "greenhouse"
    assert result.best_match.tenant_identifier == "examplecompany"


def test_discover_career_links_no_match_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>Nothing here</body></html>")

    result = discover_career_links("norealats.example", client=_client_returning(handler))
    assert result.best_match is None


def test_discover_career_links_respects_robots_disallow():
    fetched_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /careers\n")
        fetched_paths.append(httpx.URL(url).path)
        return httpx.Response(200, text="<html></html>")

    discover_career_links("robotstest.example", client=_client_returning(handler))
    assert "/careers" not in fetched_paths


def test_discover_career_links_bounded_number_of_pages():
    from app import config

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        call_count["n"] += 1
        return httpx.Response(200, text="<html></html>")

    discover_career_links("boundedtest.example", client=_client_returning(handler))
    assert call_count["n"] <= config.PAGE_DISCOVERY_MAX_PAGES


def test_discover_career_links_response_size_cap():
    from app import config

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * (config.PAGE_DISCOVERY_MAX_RESPONSE_BYTES + 1000))

    result = discover_career_links("bigresponse.example", client=_client_returning(handler))
    assert result.best_match is None  # oversized response is discarded, not parsed
