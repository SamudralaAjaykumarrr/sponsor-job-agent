"""Unit tests for app.applications.browser_runtime that do NOT require a
real browser -- pure helper functions, the concurrency guard, and the
registry lifecycle (using a fake in-registry placeholder instead of a real
Playwright browser). Real-Chromium-driven tests live in
tests/test_browser_assist_e2e.py (marked `browser`)."""

import pytest

from app import config
from app.applications import browser_runtime


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)


def test_disabled_flag_raises_before_touching_registry(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    with pytest.raises(browser_runtime.BrowserRuntimeUnavailable):
        browser_runtime.open_session("s1", provider="mock_ats", url="https://x")
    assert browser_runtime.active_count() == 0


def test_playwright_not_installed_raises(monkeypatch):
    monkeypatch.setattr(browser_runtime, "playwright_available", lambda: False)
    with pytest.raises(browser_runtime.BrowserRuntimeUnavailable):
        browser_runtime.open_session("s1", provider="mock_ats", url="https://x")


def test_concurrency_bound_raises_busy(monkeypatch):
    """CLAUDE.md Phase 10 section 45: browser sessions are expensive/bounded
    -- verified here purely via the registry guard, without ever launching a
    real browser."""
    monkeypatch.setattr(browser_runtime, "playwright_available", lambda: True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_CONCURRENCY", 1)

    class _FakeLive:
        pass

    browser_runtime._REGISTRY["already-open"] = _FakeLive()
    try:
        with pytest.raises(browser_runtime.BrowserRuntimeBusy):
            browser_runtime.open_session("s2", provider="mock_ats", url="https://x")
    finally:
        browser_runtime._REGISTRY.pop("already-open", None)


def test_is_live_and_active_count_reflect_registry():
    class _FakeLive:
        pass

    assert not browser_runtime.is_live("abc")
    assert browser_runtime.active_count() == 0
    browser_runtime._REGISTRY["abc"] = _FakeLive()
    try:
        assert browser_runtime.is_live("abc")
        assert browser_runtime.active_count() == 1
    finally:
        browser_runtime._REGISTRY.pop("abc", None)


def test_get_live_raises_unavailable_for_unknown_session():
    with pytest.raises(browser_runtime.BrowserRuntimeUnavailable):
        browser_runtime.rediscover("never-existed")


def test_close_session_on_unknown_session_is_a_safe_no_op():
    browser_runtime.close_session("never-existed")  # must not raise


def test_selector_for_prefers_id_then_name_then_nth_match():
    assert browser_runtime._selector_for({"id": "abc", "name": "x", "index": 0}) == "#abc"
    assert browser_runtime._selector_for({"id": "", "name": "email", "index": 0}) == "[name='email']"
    assert browser_runtime._selector_for({"id": "", "name": "", "index": 3}) == ":nth-match(input, textarea, select, 4)"


def test_decline_option_matches_known_phrases():
    choices = ["I am a veteran", "I am not a veteran", "I don't wish to answer"]
    assert browser_runtime._decline_option(choices) == "I don't wish to answer"
    assert browser_runtime._decline_option(["Yes", "No"]) is None


def test_fingerprint_fields_is_deterministic_for_identical_input():
    fields = [
        {"name": "email", "label": "Email", "type": "text", "required": True, "choices": []},
        {"name": "full_name", "label": "Full Name", "type": "text", "required": True, "choices": []},
    ]
    assert browser_runtime._fingerprint_fields(fields) == browser_runtime._fingerprint_fields(list(fields))
    assert browser_runtime._fingerprint_fields(fields) != browser_runtime._fingerprint_fields([])


def test_fingerprint_fields_changes_when_a_field_is_added():
    base = [{"name": "email", "label": "Email", "type": "text", "required": True, "choices": []}]
    extra = base + [{"name": "phone", "label": "Phone", "type": "text", "required": False, "choices": []}]
    assert browser_runtime._fingerprint_fields(base) != browser_runtime._fingerprint_fields(extra)
