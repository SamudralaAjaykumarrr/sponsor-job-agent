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


# --- autonomous-ux-reliability-v1 section D: orphan-Chromium-process fix ---
# open_session()'s except-branch calls _discard() when _do_open() raises
# AFTER browser.launch() already succeeded (e.g. a navigation timeout) --
# _discard() must still tear down the already-launched browser/context/
# playwright driver, never just drop the Python reference and leak the OS
# process. These tests verify that via a real ThreadPoolExecutor (cheap) and
# fake browser/context/playwright objects, without ever launching a real
# Chromium.


def _fake_live_session(session_id: str) -> "browser_runtime._LiveSession":
    import unittest.mock as mock
    from concurrent.futures import ThreadPoolExecutor

    live = browser_runtime._LiveSession(session_id, "mock_ats", "https://x")
    live.browser = mock.Mock(name="browser")
    live.context = mock.Mock(name="context")
    live._pw_cm = mock.MagicMock(name="pw_cm")
    # Real executor (not mocked) -- the fix's correctness hinges on the
    # close call actually running on this session's own dedicated thread.
    live.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"test-{session_id}")
    return live


def test_discard_closes_already_launched_browser_not_just_drops_it():
    live = _fake_live_session("leak-check-1")
    browser_runtime._REGISTRY["leak-check-1"] = live
    try:
        browser_runtime._discard("leak-check-1")
        live.executor.shutdown(wait=True)  # ensure the submitted close finished before asserting
    finally:
        browser_runtime._REGISTRY.pop("leak-check-1", None)

    live.context.close.assert_called_once()
    live.browser.close.assert_called_once()
    live._pw_cm.__exit__.assert_called_once()
    assert "leak-check-1" not in browser_runtime._REGISTRY


def test_open_session_failure_after_launch_still_closes_browser(monkeypatch):
    """Simulates the real regression: _do_open() raises (e.g. goto() timed
    out) after browser.launch() already succeeded inside it -- open_session()
    must propagate the error but the already-launched browser must still be
    torn down via _discard(), not orphaned."""
    import unittest.mock as mock

    monkeypatch.setattr(browser_runtime, "playwright_available", lambda: True)

    created: dict = {}

    def _fake_do_open(self, url):
        # Mirrors the real _do_open: launches (creates) the browser/context
        # THEN fails (e.g. goto() timeout) -- the browser object already
        # exists and must be cleaned up despite the raise.
        self.browser = mock.Mock(name="browser")
        self.context = mock.Mock(name="context")
        self._pw_cm = mock.MagicMock(name="pw_cm")
        created["browser"] = self.browser
        created["context"] = self.context
        created["pw_cm"] = self._pw_cm
        raise TimeoutError("simulated goto() timeout")

    monkeypatch.setattr(browser_runtime._LiveSession, "_do_open", _fake_do_open)

    with pytest.raises(Exception):
        browser_runtime.open_session("leak-check-2", provider="mock_ats", url="https://x")

    # Give the background thread a moment to run the submitted close (it was
    # queued behind the failed _do_open call on the same thread).
    import time as _time

    for _ in range(50):
        if created.get("browser") is not None and created["browser"].close.called:
            break
        _time.sleep(0.02)

    assert "leak-check-2" not in browser_runtime._REGISTRY
    created["browser"].close.assert_called_once()
    created["context"].close.assert_called_once()
    created["pw_cm"].__exit__.assert_called_once()
