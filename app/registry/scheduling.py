"""Deterministic (non-ML) adaptive polling rules for company_registry tenants.
Productive tenants get polled more often; failing/low-yield tenants back off.
Bounded by PROVIDER_MIN_POLL_MINUTES / PROVIDER_MAX_POLL_MINUTES so no tenant
is ever polled unboundedly fast or effectively abandoned forever."""

from datetime import datetime, timedelta, timezone
from enum import Enum

from app import config


class TenantHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"


FAILURE_BACKOFF_THRESHOLD = 3   # consecutive failures before DEGRADED
FAILING_THRESHOLD = 10          # consecutive failures before FAILING


def _clamp_minutes(minutes: float) -> int:
    return int(max(config.PROVIDER_MIN_POLL_MINUTES, min(config.PROVIDER_MAX_POLL_MINUTES, minutes)))


def next_interval_minutes(
    current_interval_minutes: int,
    *,
    success: bool,
    jobs_new: int,
    consecutive_failures: int,
) -> int:
    """Pure function: given the outcome of one poll, returns the next poll
    interval in minutes. Rules:
      - failure: double the interval per additional consecutive failure (capped)
      - success + new jobs found: speed up (interval * 0.75)
      - success + no new jobs: slow down slightly (interval * 1.25)
    """
    if not success:
        # Double the current interval on every failure (bounded) -- grows
        # from wherever the tenant's interval currently sits rather than a
        # fixed base, so a tenant that was already polling slowly backs off
        # from there instead of resetting.
        base = max(current_interval_minutes, config.PROVIDER_DEFAULT_POLL_MINUTES)
        return _clamp_minutes(base * 2)

    if jobs_new > 0:
        return _clamp_minutes(current_interval_minutes * 0.75)
    return _clamp_minutes(current_interval_minutes * 1.25)


def compute_health(consecutive_failures: int, last_success_at: str | None) -> TenantHealth:
    if consecutive_failures >= FAILING_THRESHOLD:
        return TenantHealth.FAILING
    if consecutive_failures >= FAILURE_BACKOFF_THRESHOLD:
        return TenantHealth.DEGRADED
    if last_success_at is None:
        return TenantHealth.DEGRADED
    return TenantHealth.HEALTHY


def compute_next_poll_at(interval_minutes: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(minutes=interval_minutes)).isoformat()
