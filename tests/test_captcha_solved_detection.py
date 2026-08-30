"""Reliable Human-Handoff V1: real-Chromium tests proving CAPTCHA detection
distinguishes a still-blocking widget from a genuinely solved one. A real
live handoff against Robinhood's Greenhouse posting caught this bug: the
widget's own container element (`<div class="g-recaptcha">`) never
disappears once rendered, whether the challenge is still blocking or a
human already solved it in the visible browser -- the old presence-only
check kept reporting CAPTCHA_PRESENT even after the human genuinely solved
it, which drove calling code to treat a solved challenge as still-blocking.
Marked `browser`; every URL is a local `file://` fixture, never a real
website. No test here ever submits a form."""

import pytest

from app import config
from app.applications import browser_runtime
from tests.browser_fixtures import (
    DEFAULT_JOB_COMPANY,
    DEFAULT_JOB_TITLE,
    captcha_page_with_identity,
    invisible_recaptcha_badge_page,
)

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


# --- regression: an unsolved captcha must still pause, exactly as before ---

def test_unsolved_recaptcha_still_pauses(tmp_path):
    url = captcha_page_with_identity(tmp_path, solved=False, response_field="g-recaptcha-response")
    session_id = "t-captcha-unsolved"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason == "CAPTCHA_PRESENT"
    finally:
        browser_runtime.close_session(session_id)


def test_widget_present_with_no_response_field_at_all_still_pauses(tmp_path):
    """A custom/unrecognized challenge type (no standard response token
    field at all) must never be treated as solved -- this is the "never
    invent a pass when no such token exists" half of the fix."""
    from tests.browser_fixtures import _jsonld_block, _write
    import textwrap

    body = _jsonld_block() + textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text">
          <div class="g-recaptcha" data-sitekey="fake-test-key">Please verify you are human.</div>
          <button type="submit">Submit Application</button>
        </form>
    """)
    url = _write(tmp_path, "captcha_no_response_field.html", body)
    session_id = "t-captcha-no-token"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason == "CAPTCHA_PRESENT"
    finally:
        browser_runtime.close_session(session_id)


# --- the actual fix: a genuinely solved challenge must NOT pause -----------

def test_solved_recaptcha_does_not_pause(tmp_path):
    url = captcha_page_with_identity(tmp_path, solved=True, response_field="g-recaptcha-response")
    session_id = "t-captcha-solved-recaptcha"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


def test_solved_hcaptcha_does_not_pause(tmp_path):
    url = captcha_page_with_identity(tmp_path, solved=True, response_field="h-captcha-response")
    session_id = "t-captcha-solved-hcaptcha"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


def test_solved_turnstile_does_not_pause(tmp_path):
    url = captcha_page_with_identity(tmp_path, solved=True, response_field="cf-turnstile-response")
    session_id = "t-captcha-solved-turnstile"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


# --- repeated rediscovery after solving stays consistently unblocked ------
# (mirrors what happens across a real multi-minute human-handoff wait,
# where the caller polls/rediscovers repeatedly).

def test_repeated_rediscovery_after_solve_remains_unblocked(tmp_path):
    url = captcha_page_with_identity(tmp_path, solved=True, response_field="g-recaptcha-response")
    session_id = "t-captcha-repeated"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
        for _ in range(3):
            outcome = browser_runtime.rediscover(session_id)
            assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


# --- an empty-but-present response field is correctly treated as unsolved -

def test_empty_response_field_present_still_pauses(tmp_path):
    url = captcha_page_with_identity(tmp_path, solved=False, response_field="g-recaptcha-response")
    session_id = "t-captcha-empty-token"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason == "CAPTCHA_PRESENT"
    finally:
        browser_runtime.close_session(session_id)


# --- invisible reCAPTCHA's permanent, non-interactive badge must NEVER
# be treated as a blocking challenge -- the real live false positive this
# whole feature was built to fix. Enterprise's suffixed response-token id
# (g-recaptcha-response-100000) is exercised here too. ---

def test_invisible_recaptcha_badge_alone_does_not_pause(tmp_path):
    url = invisible_recaptcha_badge_page(tmp_path, with_genuine_challenge=False)
    session_id = "t-badge-only"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


def test_invisible_recaptcha_badge_repeated_rediscovery_stays_unblocked(tmp_path):
    """Mirrors what a real multi-minute human-handoff wait produces --
    the badge alone must never start blocking on a later poll either."""
    url = invisible_recaptcha_badge_page(tmp_path, with_genuine_challenge=False)
    session_id = "t-badge-repeated"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
        for _ in range(3):
            outcome = browser_runtime.rediscover(session_id)
            assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


# --- the badge must never mask a GENUINE challenge rendered alongside it --

def test_invisible_badge_never_masks_a_genuine_challenge_present_alongside_it(tmp_path):
    url = invisible_recaptcha_badge_page(tmp_path, with_genuine_challenge=True)
    session_id = "t-badge-plus-real-challenge"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason == "CAPTCHA_PRESENT"
    finally:
        browser_runtime.close_session(session_id)
