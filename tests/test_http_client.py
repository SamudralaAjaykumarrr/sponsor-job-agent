import httpx
import pytest

from app.providers.http_client import ProviderHTTPError, ResponseTooLargeError, get_json, request_with_retries


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_json_success():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    data = get_json(_client(handler), "https://example.com/x", provider="test")
    assert data == {"ok": True}


def test_transient_500_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, json={"ok": True})

    data = get_json(_client(handler), "https://example.com/x", provider="test",
                     max_retries=3, sleep_fn=lambda s: None)
    assert data == {"ok": True}
    assert calls["n"] == 3


def test_retries_are_bounded_not_infinite():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="always down")

    with pytest.raises(ProviderHTTPError):
        get_json(_client(handler), "https://example.com/x", provider="test",
                 max_retries=2, sleep_fn=lambda s: None)
    assert calls["n"] == 3  # initial attempt + 2 retries, never more


def test_non_retryable_4xx_raises_immediately():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    with pytest.raises(ProviderHTTPError):
        get_json(_client(handler), "https://example.com/x", provider="test",
                 max_retries=3, sleep_fn=lambda s: None)
    assert calls["n"] == 1  # no retry on a non-transient client error


def test_429_respects_retry_after_header():
    calls = {"n": 0}
    waited = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
        return httpx.Response(200, json={"ok": True})

    data = get_json(_client(handler), "https://example.com/x", provider="test",
                     max_retries=2, sleep_fn=lambda s: waited.append(s))
    assert data == {"ok": True}
    assert waited == [2.0]


def test_network_error_retries_then_raises():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderHTTPError):
        get_json(_client(handler), "https://example.com/x", provider="test",
                 max_retries=1, sleep_fn=lambda s: None)


def test_response_too_large_by_content_length_raises():
    def handler(request):
        return httpx.Response(200, headers={"content-length": "999999999"}, json={"ok": True})

    with pytest.raises(ResponseTooLargeError):
        request_with_retries(_client(handler), "GET", "https://example.com/x", provider="test",
                              max_response_bytes=100, sleep_fn=lambda s: None)


def test_response_too_large_by_actual_body_raises():
    def handler(request):
        return httpx.Response(200, json={"data": "x" * 1000})

    with pytest.raises(ResponseTooLargeError):
        request_with_retries(_client(handler), "GET", "https://example.com/x", provider="test",
                              max_response_bytes=50, sleep_fn=lambda s: None)


def test_user_agent_is_set_on_built_client():
    from app.providers.http_client import build_client
    client = build_client()
    try:
        assert "SponsorJobAgent" in client.headers.get("user-agent", "")
    finally:
        client.close()
