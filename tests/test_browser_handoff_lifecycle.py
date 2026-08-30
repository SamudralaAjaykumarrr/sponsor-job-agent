"""Reliable Human-Handoff V1: proves (rather than assumes) the properties a
multi-minute human handoff (CAPTCHA, login, an unknown legal question) needs
from the existing browser_runtime/browser_session architecture -- that a
live session's process ownership is NOT tied to any single top-level call
returning, and that no automatic time-based cleanup ever runs without an
explicit, deliberate call to expire_stale_sessions(). This was investigated
live: a real handoff against Robinhood's Greenhouse posting appeared to
"die" while waiting for a human to solve a CAPTCHA. The actual, PROVEN root
cause (see tests/test_captcha_solved_detection.py) was a false-positive
CAPTCHA re-detection driving a short-lived diagnostic script to exit --
process exit is what killed the pipe-connected Chromium, not any spontaneous
timeout/cleanup in this module. These tests exist to keep that property
true and provable going forward, not to add a new mechanism -- the existing
architecture already satisfies it. Marked `browser`; every URL is a local
`file://` fixture. No test here ever submits a form."""

import random

import pytest

from app import config
from app.applications import browser_runtime, browser_session
from tests.browser_fixtures import DEFAULT_JOB_COMPANY, DEFAULT_JOB_TITLE, captcha_page_with_identity

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _require_chromium_launchable():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    try:
        with playwright.sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium browser binary not launchable: {exc}")


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_HEADLESS", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_TIMEOUT_SECONDS", 15)


def _open(session_id: str, url: str):
    return browser_runtime.open_session(
        session_id, provider="greenhouse", url=url,
        expected_title=DEFAULT_JOB_TITLE, expected_company=DEFAULT_JOB_COMPANY,
    )


def _open_with_db_row(url: str) -> str:
    """Like _open(), but ALSO creates the real browser_assist_sessions DB
    row first (matching how app.applications.browser_assist.start_session()
    actually orchestrates a session) -- needed for tests that exercise
    expire_stale_sessions(), which reads that table, not the in-process
    browser_runtime registry. A random job_id avoids colliding with the
    partial UNIQUE(job_id) WHERE active=1 index against rows any PRIOR test
    run left active (this file's tests close the browser_runtime registry
    entry but deliberately leave the DB row's terminal status intact for
    their own assertions, so a fixed/sequential id can collide across
    separate pytest invocations sharing the same real database)."""
    row = browser_session.create_session(
        execution_id="t-exec-handoff", job_id=random.randint(10_000_000, 99_999_999),
        provider="greenhouse", application_url=url,
    )
    session_id = row["session_id"]
    browser_runtime.open_session(
        session_id, provider="greenhouse", url=url,
        expected_title=DEFAULT_JOB_TITLE, expected_company=DEFAULT_JOB_COMPANY,
    )
    return session_id


def _do_unrelated_work():
    """Stands in for "the automation worker/request handler returns" --
    completely unrelated work happening in the SAME process, proving the
    live session isn't tied to any one call's stack frame."""
    return sum(i * i for i in range(1000))


# --- 1/2: CAPTCHA handoff enters a paused state; the browser stays alive
# while unrelated work happens in between (simulating a caller yielding). ---

def test_captcha_handoff_pauses_and_session_survives_unrelated_work_between_calls(tmp_path, tmp_env):
    url = captcha_page_with_identity(tmp_path, solved=False)
    session_id = "t-handoff-survives"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason == "CAPTCHA_PRESENT"
        assert browser_runtime.is_live(session_id) is True

        # Simulate the calling worker/request handler returning and doing
        # other, unrelated things -- exactly what a real FastAPI request
        # handler completing (and the server moving on to other requests)
        # looks like from this module's point of view.
        for _ in range(5):
            _do_unrelated_work()

        assert browser_runtime.is_live(session_id) is True
        # The SAME browser (not a reconstruction) can still be interacted
        # with -- rediscovery still works against the live connection.
        outcome2 = browser_runtime.rediscover(session_id)
        assert outcome2.pause_reason == "CAPTCHA_PRESENT"  # still unsolved, correctly
    finally:
        browser_runtime.close_session(session_id)


# --- 3: a "temporary helper" (an ordinary function call, mirroring what a
# disposable script's main() does) completing does NOT close the session --
# only close_session()/process exit does. ---

def test_helper_function_returning_does_not_close_the_session(tmp_path, tmp_env):
    url = captcha_page_with_identity(tmp_path, solved=False)
    session_id = "t-helper-returns"

    def _helper_that_opens_and_returns():
        return _open(session_id, url)

    try:
        outcome = _helper_that_opens_and_returns()  # the "helper" has now returned
        assert outcome.pause_reason == "CAPTCHA_PRESENT"
        # Nothing about the helper returning tore the session down.
        assert browser_runtime.is_live(session_id) is True
        assert browser_runtime.rediscover(session_id).pause_reason == "CAPTCHA_PRESENT"
    finally:
        browser_runtime.close_session(session_id)


# --- 4: a simulated extended human wait (many rediscovery polls, mirroring
# what a real multi-minute wait looks like from the caller's side) never
# triggers cleanup on its own -- only an explicit expire_stale_sessions()
# call, with a real elapsed-time cutoff, can ever do that. ---

def test_extended_wait_never_triggers_automatic_cleanup(tmp_path, tmp_env):
    url = captcha_page_with_identity(tmp_path, solved=False)
    session_id = _open_with_db_row(url)
    try:
        # Many repeated polls, as a real multi-minute wait would produce --
        # none of this, by itself, ever calls expire_stale_sessions().
        for _ in range(10):
            outcome = browser_runtime.rediscover(session_id)
            assert outcome.pause_reason == "CAPTCHA_PRESENT"
        assert browser_runtime.is_live(session_id) is True
        row = browser_session.get_session(session_id)
        assert row is not None and row["active"] == 1
        assert row["status"] != browser_session.BrowserSessionStatus.EXPIRED.value
    finally:
        browser_runtime.close_session(session_id)
        browser_session.close_session(session_id)


# --- explicit reaping still works correctly for a GENUINELY abandoned
# session (never called again) once the operator deliberately runs it. ---

def test_expire_stale_sessions_is_explicit_never_automatic(tmp_path, tmp_env):
    from app.applications import browser_assist

    url = captcha_page_with_identity(tmp_path, solved=False)
    session_id = _open_with_db_row(url)
    try:
        # A short timeout window would immediately mark this expired IF
        # the reaper ran automatically -- it must not, until called.
        assert browser_session.get_session(session_id)["active"] == 1
        expired_before_call = browser_assist.expire_stale_sessions()
        assert session_id not in {r["session_id"] for r in expired_before_call}
        assert browser_session.get_session(session_id)["active"] == 1

        # Now genuinely make it stale (activity far in the past) and prove
        # the SAME explicit call correctly reaps it -- cleanup still works,
        # it just never fires on its own.
        from app.db import db_session

        with db_session() as conn:
            conn.execute(
                "UPDATE browser_assist_sessions SET last_activity_at = '2000-01-01T00:00:00+00:00' "
                "WHERE session_id = ?", (session_id,),
            )
        expired_after = browser_assist.expire_stale_sessions()
        assert session_id in {r["session_id"] for r in expired_after}
        row = browser_session.get_session(session_id)
        assert row["status"] == browser_session.BrowserSessionStatus.EXPIRED.value
    finally:
        try:
            browser_runtime.close_session(session_id)
        except Exception:  # noqa: BLE001
            pass


# --- explicit close still works (Stop Agent / operator-intended close). ---

def test_explicit_close_session_still_works(tmp_path, tmp_env):
    url = captcha_page_with_identity(tmp_path, solved=False)
    session_id = "t-explicit-close"
    _open(session_id, url)
    assert browser_runtime.is_live(session_id) is True
    browser_runtime.close_session(session_id)
    assert browser_runtime.is_live(session_id) is False
