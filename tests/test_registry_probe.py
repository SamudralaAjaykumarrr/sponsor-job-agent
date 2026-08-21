import httpx
import pytest

from app.providers.http_client import ProviderHTTPError
from app.registry import probe


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_greenhouse_success():
    def handler(request):
        return httpx.Response(200, json={"jobs": []})

    resp = probe.probe("greenhouse", "acme", client=_client(handler))
    assert resp.status_code == 200


def test_probe_greenhouse_404_raises():
    def handler(request):
        return httpx.Response(404, text="not found")

    with pytest.raises(ProviderHTTPError):
        probe.probe("greenhouse", "doesnotexist", client=_client(handler))


def test_probe_unknown_provider_raises():
    with pytest.raises(ProviderHTTPError):
        probe.probe("some-unknown-provider", "acme")


def test_has_probe_matches_supported_providers():
    assert probe.has_probe("greenhouse")
    assert probe.has_probe("workday")
    assert not probe.has_probe("icims")  # UNSUPPORTED provider, no probe implemented


def test_probe_workday_reconstructs_short_form_tenant():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"jobPostings": [], "total": 0})

    probe.probe("workday", "acme/wd5/External", client=_client(handler))
    assert "acme.wd5.myworkdayjobs.com" in seen["url"]
