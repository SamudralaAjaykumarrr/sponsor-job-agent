"""Application rate limiting (CLAUDE.md Phase 8 sections 46, 62). Enforced by
querying application_audit_log 'submit_attempted' events directly against
the shared database -- already fleet-wide the instant DATABASE_URL points at
a shared Postgres instance, matching Phase 6's distributed-rate-limiting
principle (no separate in-memory counter that would only be per-process)."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import config
from app.db import db_session


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RateLimitResult:
    allowed: bool
    reason: str = ""


def _count_submit_attempts_since(cutoff_iso: str, *, company: str | None = None) -> int:
    query = (
        "SELECT COUNT(*) AS c FROM application_audit_log a "
        "JOIN jobs j ON j.id = a.job_id "
        "WHERE a.event_type = 'submit_attempted' AND a.created_at >= ?"
    )
    params: list = [cutoff_iso]
    if company:
        query += " AND j.company = ?"
        params.append(company)
    with db_session() as conn:
        return conn.execute(query, params).fetchone()["c"]


def check_rate_limits(company: str) -> RateLimitResult:
    now = utcnow_dt()
    hour_cutoff = (now - timedelta(hours=1)).isoformat()
    day_cutoff = (now - timedelta(days=1)).isoformat()

    hourly = _count_submit_attempts_since(hour_cutoff)
    if hourly >= config.MAX_APPLICATIONS_PER_HOUR:
        return RateLimitResult(False, f"MAX_APPLICATIONS_PER_HOUR ({config.MAX_APPLICATIONS_PER_HOUR}) reached: {hourly} in the last hour.")

    daily = _count_submit_attempts_since(day_cutoff)
    if daily >= config.MAX_APPLICATIONS_PER_DAY:
        return RateLimitResult(False, f"MAX_APPLICATIONS_PER_DAY ({config.MAX_APPLICATIONS_PER_DAY}) reached: {daily} in the last 24h.")

    company_daily = _count_submit_attempts_since(day_cutoff, company=company)
    if company_daily >= config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY:
        return RateLimitResult(
            False,
            f"MAX_APPLICATIONS_PER_COMPANY_PER_DAY ({config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY}) reached for "
            f"'{company}': {company_daily} in the last 24h.",
        )

    return RateLimitResult(True)
