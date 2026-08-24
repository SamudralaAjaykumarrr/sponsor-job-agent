"""Workday/SmartRecruiters/Workable browser-assist hardening (2026-08-22):
doctor check added this pass. Read-only report generation -- never
auto-repairs, mirroring tests/test_applications_doctor_phase12.py's own
pattern for the same browser_spa_events table."""

import pytest

from app.applications import spa_events
from app.applications.doctor import run_doctor


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def test_no_validation_blocked_events_means_no_issue():
    report = run_doctor()
    assert not any(i.check == "validation_blocked" for i in report.issues)


def test_validation_blocked_event_flagged():
    spa_events.record(spa_events.EVENT_VALIDATION_BLOCKED, session_id="bsess_x", provider="workday",
                       detail="School is required")
    report = run_doctor()
    assert any(i.check == "validation_blocked" for i in report.issues)


def test_validation_blocked_issue_is_a_warning_not_serious():
    spa_events.record(spa_events.EVENT_VALIDATION_BLOCKED, session_id="bsess_x", provider="workday")
    report = run_doctor()
    issue = next(i for i in report.issues if i.check == "validation_blocked")
    assert issue.severity == "warning"
