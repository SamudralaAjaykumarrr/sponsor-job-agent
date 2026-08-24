"""Real-browser end-to-end coverage of the application-lifecycle-exception-
resume-v1 Demo / Test Mode experience -- the sanctioned way to visually
exercise Needs Action / Issues / Ready to Apply / Submitted without a real
employer. Marked `browser`, mirroring tests/test_premium_ui_playwright.py's
exact skip-if-unavailable convention (skipped automatically unless
Playwright AND its Chromium binary are genuinely launchable) and its
`live_server` fixture (a real FastAPI instance in a background thread,
using tmp_env's already-isolated config/db paths)."""

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


def _run_demo(page, base_url: str, key: str) -> None:
    page.goto(base_url + "/demo")
    page.click(f'form[action="/demo/{key}/run"] button[type=submit]')
    page.wait_for_load_state("networkidle")


def test_demo_page_loads(live_server, page):
    page.goto(live_server + "/demo")
    assert "Demo / Test Mode" in page.content()
    assert "Demo Successful Application" in page.content()
    assert "Demo Job Expired" in page.content()


def test_needs_action_captcha_flow_resolve_and_ready_to_apply(live_server, page):
    _run_demo(page, live_server, "captcha")
    assert "CAPTCHA required" in page.content()

    page.click('form[action="/demo/captcha/resolve"] button[type=submit]')
    page.wait_for_load_state("networkidle")
    assert "CAPTCHA required" not in page.content()
    assert "SUBMISSION_READY" in page.content()  # execution status shown on the demo card


def test_issues_job_expired_flow(live_server, page):
    _run_demo(page, live_server, "job_expired")
    assert "Job no longer accepting applications" in page.content()
    assert "JOB_NO_LONGER_ACTIVE" in page.content()


def test_ready_to_apply_and_submitted_via_approve_and_apply(live_server, page):
    _run_demo(page, live_server, "successful_application")
    assert "SUBMISSION_READY" in page.content()

    page.click('form[action^="/jobs/"][action$="/applications/approve"] button[type=submit]')
    page.wait_for_load_state("networkidle")

    page.goto(live_server + "/demo")
    assert "APPLIED" in page.content()
