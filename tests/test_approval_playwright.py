"""Real-browser end-to-end coverage of the approval-gated autonomy product
flow (approval-gated-autonomy-v1 spec section 23): Dashboard -> START AGENT
(TEST MODE) -> wait for a READY FOR APPROVAL card -> open review -> verify
one-page/claim-check/ATS indicators -> click APPROVE & APPLY -> confirmation.
Marked `browser`, following this project's existing convention
(tests/test_premium_ui_playwright.py) -- skipped automatically unless
Playwright AND its Chromium binary are actually launchable in this
environment (`pytest -m browser`); never runs by default.

CLAUDE.md approval-gated-autonomy-v1 section 18 (TEST MODE isolation) is a
hard, unconditional rule: a TEST MODE mock_ats fixture must never appear on
the real-mode Tracker (app.pipeline_dashboard.build_tracker_board excludes
is_test_fixture=1 rows unconditionally). Section 23's "Tracker shows
Applied/Confirmed" is therefore verified here via the job detail page's own
state (which does show TEST MODE jobs -- it's a per-job page, not a
real-mode-only aggregate) rather than by asserting the fixture appears on
the shared /tracker page, which would violate section 18."""

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
    """Runs the real app (real lifespan, real orchestrator) in a background
    thread of this test process -- inherits tmp_env's isolated config/db
    paths, never the real project data."""
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


def _wait_for_ready_for_approval_card(page, base_url: str, timeout_s: float = 40.0) -> None:
    # TEST MODE isolation (spec section 18): the fixture job is hidden from
    # the default real-mode dashboard view -- ?include_test_data=true is the
    # same explicit, opt-in audit toggle the dashboard's own "view test job"
    # link already uses, reused unchanged here.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.goto(base_url + "/?include_test_data=true")
        if page.locator("#ready-for-approval").count() > 0:
            return
        time.sleep(1.0)
    pytest.fail("READY FOR APPROVAL card never appeared within the timeout")


def test_full_approval_flow_dashboard_to_confirmed(live_server, page):
    page.goto(live_server + "/")
    page.click('button:has-text("START AGENT (TEST MODE)")')
    page.wait_for_selector('button:has-text("STOP AGENT")', timeout=10000)

    _wait_for_ready_for_approval_card(page, live_server)

    # Review the prepared package before approving.
    review_link = page.locator("#ready-for-approval a:has-text('Review Application')").first
    review_link.click()
    page.wait_for_load_state("networkidle")
    body_text = page.locator("body").inner_text()
    assert "READY FOR APPROVAL" in body_text
    # Resume/claim-check/ATS diagnostics already rendered by the JD
    # coverage section (badge-1page / Claim check row).
    assert "1 PAGE" in body_text
    assert "Claim check" in body_text
    assert "No submission has happened yet" in body_text

    # APPROVE & APPLY.
    approve_button = page.locator('button:has-text("APPROVE & APPLY")').first
    approve_button.click()
    page.wait_for_load_state("networkidle")

    body_text = page.locator("body").inner_text()
    assert "APPLIED" in body_text
    assert "Confirmation:" in body_text

    page.goto(live_server + "/")
    page.click('button:has-text("STOP AGENT")')
    page.wait_for_selector('button:has-text("START AGENT")', timeout=15000)
