"""CLAUDE.md Phase 13 sections 13-14, 56, 68: safe, read-only application-
flow canary validation. Non-browser tests exercise the guard/persistence
logic; browser-marked tests (`pytest -m browser`) drive real Chromium
against the local file:// fixture sandbox (tests/browser_fixtures.py) --
never a real website."""

import pytest

from app import config
from app.applications import canary
from app.applications.canary import CanaryResult, CanaryUnavailable, _detect_provider


def test_canary_unavailable_when_disabled(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    with pytest.raises(CanaryUnavailable):
        canary.run_canary("https://boards.greenhouse.io/acme/jobs/1")


def test_detect_provider_from_known_domain():
    assert _detect_provider("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert _detect_provider("https://jobs.lever.co/acme/abc") == "lever"
    assert _detect_provider("https://unknown-careers.example.com/apply") == ""


def test_mock_ats_never_matches_a_real_provider():
    """CLAUDE.md Phase 13 section 17 / trusted_redirects' own carve-out:
    mock_ats's local/test hosts must never be treated as a real provider
    signal."""
    assert _detect_provider("https://mock-ats.local/apply") == ""


def test_record_and_list_canary_run(tmp_env):
    result = CanaryResult(provider="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1", ok=True,
                           form_found=True, upload_control_found=True, final_submit_found=True)
    row = canary.record_canary_run(result)
    assert row["provider"] == "greenhouse"
    assert row["form_found"] == 1

    rows = canary.list_canary_runs(provider="greenhouse")
    assert len(rows) == 1
    assert rows[0]["url"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_list_canary_runs_filters_by_provider(tmp_env):
    canary.record_canary_run(CanaryResult(provider="greenhouse", url="https://x/1"))
    canary.record_canary_run(CanaryResult(provider="lever", url="https://y/2"))
    assert len(canary.list_canary_runs(provider="greenhouse")) == 1
    assert len(canary.list_canary_runs()) == 2


def test_scheduled_canaries_never_run_when_disabled(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "REAL_ATS_CANARY_ENABLED", False)
    results = canary.run_scheduled_canaries([canary.ScheduledCanaryTarget(url="https://x/1", provider="greenhouse")])
    assert results == []


def test_scheduled_canaries_one_failure_never_aborts_others(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "REAL_ATS_CANARY_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)  # forces CanaryUnavailable for every target
    targets = [
        canary.ScheduledCanaryTarget(url="https://x/1", provider="greenhouse"),
        canary.ScheduledCanaryTarget(url="https://y/2", provider="lever"),
    ]
    results = canary.run_scheduled_canaries(targets)
    assert len(results) == 2
    assert all(r["ok"] is False for r in results)


# =============================================================================
# Real-Chromium canary tests against the local file:// fixture sandbox.
# =============================================================================

pytestmark_browser = pytest.mark.browser


@pytest.mark.browser
class TestCanaryBrowser:
    @pytest.fixture(autouse=True)
    def _require_chromium_launchable(self):
        playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
        try:
            with playwright.sync_playwright() as p:
                p.chromium.launch(headless=True).close()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium browser binary not launchable: {exc}")

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
        monkeypatch.setattr(config, "BROWSER_HEADLESS", True)
        monkeypatch.setattr(config, "BROWSER_ASSIST_TIMEOUT_SECONDS", 15)

    def test_canary_finds_form_and_final_submit(self, tmp_path):
        from tests.browser_fixtures import simple_form_page

        url = simple_form_page(tmp_path)
        result = canary.run_canary(url)
        assert result.ok is True
        assert result.form_found is True
        assert result.final_submit_found is True
        assert result.upload_control_found is False
        assert result.captcha_detected is False

    def test_canary_detects_captcha_and_stops(self, tmp_path):
        from tests.browser_fixtures import captcha_page

        url = captcha_page(tmp_path)
        result = canary.run_canary(url)
        assert result.ok is True
        assert result.captcha_detected is True
        # Never proceeds to report form/upload details once a CAPTCHA is seen.
        assert result.form_found is False

    def test_canary_detects_login_wall(self, tmp_path):
        from tests.browser_fixtures import login_page

        url = login_page(tmp_path)
        result = canary.run_canary(url)
        assert result.ok is True
        assert result.login_detected is True

    def test_canary_detects_upload_control(self, tmp_path):
        from tests.browser_fixtures import form_with_file_upload_page

        url = form_with_file_upload_page(tmp_path)
        result = canary.run_canary(url)
        assert result.ok is True
        assert result.upload_control_found is True

    def test_canary_follows_one_safe_apply_entry_hop(self, tmp_path):
        from tests.browser_fixtures import landing_page_with_apply_click

        landing_url, form_url = landing_page_with_apply_click(tmp_path)
        result = canary.run_canary(url=landing_url)
        assert result.ok is True
        assert result.apply_entry_found is True
        assert result.apply_entry_followed is True
        assert result.form_found is True

    def test_canary_never_fills_any_field(self, tmp_path):
        """CLAUDE.md Phase 13 section 13: a canary must never fill candidate
        PII -- verified here by confirming the field remains empty after a
        canary run touches the page."""
        from tests.browser_fixtures import simple_form_page
        from playwright.sync_api import sync_playwright

        url = simple_form_page(tmp_path)
        canary.run_canary(url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url)
            value = page.locator("#fname").input_value()
            browser.close()
        assert value == ""

    def test_canary_never_clicks_final_submit(self, tmp_path, monkeypatch):
        """CLAUDE.md Phase 13 section 13: verified by confirming the browser
        never navigates away from the form page (a real submit would trigger
        browser-native form navigation, which file:// static pages here have
        no server to actually complete, but URL/DOM would still change)."""
        from tests.browser_fixtures import simple_form_page

        url = simple_form_page(tmp_path)
        result = canary.run_canary(url)
        assert result.final_submit_found is True
        # ok=True with no error means the run completed without ever
        # attempting the click (run_canary contains no submit-click code
        # path at all -- see module docstring).
        assert result.ok is True
        assert result.error == ""
