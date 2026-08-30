"""Reliable Human-Handoff V1: real-Chromium tests proving MFA-phrase
detection scans only VISIBLE page text, not raw HTML source. A real live
handoff against Robinhood's Greenhouse posting caught this bug: "2fa"
(one of app.applications.browser_runtime._MFA_PHRASES) matched inside an
unrelated Google API proxy iframe's own hashed URL fragment -- present in
page.content() but nowhere in the visible page text a person would ever
read -- which paused the canary as MFA_REQUIRED with no actual
authentication challenge anywhere on the page. This is the same class of
bug (a raw-HTML-source substring scan matching unrelated script/URL text)
the project's own Phase 13 CAPTCHA fix already established a fix for.
Marked `browser`; every URL is a local `file://` fixture, never a real
website. No test here ever submits a form."""

import pytest

from app import config
from app.applications import browser_runtime
from tests.browser_fixtures import (
    DEFAULT_JOB_COMPANY,
    DEFAULT_JOB_TITLE,
    greenhouse_like_otp_page,
    mfa_phrase_false_positive_page,
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


def test_mfa_phrase_buried_in_unrelated_iframe_url_does_not_pause(tmp_path):
    url = mfa_phrase_false_positive_page(tmp_path)
    session_id = "t-mfa-false-positive"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


def test_mfa_false_positive_repeated_rediscovery_stays_unblocked(tmp_path):
    url = mfa_phrase_false_positive_page(tmp_path)
    session_id = "t-mfa-false-positive-repeated"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason is None
        for _ in range(3):
            outcome = browser_runtime.rediscover(session_id)
            assert outcome.pause_reason is None
    finally:
        browser_runtime.close_session(session_id)


# --- regression: a genuinely VISIBLE MFA challenge must still pause -------

def test_genuine_visible_otp_challenge_still_pauses(tmp_path):
    url = greenhouse_like_otp_page(tmp_path)
    session_id = "t-mfa-genuine"
    try:
        outcome = _open(session_id, url)
        assert outcome.pause_reason == "MFA_REQUIRED"
    finally:
        browser_runtime.close_session(session_id)
