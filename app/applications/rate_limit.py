"""Application rate limiting (CLAUDE.md Phase 8 sections 46, 62). Enforced by
querying application_audit_log 'submit_attempted' events directly against
the shared database -- already fleet-wide the instant DATABASE_URL points at
a shared Postgres instance, matching Phase 6's distributed-rate-limiting
principle (no separate in-memory counter that would only be per-process).

Apply/Automation Settings V1 demo-isolation fix: every count below joins to
`jobs` and excludes `is_test_fixture = 1` rows -- deterministic demo/test
scenarios (app.applications.demo, app.agent.orchestrator's TEST MODE
fixture) must never consume, or be blocked by, a REAL application's rate
limit budget, and vice versa. This never weakens real enforcement: a real
job is never `is_test_fixture`, so its counting is completely unchanged.
The dedicated "application limit reached" demo scenario
(app.applications.demo's "application_limit" key) demonstrates the blocked
experience via a short-lived, restored `config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY`
override instead of relying on this (deliberately exempted) counting path."""

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
        "WHERE a.event_type = 'submit_attempted' AND a.created_at >= ? AND j.is_test_fixture = 0"
    )
    params: list = [cutoff_iso]
    if company:
        query += " AND j.company = ?"
        params.append(company)
    with db_session() as conn:
        return conn.execute(query, params).fetchone()["c"]


def _count_concurrent_active() -> int:
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions ae "
            "JOIN jobs j ON j.id = ae.job_id "
            "WHERE ae.active = 1 AND j.is_test_fixture = 0"
        ).fetchone()["c"]


def check_rate_limits(company: str) -> RateLimitResult:
    now = utcnow_dt()
    hour_cutoff = (now - timedelta(hours=1)).isoformat()
    day_cutoff = (now - timedelta(days=1)).isoformat()
    week_cutoff = (now - timedelta(days=7)).isoformat()

    hourly = _count_submit_attempts_since(hour_cutoff)
    if hourly >= config.MAX_APPLICATIONS_PER_HOUR:
        return RateLimitResult(False, f"MAX_APPLICATIONS_PER_HOUR ({config.MAX_APPLICATIONS_PER_HOUR}) reached: {hourly} in the last hour.")

    daily = _count_submit_attempts_since(day_cutoff)
    if daily >= config.MAX_APPLICATIONS_PER_DAY:
        return RateLimitResult(False, f"MAX_APPLICATIONS_PER_DAY ({config.MAX_APPLICATIONS_PER_DAY}) reached: {daily} in the last 24h.")

    if config.MAX_APPLICATIONS_PER_WEEK:
        weekly = _count_submit_attempts_since(week_cutoff)
        if weekly >= config.MAX_APPLICATIONS_PER_WEEK:
            return RateLimitResult(
                False, f"MAX_APPLICATIONS_PER_WEEK ({config.MAX_APPLICATIONS_PER_WEEK}) reached: {weekly} in the last 7 days."
            )

    company_daily = _count_submit_attempts_since(day_cutoff, company=company)
    if company_daily >= config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY:
        return RateLimitResult(
            False,
            f"MAX_APPLICATIONS_PER_COMPANY_PER_DAY ({config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY}) reached for "
            f"'{company}': {company_daily} in the last 24h.",
        )

    if config.MAX_CONCURRENT_APPLICATIONS:
        concurrent = _count_concurrent_active()
        if concurrent >= config.MAX_CONCURRENT_APPLICATIONS:
            return RateLimitResult(
                False,
                f"MAX_CONCURRENT_APPLICATIONS ({config.MAX_CONCURRENT_APPLICATIONS}) reached: "
                f"{concurrent} application(s) currently in progress.",
            )

    return RateLimitResult(True)
