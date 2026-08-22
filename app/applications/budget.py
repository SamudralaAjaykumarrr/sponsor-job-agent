"""Deterministic daily application-budget accounting (CLAUDE.md Phase 9
section 11). Every value is a live DB query scoped to "today" (UTC calendar
day) at collection time -- never an in-process accumulator (which would be
wrong the moment more than one worker process exists), matching the same
principle as app.workers.metrics.fleet_snapshot and app.applications.metrics.

`submitted_today` counts only genuine submit attempts (the
'submit_attempted' audit event, written immediately before provider.submit()
is called) -- PREPARE-only runs that stop at SUBMISSION_READY/
NEEDS_USER_ACTION are never counted as submitted, per the section's explicit
requirement."""

from dataclasses import dataclass
from datetime import datetime, timezone

from app import config
from app.applications.models import ExecutionStatus
from app.db import db_session


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


@dataclass
class DailyBudget:
    submitted_today: int
    confirmed_today: int
    failed_today: int
    needs_user_action_today: int
    max_applications_per_hour: int
    max_applications_per_day: int
    max_applications_per_company_per_day: int
    submitted_last_hour: int

    def as_dict(self) -> dict:
        return {
            "submitted_today": self.submitted_today, "confirmed_today": self.confirmed_today,
            "failed_today": self.failed_today, "needs_user_action_today": self.needs_user_action_today,
            "max_applications_per_hour": self.max_applications_per_hour,
            "max_applications_per_day": self.max_applications_per_day,
            "max_applications_per_company_per_day": self.max_applications_per_company_per_day,
            "submitted_last_hour": self.submitted_last_hour,
            "daily_budget_remaining": max(0, self.max_applications_per_day - self.submitted_today),
            "hourly_budget_remaining": max(0, self.max_applications_per_hour - self.submitted_last_hour),
        }


_FAILED_STATUSES = (
    ExecutionStatus.SUBMISSION_FAILED.value, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value,
    ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value, ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED.value,
    ExecutionStatus.JOB_NO_LONGER_ACTIVE.value,
)
_NEEDS_ACTION_STATUSES = (
    ExecutionStatus.NEEDS_USER_ACTION.value, ExecutionStatus.VALIDATION_REQUIRED.value,
    ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value,
)


def collect() -> DailyBudget:
    from datetime import timedelta

    today_start = _today_start_iso()
    hour_start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    with db_session() as conn:
        submitted_today = conn.execute(
            "SELECT COUNT(*) AS c FROM application_audit_log WHERE event_type = 'submit_attempted' AND created_at >= ?",
            (today_start,),
        ).fetchone()["c"]
        submitted_last_hour = conn.execute(
            "SELECT COUNT(*) AS c FROM application_audit_log WHERE event_type = 'submit_attempted' AND created_at >= ?",
            (hour_start,),
        ).fetchone()["c"]
        confirmed_today = conn.execute(
            "SELECT COUNT(*) AS c FROM application_executions WHERE status = ? AND finished_at >= ?",
            (ExecutionStatus.APPLIED.value, today_start),
        ).fetchone()["c"]
        placeholders = ", ".join("?" for _ in _FAILED_STATUSES)
        failed_today = conn.execute(
            f"SELECT COUNT(*) AS c FROM application_executions WHERE status IN ({placeholders}) AND finished_at >= ?",
            (*_FAILED_STATUSES, today_start),
        ).fetchone()["c"]
        placeholders2 = ", ".join("?" for _ in _NEEDS_ACTION_STATUSES)
        needs_user_action_today = conn.execute(
            f"SELECT COUNT(*) AS c FROM application_executions WHERE status IN ({placeholders2}) AND updated_at >= ?",
            (*_NEEDS_ACTION_STATUSES, today_start),
        ).fetchone()["c"]

    return DailyBudget(
        submitted_today=submitted_today, confirmed_today=confirmed_today, failed_today=failed_today,
        needs_user_action_today=needs_user_action_today,
        max_applications_per_hour=config.MAX_APPLICATIONS_PER_HOUR,
        max_applications_per_day=config.MAX_APPLICATIONS_PER_DAY,
        max_applications_per_company_per_day=config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY,
        submitted_last_hour=submitted_last_hour,
    )
