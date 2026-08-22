"""CLAUDE.md Phase 10 sections 4-8, 50, 63: browser-assist session
persistence, lifecycle, distributed-ownership leasing, and stale-session
reaping. Pure DB-layer tests -- no Playwright/browser involved."""

import time

import pytest

from app.applications import browser_session


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def test_create_and_get_session():
    row = browser_session.create_session(
        execution_id="exec_1", job_id=1, provider="greenhouse", application_url="https://boards.greenhouse.io/x/1",
    )
    assert row["session_id"].startswith("bsess_")
    assert row["status"] == browser_session.BrowserSessionStatus.STARTING.value
    assert row["active"] == 1

    fetched = browser_session.get_session(row["session_id"])
    assert fetched["execution_id"] == "exec_1"
    assert fetched["job_id"] == 1


def test_duplicate_active_session_for_same_job_is_rejected():
    browser_session.create_session(execution_id="exec_1", job_id=42, provider="lever", application_url="https://x")
    with pytest.raises(browser_session.DuplicateSessionError):
        browser_session.create_session(execution_id="exec_2", job_id=42, provider="lever", application_url="https://y")


def test_active_session_lookup_by_job():
    row = browser_session.create_session(execution_id="exec_1", job_id=7, provider="ashby", application_url="https://x")
    found = browser_session.get_active_session_for_job(7)
    assert found["session_id"] == row["session_id"]
    assert browser_session.get_active_session_for_job(999) is None


def test_update_to_terminal_status_flips_active_to_zero():
    row = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    updated = browser_session.update_session(row["session_id"], status=browser_session.BrowserSessionStatus.CLOSED.value)
    assert updated["active"] == 0
    assert updated["closed_at"]
    # A new session for the same job can now be created since the old one is terminal.
    browser_session.create_session(execution_id="exec_2", job_id=1, provider="lever", application_url="https://y")


def test_update_to_paused_status_keeps_active():
    row = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    updated = browser_session.update_session(
        row["session_id"], status=browser_session.BrowserSessionStatus.PAUSED_CAPTCHA.value, needs_user_action=1,
    )
    assert updated["active"] == 1
    assert updated["status"] == "PAUSED_CAPTCHA"


def test_submission_status_unknown_stays_active_like_execution_model():
    """CLAUDE.md Phase 10 section 51 / Phase 8's existing rule: an
    unresolved outcome is never silently discarded -- it stays active,
    blocking a second concurrent session for the same job until a human
    reconciles it."""
    row = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    updated = browser_session.update_session(
        row["session_id"], status=browser_session.BrowserSessionStatus.SUBMISSION_STATUS_UNKNOWN.value,
        needs_user_action=1,
    )
    assert updated["active"] == 1
    with pytest.raises(browser_session.DuplicateSessionError):
        browser_session.create_session(execution_id="exec_2", job_id=1, provider="lever", application_url="https://y")


def test_touch_activity_updates_timestamp():
    row = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    first = row["last_activity_at"]
    time.sleep(0.01)
    browser_session.touch_activity(row["session_id"])
    updated = browser_session.get_session(row["session_id"])
    assert updated["last_activity_at"] >= first


def test_claim_and_release_lease():
    row = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    session_id = row["session_id"]

    claimed = browser_session.claim_session(session_id, worker_id="worker-a", lease_seconds=60)
    assert claimed is not None
    assert claimed["lease_owner"] == "worker-a"

    # A second worker cannot claim the same still-leased session.
    second = browser_session.claim_session(session_id, worker_id="worker-b", lease_seconds=60)
    assert second is None

    browser_session.release_session_lease(session_id)
    reclaimed = browser_session.claim_session(session_id, worker_id="worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed["lease_owner"] == "worker-b"


def test_expired_lease_is_reclaimable_by_a_different_worker():
    row = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    session_id = row["session_id"]
    browser_session.claim_session(session_id, worker_id="worker-a", lease_seconds=-5)  # already expired
    reclaimed = browser_session.claim_session(session_id, worker_id="worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed["lease_owner"] == "worker-b"


def test_expire_stale_sessions_marks_expired_and_frees_job_for_a_new_session():
    row = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    session_id = row["session_id"]
    # Force last_activity_at far into the past.
    browser_session.update_session(session_id, last_activity_at="2000-01-01T00:00:00+00:00")

    expired = browser_session.expire_stale_sessions(timeout_minutes=30)
    assert len(expired) == 1
    assert expired[0]["session_id"] == session_id

    updated = browser_session.get_session(session_id)
    assert updated["status"] == browser_session.BrowserSessionStatus.EXPIRED.value
    assert updated["active"] == 0

    # Never deletes the row -- audit trail preserved.
    assert browser_session.get_session(session_id) is not None
    # Job is now free for a fresh session.
    browser_session.create_session(execution_id="exec_2", job_id=1, provider="lever", application_url="https://y")


def test_expire_stale_sessions_leaves_recently_active_sessions_alone():
    browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    expired = browser_session.expire_stale_sessions(timeout_minutes=30)
    assert expired == []


def test_summarize_counts_by_status():
    s1 = browser_session.create_session(execution_id="exec_1", job_id=1, provider="lever", application_url="https://x")
    s2 = browser_session.create_session(execution_id="exec_2", job_id=2, provider="lever", application_url="https://y")
    browser_session.update_session(s1["session_id"], status=browser_session.BrowserSessionStatus.PAUSED_CAPTCHA.value)
    browser_session.update_session(s2["session_id"], status=browser_session.BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value)

    summary = browser_session.summarize()
    assert summary.paused_captcha == 1
    assert summary.ready_for_submit == 1
    assert summary.active_sessions == 2
