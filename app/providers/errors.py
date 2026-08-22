"""Structured provider fetch results (CLAUDE.md Phase 6 sections 12-15).

Phase 5 identified a real architectural gap: every provider connector's
per-tenant fetch helper (e.g. GreenhouseProvider._fetch_board) catches its
own HTTP/parsing errors, logs, and returns an empty list -- which is exactly
right for the multi-tenant static-config discovery path (one bad tenant
must never abort the others), but means the caller can never tell "this
board legitimately has zero jobs right now" apart from "this board's fetch
just failed". That silent swallowing is exactly what let a real
ResponseTooLargeError go unrecorded during Phase 5's own live validation
(see CLAUDE.md Phase 5 section notes) until an outer safety net in
app/workers/runner.py was added as a last resort.

Fix, chosen to minimize risk of regressing 11 already-tested provider
connectors: rather than rewriting every connector's control flow to raise
instead of swallow (a large, regression-prone diff across every provider
file), each connector's existing `except` blocks additionally stash the
last exception it swallowed onto `self._last_error` (see the one-line
addition in each provider file). `fetch_jobs()` itself is COMPLETELY
UNCHANGED -- same signature, same swallow-and-return-[] behavior, same 423
pre-Phase-6 tests still pass unmodified. `fetch_jobs_result()` below is a
NEW method (added once, in the JobProvider base class) that calls
`fetch_jobs()` and then classifies the outcome: if jobs came back, or if no
error was stashed, the empty/non-empty result is treated as a genuine
SUCCESS; if the list came back empty AND an error was stashed, that error is
classified into the typed ProviderFetchStatus so callers (the worker
runner) can finally see it, feed the circuit breaker accurately, and record
a real error_type in attempt history instead of a false "everything's fine".

This only distinguishes "at least one tenant attempt failed" from "every
tenant attempt succeeded" for a provider instance -- for the Phase 4/5
per-tenant registry path (app.providers.registry.build_provider_for_tenant
always constructs a single-tenant instance), that is exactly precise: one
tenant, one outcome, unambiguous."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx

from app.providers.http_client import ProviderHTTPError, ResponseTooLargeError

_PERMANENT_HTTP_STATUSES = {400, 404, 410}
_AUTH_HTTP_STATUSES = {401, 403}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProviderFetchStatus(str, Enum):
    """Every possible outcome of one fetch_jobs_result() call. The critical
    rule (CLAUDE.md section 12): SUCCESS_EMPTY must never be confused with
    any failure status below it."""

    SUCCESS_WITH_JOBS = "SUCCESS_WITH_JOBS"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    TEMPORARY_HTTP_FAILURE = "TEMPORARY_HTTP_FAILURE"
    PERMANENT_NOT_FOUND = "PERMANENT_NOT_FOUND"
    INVALID_TENANT = "INVALID_TENANT"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    UNSUPPORTED = "UNSUPPORTED"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"

    @property
    def is_success(self) -> bool:
        return self in (ProviderFetchStatus.SUCCESS_WITH_JOBS, ProviderFetchStatus.SUCCESS_EMPTY)


# Statuses that are worth retrying later (transient) vs never (permanent) --
# mirrors the same permanent-vs-temporary distinction app.workers.retry and
# app.registry.verification already use, applied to the richer enum here.
_RETRYABLE_STATUSES = {
    ProviderFetchStatus.TIMEOUT,
    ProviderFetchStatus.RATE_LIMITED,
    ProviderFetchStatus.TEMPORARY_HTTP_FAILURE,
    ProviderFetchStatus.RESPONSE_TOO_LARGE,
    ProviderFetchStatus.MALFORMED_RESPONSE,
    ProviderFetchStatus.UNKNOWN_FAILURE,
}


@dataclass
class ProviderFetchResult:
    status: ProviderFetchStatus
    jobs: list
    provider: str
    tenant: str
    started_at: str
    finished_at: str
    latency_ms: float
    http_status: Optional[int] = None
    retry_after: Optional[float] = None
    error_type: Optional[str] = None
    error_message_safe: str = ""
    retryable: bool = False
    schema_fingerprint: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.status.is_success


def _http_status_from_message(message: str) -> Optional[int]:
    for code in (429, 500, 502, 503, 504, *_PERMANENT_HTTP_STATUSES, *_AUTH_HTTP_STATUSES):
        if f"HTTP {code}" in message:
            return code
    return None


def classify_exception(exc: BaseException) -> tuple["ProviderFetchStatus", bool, Optional[int]]:
    """Returns (status, retryable, http_status). Never raises -- an
    unrecognized exception shape becomes UNKNOWN_FAILURE/retryable=True,
    the same conservative default app.workers.retry.classify_exception
    already uses, so an ambiguous error is never treated as a permanent
    reason to give up."""
    if isinstance(exc, ResponseTooLargeError):
        return ProviderFetchStatus.RESPONSE_TOO_LARGE, True, None

    if isinstance(exc, ProviderHTTPError):
        message = str(exc)
        http_status = _http_status_from_message(message)
        if http_status in _AUTH_HTTP_STATUSES:
            return ProviderFetchStatus.INVALID_TENANT, False, http_status
        if http_status in _PERMANENT_HTTP_STATUSES:
            return ProviderFetchStatus.PERMANENT_NOT_FOUND, False, http_status
        if http_status == 429:
            return ProviderFetchStatus.RATE_LIMITED, True, http_status
        if http_status and http_status >= 500:
            return ProviderFetchStatus.TEMPORARY_HTTP_FAILURE, True, http_status
        return ProviderFetchStatus.UNKNOWN_FAILURE, True, http_status

    if isinstance(exc, httpx.TimeoutException):
        return ProviderFetchStatus.TIMEOUT, True, None
    if isinstance(exc, httpx.TransportError):
        return ProviderFetchStatus.TEMPORARY_HTTP_FAILURE, True, None
    if isinstance(exc, (json.JSONDecodeError, ValueError, KeyError, TypeError)):
        return ProviderFetchStatus.MALFORMED_RESPONSE, True, None

    return ProviderFetchStatus.UNKNOWN_FAILURE, True, None


def build_result(
    *, provider: str, tenant: str, jobs: list, started_at: str, error: Optional[BaseException],
) -> ProviderFetchResult:
    finished_at = utcnow()
    started_dt = datetime.fromisoformat(started_at)
    finished_dt = datetime.fromisoformat(finished_at)
    latency_ms = (finished_dt - started_dt).total_seconds() * 1000.0

    if jobs:
        # Jobs came back -- unambiguous success regardless of whether some
        # earlier attempt inside this call also hit a transient error.
        return ProviderFetchResult(
            status=ProviderFetchStatus.SUCCESS_WITH_JOBS, jobs=jobs, provider=provider, tenant=tenant,
            started_at=started_at, finished_at=finished_at, latency_ms=latency_ms,
        )
    if error is None:
        return ProviderFetchResult(
            status=ProviderFetchStatus.SUCCESS_EMPTY, jobs=jobs, provider=provider, tenant=tenant,
            started_at=started_at, finished_at=finished_at, latency_ms=latency_ms,
        )
    status, retryable, http_status = classify_exception(error)
    return ProviderFetchResult(
        status=status, jobs=jobs, provider=provider, tenant=tenant, started_at=started_at,
        finished_at=finished_at, latency_ms=latency_ms, http_status=http_status,
        error_type=status.value, error_message_safe=str(error)[:300], retryable=retryable,
    )
