"""Centralized retry classification + bounded exponential backoff for the
Phase 5 worker fleet. Mirrors the philosophy already established in
app.registry.verification (an unrecognized failure shape is treated as
retryable/transient, never permanent, so a portal is never permanently
discarded on an ambiguous error) but is the canonical classifier for the
ongoing polling loop specifically -- see CLAUDE.md Phase 5 section 9.

Retry-After is already honored at the HTTP layer (app.providers.http_client
.request_with_retries reads the header during its own bounded retry loop
before ever raising). What this module adds is the OUTER, cross-cycle policy:
how long to wait before this portal is even attempted again, and when enough
consecutive PERMANENT failures justify giving up (dead-lettering) rather than
retrying forever."""

import httpx

from app.providers.http_client import ProviderHTTPError

_TEMPORARY_MARKERS = (
    "429", "500", "502", "503", "504", "timeout", "Timeout",
    "request failed after", "TransportError", "connection", "ResponseTooLargeError",
)
_PERMANENT_MARKERS = ("400", "401", "403", "404", "410")


def classify_exception(exc: Exception) -> tuple[bool, str]:
    """Returns (retryable, error_type). Never raises."""
    if isinstance(exc, ProviderHTTPError):
        message = str(exc)
        if any(marker in message for marker in _PERMANENT_MARKERS):
            return False, "permanent_http_error"
        if any(marker in message for marker in _TEMPORARY_MARKERS):
            return True, "temporary_http_error"
        # Unrecognized shape -- conservative: treat as retryable so a portal
        # is never permanently discarded on an ambiguous error message.
        return True, "unclassified_http_error"
    if isinstance(exc, (httpx.TimeoutException,)):
        return True, "timeout"
    if isinstance(exc, (httpx.TransportError,)):
        return True, "connection_error"
    # Any other unexpected exception (parsing bug, etc.) -- retryable by
    # default, same conservative philosophy as verification.py.
    return True, "unexpected_error"


def backoff_seconds(attempt_number: int, *, base_seconds: float = 30.0, cap_seconds: float = 3600.0) -> float:
    """Bounded exponential backoff: 30s, 60s, 120s, 240s, ... capped. Never
    unbounded, never infinite -- attempt_number is 1-indexed (first retry)."""
    delay = base_seconds * (2 ** max(0, attempt_number - 1))
    return min(delay, cap_seconds)
