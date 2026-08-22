"""CLAUDE.md Phase 6 sections 12-14: structured ProviderFetchResult. Exercises
the classify_exception() unit logic directly, then end-to-end through a real
provider (Greenhouse) with a mocked httpx transport to prove the
`self._last_error` stash + JobProvider.fetch_jobs_result() wiring actually
distinguishes success/empty/failure without changing fetch_jobs() itself."""

import json

import httpx
import pytest

from app.providers.errors import ProviderFetchStatus, classify_exception
from app.providers.greenhouse import GreenhouseProvider
from app.providers.http_client import ProviderHTTPError, ResponseTooLargeError
from app.providers.unsupported import JobviteProvider


# --- classify_exception() unit coverage of every documented status ---------

def test_classify_response_too_large():
    status, retryable, http = classify_exception(ResponseTooLargeError("boom"))
    assert status == ProviderFetchStatus.RESPONSE_TOO_LARGE
    assert retryable is True


def test_classify_timeout():
    status, retryable, _ = classify_exception(httpx.TimeoutException("timed out"))
    assert status == ProviderFetchStatus.TIMEOUT
    assert retryable is True


def test_classify_transport_error():
    status, retryable, _ = classify_exception(httpx.ConnectError("refused"))
    assert status == ProviderFetchStatus.TEMPORARY_HTTP_FAILURE
    assert retryable is True


def test_classify_permanent_not_found():
    status, retryable, http = classify_exception(ProviderHTTPError("greenhouse", "HTTP 404: not found"))
    assert status == ProviderFetchStatus.PERMANENT_NOT_FOUND
    assert retryable is False
    assert http == 404


def test_classify_invalid_tenant_auth_error():
    status, retryable, http = classify_exception(ProviderHTTPError("greenhouse", "HTTP 403: forbidden"))
    assert status == ProviderFetchStatus.INVALID_TENANT
    assert retryable is False
    assert http == 403


def test_classify_rate_limited():
    status, retryable, http = classify_exception(ProviderHTTPError("greenhouse", "HTTP 429 after 3 attempt(s)"))
    assert status == ProviderFetchStatus.RATE_LIMITED
    assert retryable is True
    assert http == 429


def test_classify_temporary_http_failure_5xx():
    status, retryable, http = classify_exception(ProviderHTTPError("greenhouse", "HTTP 503 after 3 attempt(s)"))
    assert status == ProviderFetchStatus.TEMPORARY_HTTP_FAILURE
    assert retryable is True
    assert http == 503


def test_classify_malformed_response():
    status, retryable, _ = classify_exception(json.JSONDecodeError("bad", "doc", 0))
    assert status == ProviderFetchStatus.MALFORMED_RESPONSE
    assert retryable is True


def test_classify_unknown_failure_is_conservative():
    status, retryable, _ = classify_exception(RuntimeError("something weird"))
    assert status == ProviderFetchStatus.UNKNOWN_FAILURE
    assert retryable is True


# --- end-to-end through a real provider -------------------------------------

def test_unsupported_provider_short_circuits_without_request():
    provider = JobviteProvider(["some-tenant"])
    result = provider.fetch_jobs_result(10, tenant="some-tenant")
    assert result.status == ProviderFetchStatus.UNSUPPORTED
    assert result.jobs == []
    assert result.retryable is False


def test_success_with_jobs(mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Backend Engineer", "location": {"name": "Remote"},
             "content": "desc", "absolute_url": "https://x/1", "updated_at": "2026-01-01T00:00:00Z"},
        ]})

    mock_httpx(handler)
    provider = GreenhouseProvider(["acme"])
    result = provider.fetch_jobs_result(10, tenant="acme")
    assert result.status == ProviderFetchStatus.SUCCESS_WITH_JOBS
    assert result.is_success
    assert len(result.jobs) == 1


def test_success_empty_board_is_not_a_failure(mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)
    provider = GreenhouseProvider(["acme"])
    result = provider.fetch_jobs_result(10, tenant="acme")
    assert result.status == ProviderFetchStatus.SUCCESS_EMPTY
    assert result.is_success
    assert result.jobs == []


def test_404_is_reported_as_permanent_not_found(mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    mock_httpx(handler)
    provider = GreenhouseProvider(["missing-board"])
    result = provider.fetch_jobs_result(10, tenant="missing-board")
    assert result.status == ProviderFetchStatus.PERMANENT_NOT_FOUND
    assert not result.is_success
    assert result.retryable is False
    assert result.jobs == []


def test_response_too_large_is_reported_not_silently_empty(mock_httpx):
    big_payload = json.dumps({"jobs": [{"id": i, "title": "x" * 1000} for i in range(20000)]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big_payload)

    mock_httpx(handler)
    provider = GreenhouseProvider(["huge-board"], timeout=2.0)
    result = provider.fetch_jobs_result(10, tenant="huge-board")
    assert result.status == ProviderFetchStatus.RESPONSE_TOO_LARGE
    assert result.retryable is True
    assert result.jobs == []


def test_fetch_jobs_itself_is_unchanged_by_last_error_stash(mock_httpx):
    """fetch_jobs() must still just return [] on failure -- no new
    exception type, no new required handling for any existing caller."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    mock_httpx(handler)
    provider = GreenhouseProvider(["missing-board"])
    jobs = provider.fetch_jobs(10)
    assert jobs == []
