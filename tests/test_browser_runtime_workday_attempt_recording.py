"""Workday/SmartRecruiters/Workable browser-assist hardening (2026-08-22):
unit tests for browser_runtime._LiveSession._record_workday_attempt /
_do_discover's auto-recording wrap -- no real browser required (mirrors
tests/test_browser_runtime_unit.py's own approach of constructing
_LiveSession directly and never calling .run()/opening a real page)."""

import pytest

from app.applications import browser_runtime, workday_tenant
from app.applications.browser_runtime import DiscoveryOutcome, _LiveSession


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _session(provider: str, url: str) -> _LiveSession:
    live = _LiveSession("sess-1", provider, url)
    return live


def _close(live: _LiveSession) -> None:
    live.executor.shutdown(wait=False)


def test_record_workday_attempt_writes_a_row_for_recognized_tenant():
    live = _session("workday", "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Engineer_R-1234")
    try:
        outcome = DiscoveryOutcome(
            pause_reason=None, current_url=live.application_url, fields=[{"type": "text"}],
            stage="APPLICATION_FORM", entry_detection_result="FORM_ALREADY_VISIBLE",
        )
        live._record_workday_attempt(outcome)
        attempts = workday_tenant.list_attempts("acme", "External")
        assert len(attempts) == 1
        assert attempts[0]["result"] == "FORM_ALREADY_VISIBLE"
        assert attempts[0]["requisition_id"] == "R-1234"
        assert attempts[0]["notes"] == "auto-recorded by browser_runtime._do_discover"
    finally:
        _close(live)


def test_record_workday_attempt_uses_pause_reason_when_present():
    live = _session("workday", "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Engineer_R-1234")
    try:
        outcome = DiscoveryOutcome(pause_reason="LOGIN_REQUIRED", current_url=live.application_url)
        live._record_workday_attempt(outcome)
        attempts = workday_tenant.list_attempts("acme", "External")
        assert attempts[0]["result"] == "LOGIN_REQUIRED"
    finally:
        _close(live)


def test_record_workday_attempt_never_raises_on_bad_input():
    """Best-effort -- a DB error or malformed URL must never propagate into
    a real discovery pass."""
    live = _session("workday", "not a valid url at all")
    try:
        outcome = DiscoveryOutcome(pause_reason=None, current_url="")
        live._record_workday_attempt(outcome)  # must not raise
    finally:
        _close(live)


def test_do_discover_wrapper_records_for_workday_but_not_other_providers(monkeypatch):
    canned = DiscoveryOutcome(pause_reason=None, current_url="https://acme.wd5.myworkdayjobs.com/External/job/x_R-9",
                               fields=[], stage="LANDING_PAGE")
    live = _session("workday", "https://acme.wd5.myworkdayjobs.com/External/job/x_R-9")
    try:
        monkeypatch.setattr(live, "_do_discover_impl", lambda: canned)
        result = live._do_discover()
        assert result is canned
        assert len(workday_tenant.list_attempts("acme", "External")) == 1
    finally:
        _close(live)

    other = _session("workable", "https://apply.workable.com/acme/j/ABC/")
    try:
        monkeypatch.setattr(other, "_do_discover_impl", lambda: canned)
        other._do_discover()
        # workable is not a tenant-shaped provider -- never recorded here
        assert workday_tenant.list_observations() == [] or all(
            row["tenant"] != "" for row in workday_tenant.list_observations()
        )
        assert len(workday_tenant.list_attempts("acme", "External")) == 1  # unchanged from the workday session above
    finally:
        _close(other)
