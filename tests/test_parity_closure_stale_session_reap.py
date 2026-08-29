"""Tsenta-parity-closure-v1, P0#1: deterministic regression coverage for the
stale-browser-session-reap bug found live during the Tsenta parity audit
(job 327's Airbnb session sat active=1 indefinitely because the scheduler's
reap branch was gated on the same BROWSER_ASSIST_ENABLED flag that gates
session CREATION). Session creation and session cleanup must be
independently gated -- see app.applications.background_scheduler's module
docstring and app.applications.browser_assist.expire_stale_sessions().

Five deterministic scenarios required by the closure spec, named A-E below.
No job 327 (or any other specific job id) is hard-coded -- every scenario
builds its own synthetic session row. No real browser/Playwright/network
involved anywhere in this file."""

import asyncio

import pytest

import app.applications.background_scheduler as bg_mod
from app import config
from app.applications import browser_assist, browser_session
from app.applications.background_scheduler import ApplicationBackgroundScheduler


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _make_stale_session(job_id: int, execution_id: str = "exec_stale") -> dict:
    row = browser_session.create_session(
        execution_id=execution_id, job_id=job_id, provider="greenhouse",
        application_url=f"https://job-boards.greenhouse.io/fixture/jobs/{job_id}",
    )
    browser_session.update_session(row["session_id"], last_activity_at="2000-01-01T00:00:00+00:00")
    return row


# --- A: stale session + browser assist currently disabled -> reaped -------

def test_a_stale_session_reaped_even_with_browser_assist_currently_disabled(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
    row = _make_stale_session(job_id=101)

    expired = browser_assist.expire_stale_sessions()

    assert [e["session_id"] for e in expired] == [row["session_id"]]
    updated = browser_session.get_session(row["session_id"])
    assert updated["status"] == browser_session.BrowserSessionStatus.EXPIRED.value
    assert updated["active"] == 0


# --- B: stale session + assist enabled -> reaped ---------------------------

def test_b_stale_session_reaped_when_browser_assist_enabled(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
    row = _make_stale_session(job_id=102)

    expired = browser_assist.expire_stale_sessions()

    assert [e["session_id"] for e in expired] == [row["session_id"]]
    assert browser_session.get_session(row["session_id"])["active"] == 0


# --- C: healthy session -> retained -----------------------------------------

@pytest.mark.parametrize("assist_enabled", [True, False])
def test_c_healthy_session_is_not_reaped(monkeypatch, assist_enabled):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", assist_enabled)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
    row = browser_session.create_session(
        execution_id="exec_healthy", job_id=103, provider="lever", application_url="https://jobs.lever.co/fixture/1",
    )

    expired = browser_assist.expire_stale_sessions()

    assert expired == []
    live = browser_session.get_session(row["session_id"])
    assert live["active"] == 1
    assert live["status"] != browser_session.BrowserSessionStatus.EXPIRED.value


# --- D: repeated reaper execution -> idempotent -----------------------------

def test_d_repeated_reap_is_idempotent(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
    row = _make_stale_session(job_id=104)

    first = browser_assist.expire_stale_sessions()
    second = browser_assist.expire_stale_sessions()
    third = browser_assist.expire_stale_sessions()

    assert [e["session_id"] for e in first] == [row["session_id"]]
    assert second == []
    assert third == []
    # Row is untouched by the no-op re-runs -- still exactly one EXPIRED row,
    # never re-flipped, never duplicated, never deleted.
    final = browser_session.get_session(row["session_id"])
    assert final["status"] == browser_session.BrowserSessionStatus.EXPIRED.value
    assert final["active"] == 0


# --- E: restart/reconstruction -> no duplicate execution/session -----------

def test_e_reap_then_fresh_session_reuses_same_execution_identity_without_duplication(monkeypatch):
    """Simulates a process restart after a stale session was reaped: a new
    browser-assist session for the SAME job/execution can be opened cleanly
    (the partial unique index on job_id WHERE active=1 is freed by the
    terminal EXPIRED row), and it is the only active session for that job --
    no duplicate session, and the original execution identity
    (execution_id) is preserved across the reap, never rewritten."""
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
    stale = _make_stale_session(job_id=105, execution_id="exec_shared")

    expired = browser_assist.expire_stale_sessions()
    assert [e["session_id"] for e in expired] == [stale["session_id"]]

    # A fresh session for the same job + same execution identity now succeeds.
    fresh = browser_session.create_session(
        execution_id="exec_shared", job_id=105, provider="greenhouse",
        application_url="https://job-boards.greenhouse.io/fixture/jobs/105",
    )
    assert fresh["session_id"] != stale["session_id"]
    assert fresh["execution_id"] == "exec_shared" == stale["execution_id"]

    # Exactly one active session for the job -- the old one stays terminal,
    # never resurrected, never duplicated.
    active_now = browser_session.get_active_session_for_job(105)
    assert active_now["session_id"] == fresh["session_id"]
    old_row = browser_session.get_session(stale["session_id"])
    assert old_row["active"] == 0
    assert old_row["status"] == browser_session.BrowserSessionStatus.EXPIRED.value

    # A second reap pass (simulating the scheduler firing again after
    # "restart") must not touch the fresh, healthy session.
    second_reap = browser_assist.expire_stale_sessions()
    assert second_reap == []
    assert browser_session.get_session(fresh["session_id"])["active"] == 1


# --- Scheduler-level: the toggle must never gate the reap branch -----------

def _run(coro) -> None:
    asyncio.run(coro())


def test_scheduler_reap_branch_ignores_the_toggle_in_both_directions(monkeypatch):
    """Belt-and-suspenders on top of test_application_background_scheduler.py:
    the reap branch fires on its own cadence whether the flag is True or
    False -- proving cleanup and creation are genuinely decoupled at the
    scheduler-wiring level, not just at the underlying DB-layer function."""
    for assist_enabled in (False, True):
        monkeypatch.setattr(config, "RECONCILE_WORKER_ENABLED", False)
        monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", assist_enabled)
        monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
        calls = []
        monkeypatch.setattr(bg_mod, "_run_stale_session_reap", lambda: calls.append(1))
        monkeypatch.setattr(bg_mod, "_IDLE_POLL_SECONDS", 0.01)

        sched = ApplicationBackgroundScheduler()

        async def go():
            sched.start()
            await asyncio.sleep(0.1)
            await sched.stop()

        _run(go)
        assert calls == [1], f"reap did not fire with BROWSER_ASSIST_ENABLED={assist_enabled}"
