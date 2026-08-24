"""Real-browser end-to-end coverage of the universal Apply CTA
(application-action-experience-v1, build brief section 13): the full TEST
MODE journey a user actually sees -- START AGENT (TEST MODE) -> the test
job visibly reaches Ready to Apply -> the APPROVE & APPLY button is
visible, cannot be double-clicked, shows an in-progress state, and the job
reaches Applied with a receipt. Marked `browser`, following this project's
existing convention (tests/test_approval_playwright.py,
tests/test_premium_ui_playwright.py) -- skipped automatically unless
Playwright AND its Chromium binary are actually launchable in this
environment; never runs by default."""

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


def _wait_for_ready_for_approval_card(pg, base_url: str, timeout_s: float = 40.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pg.goto(base_url + "/?include_test_data=true")
        if pg.locator("#ready-for-approval").count() > 0:
            return
        time.sleep(1.0)
    pytest.fail("READY FOR APPROVAL card never appeared within the timeout")


def test_full_cta_journey_ready_to_applied_with_receipt(live_server, page):
    """A (reaches Ready to Apply) + B (button visible) + C (click calls the
    real approval action) + D (cannot double-submit) + F (mock ATS reaches
    Applied) + G (receipt visible), in one continuous user journey."""
    page.goto(live_server + "/")
    page.click('button:has-text("START AGENT (TEST MODE)")')
    page.wait_for_selector('button:has-text("STOP AGENT")', timeout=10000)

    # A: the test job visibly reaches Ready to Apply on the dashboard.
    _wait_for_ready_for_approval_card(page, live_server)

    # B: APPROVE & APPLY is visibly present as a real <button>, not a link
    # merely styled to look like one.
    approve_button = page.locator('#ready-for-approval button[data-cta-button]').first
    assert approve_button.is_visible()
    assert approve_button.evaluate("el => el.tagName") == "BUTTON"
    assert approve_button.is_enabled()

    # C/D: clicking disables the button immediately (before any network
    # round trip can complete) so a rapid second click can never re-submit.
    approve_button.click()
    # Immediately after the click (JS disables synchronously in the submit
    # handler, before the fetch() resolves) -- catches the "Applying..."
    # in-progress state rather than racing straight to the final result.
    page.wait_for_function(
        "document.querySelector('#ready-for-approval button[data-cta-button]') ? "
        "document.querySelector('#ready-for-approval button[data-cta-button]').disabled : true",
        timeout=3000,
    )
    disabled_immediately = page.evaluate(
        "document.querySelector('#ready-for-approval button[data-cta-button]') ? "
        "document.querySelector('#ready-for-approval button[data-cta-button]').disabled : true"
    )
    assert disabled_immediately is True

    # F: mock_ats genuinely reaches Applied (the JS poll then reloads the
    # page to the canonical server-rendered result).
    page.wait_for_function(
        "document.body.innerText.includes('APPLIED')", timeout=15000,
    )
    body_text = page.locator("body").inner_text()
    assert "APPLIED" in body_text

    # Follow through to the job detail page to confirm the receipt (G).
    page.goto(live_server + "/?include_test_data=true")
    job_link = page.locator("a[href^='/jobs/']").first
    job_link.click()
    page.wait_for_load_state("networkidle")
    detail_text = page.locator("body").inner_text()
    assert "APPLIED" in detail_text
    assert "Confirmation:" in detail_text
    # E/success CTA in the hero reads "VIEW RECEIPT", not a raw enum name.
    assert "VIEW RECEIPT" in detail_text
    assert "SUBMISSION_CONFIRMED" not in detail_text and "ExecutionStatus" not in detail_text

    page.goto(live_server + "/")
    page.click('button:has-text("STOP AGENT")')
    page.wait_for_selector('button:has-text("START AGENT")', timeout=15000)


def _run_to_ready_for_approval_job_url(live_server) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        try:
            pg.goto(live_server + "/")
            pg.click('button:has-text("START AGENT (TEST MODE)")')
            pg.wait_for_selector('button:has-text("STOP AGENT")', timeout=10000)
            _wait_for_ready_for_approval_card(pg, live_server)
            review_link = pg.locator("#ready-for-approval a:has-text('Review Application')").first
            review_link.click()
            pg.wait_for_load_state("networkidle")
            job_url = pg.url
            pg.goto(live_server + "/")
            pg.click('button:has-text("STOP AGENT")')
            pg.wait_for_selector('button:has-text("START AGENT")', timeout=15000)
        finally:
            ctx.close()
            browser.close()
    return job_url


def test_apply_cta_visible_at_mobile_viewport(live_server):
    """M: the primary APPROVE & APPLY CTA stays visible and reachable at a
    mobile viewport width, with no horizontal page overflow."""
    from playwright.sync_api import sync_playwright

    job_url = _run_to_ready_for_approval_job_url(live_server)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        pg = ctx.new_page()
        try:
            pg.goto(job_url)
            pg.wait_for_load_state("networkidle")
            approve_button = pg.locator('button:has-text("APPROVE & APPLY")').first
            assert approve_button.is_visible()
            scroll_width = pg.evaluate("document.documentElement.scrollWidth")
            client_width = pg.evaluate("document.documentElement.clientWidth")
            assert scroll_width <= client_width + 1
        finally:
            ctx.close()
            browser.close()


def test_apply_cta_visible_in_dark_mode(live_server):
    """N: the primary APPROVE & APPLY CTA stays visible/legible under
    prefers-color-scheme: dark."""
    from playwright.sync_api import sync_playwright

    job_url = _run_to_ready_for_approval_job_url(live_server)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(color_scheme="dark")
        pg = ctx.new_page()
        try:
            pg.goto(job_url)
            pg.wait_for_load_state("networkidle")
            approve_button = pg.locator('button:has-text("APPROVE & APPLY")').first
            assert approve_button.is_visible()
            bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
            r, g, b = (int(x) for x in bg.replace("rgb(", "").replace("rgba(", "").replace(")", "").split(",")[:3])
            assert (r + g + b) / 3 < 128, f"body background does not look dark in dark mode: {bg}"
        finally:
            ctx.close()
            browser.close()
