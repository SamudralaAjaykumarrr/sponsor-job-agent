"""Centralized HTTP behavior shared by every provider connector: bounded
timeouts, bounded retries with exponential backoff, a response-size cap, a
descriptive User-Agent, and structured logging. No connector should build its
own httpx.Client or retry loop -- route all provider HTTP calls through here
so the safety properties (no infinite retries, no unbounded response bodies,
no hammering) hold everywhere at once."""

import logging
import time
from typing import Callable, Optional

import httpx

from app import config

logger = logging.getLogger("providers.http")


class ResponseTooLargeError(Exception):
    pass


class ProviderHTTPError(Exception):
    """Wraps a final (non-retryable, or retries-exhausted) HTTP/network failure
    with the provider name for clearer logs/discovery_log entries."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(f"{provider}: {message}")


def build_client(timeout: Optional[float] = None) -> httpx.Client:
    t = timeout if timeout is not None else config.PROVIDER_HTTP_TIMEOUT_SECONDS
    return httpx.Client(
        timeout=httpx.Timeout(connect=t, read=t, write=t, pool=t),
        headers={"User-Agent": config.PROVIDER_USER_AGENT},
    )


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    max_retries: Optional[int] = None,
    max_response_bytes: Optional[int] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET/POST with bounded retries (transient 429/5xx/timeouts/connect
    errors only) and exponential backoff. Raises ProviderHTTPError once
    retries are exhausted or on a non-retryable HTTP error status. Never
    retries indefinitely."""
    max_retries = config.PROVIDER_MAX_RETRIES if max_retries is None else max_retries
    max_response_bytes = max_response_bytes or config.PROVIDER_MAX_RESPONSE_BYTES

    attempt = 0
    backoff = 0.1
    while True:
        try:
            resp = client.request(method, url, params=params, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= max_retries:
                raise ProviderHTTPError(provider, f"request failed after {attempt + 1} attempt(s): {exc}") from exc
            logger.warning("provider %s transient network error (attempt %s): %s", provider, attempt + 1, exc)
            sleep_fn(backoff)
            backoff *= 2
            attempt += 1
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt >= max_retries:
                raise ProviderHTTPError(provider, f"HTTP {resp.status_code} after {attempt + 1} attempt(s)")
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = backoff
            logger.warning("provider %s transient HTTP %s (attempt %s), retrying in %.1fs",
                            provider, resp.status_code, attempt + 1, wait)
            sleep_fn(wait)
            backoff *= 2
            attempt += 1
            continue

        if resp.status_code >= 400:
            raise ProviderHTTPError(provider, f"HTTP {resp.status_code}: {resp.text[:200]}")

        content_length = resp.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_response_bytes:
                    raise ResponseTooLargeError(f"{provider}: declared content-length {content_length} exceeds cap")
            except ValueError:
                pass
        if len(resp.content) > max_response_bytes:
            raise ResponseTooLargeError(f"{provider}: response body ({len(resp.content)} bytes) exceeds cap")

        return resp


def get_json(
    client: httpx.Client,
    url: str,
    *,
    provider: str,
    params: Optional[dict] = None,
    max_retries: Optional[int] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
):
    resp = request_with_retries(
        client, "GET", url, provider=provider, params=params, max_retries=max_retries, sleep_fn=sleep_fn,
    )
    return resp.json()


def post_json(
    client: httpx.Client,
    url: str,
    *,
    provider: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    max_retries: Optional[int] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
):
    resp = request_with_retries(
        client, "POST", url, provider=provider, params=params, json_body=json_body,
        max_retries=max_retries, sleep_fn=sleep_fn,
    )
    return resp.json()
