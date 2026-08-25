"""Real-browser end-to-end coverage of the Apply/Automation Settings V1
page -- marked `browser`, skipped automatically unless Playwright AND its
Chromium binary are actually launchable (`pytest -m browser`), mirroring
tests/test_premium_ui_playwright.py's exact fixtures/conventions."""

import contextlib
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _require_chromium_launchable():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    try:
        with playwright.sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium browser binary not launchable: {exc}")


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_env, sample_profile):
    import uvicorn

    from app.candidate.profile import save_profile
    from app.main import app

    save_profile(sample_profile)

    port = _free_port()
    server_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        pytest.fail("live server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        yield pg
        ctx.close()
        browser.close()


def test_settings_page_loads_all_sections(live_server, page):
    page.goto(live_server + "/settings")
    assert page.locator("h1").first.inner_text() == "Apply & Automation Settings"
    body_text = page.locator("body").inner_text()
    for heading in ("Resume", "Cover Letter", "Submission", "Application Limits",
                    "Job Preferences", "Sponsorship & Work Authorization", "Advanced Safety"):
        assert heading in body_text


def test_resume_settings_persist_across_reload(live_server, page):
    page.goto(live_server + "/settings")
    page.check('input[name="resume_optimization_mode"][value="AGGRESSIVE"]')
    page.click('form[action="/settings/resume"] button[type=submit]')
    page.wait_for_load_state("networkidle")
    assert "Settings saved." in page.content()

    page.goto(live_server + "/settings")
    assert page.is_checked('input[name="resume_optimization_mode"][value="AGGRESSIVE"]')


def test_auto_submit_requires_confirmation_interaction(live_server, page):
    """Playwright coverage of the high-risk confirmation flow (Apply/
    Automation Settings V1 section 10): selecting Auto-submit and saving
    WITHOUT checking the confirmation box must not persist it; checking the
    box and re-submitting must."""
    page.goto(live_server + "/settings")
    page.check('form[action="/settings/submission"] input[value="AUTO_SUBMIT"]')
    page.click('form[action="/settings/submission"] button:has-text("Save submission settings")')
    page.wait_for_load_state("networkidle")

    assert "Confirm turning on Auto-submit" in page.content()
    assert "REVIEW BEFORE SUBMIT" in page.content()

    page.check('input[name="confirm_auto_submit"]')
    page.click('button:has-text("Confirm Auto-submit")')
    page.wait_for_load_state("networkidle")
    assert "Settings saved." in page.content()
    assert "AUTO-SUBMIT" in page.content()


def test_review_vs_auto_submit_current_state_label_updates(live_server, page):
    page.goto(live_server + "/settings")
    assert "REVIEW BEFORE SUBMIT" in page.content()

    page.check('form[action="/settings/submission"] input[value="AUTO_SUBMIT"]')
    page.click('form[action="/settings/submission"] button:has-text("Save submission settings")')
    page.wait_for_load_state("networkidle")
    page.check('input[name="confirm_auto_submit"]')
    page.click('button:has-text("Confirm Auto-submit")')
    page.wait_for_load_state("networkidle")
    assert "AUTO-SUBMIT" in page.content()

    # Switching back to Review never needs confirmation.
    page.check('form[action="/settings/submission"] input[value="REVIEW"]')
    page.click('form[action="/settings/submission"] button:has-text("Save submission settings")')
    page.wait_for_load_state("networkidle")
    assert "REVIEW BEFORE SUBMIT" in page.content()
    assert "Confirm turning on Auto-submit" not in page.content()


def test_application_limits_and_job_preferences_forms_save(live_server, page):
    page.goto(live_server + "/settings")
    page.fill("#set-max_applications_per_day", "7")
    page.click('form[action="/settings"] button:has-text("Save application limits")')
    page.wait_for_load_state("networkidle")
    assert page.input_value("#set-max_applications_per_day") == "7"

    page.fill("#pref-excluded", "staff, principal")
    page.click('form[action="/settings/preferences"] button[type=submit]')
    page.wait_for_load_state("networkidle")
    assert page.input_value("#pref-excluded") == "staff, principal"
