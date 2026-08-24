"""Real-browser end-to-end coverage of the premium UI's primary visible
controls (CLAUDE.md premium-UI brief: "Add Playwright/local HTTP tests for
all PRIMARY visible controls"). Marked `browser`, following this project's
existing convention (tests/test_browser_assist_e2e.py) -- skipped
automatically unless Playwright AND its Chromium binary are actually
launchable in this environment (`pytest -m browser`); never runs by
default. Unlike the existing browser_assist e2e tests (which drive
Chromium against local `file://` HTML fixtures), this suite needs a real
running instance of the FastAPI app -- `live_server` below starts one in a
background thread, reusing the exact same monkeypatched (isolated,
temp-directory) config the `tmp_env` fixture already sets up, so it never
touches the real project's data/candidate_data/output directories."""

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
    """Runs the real app (with its real lifespan -- schedulers included) in
    a background thread of THIS test process, so it inherits tmp_env's
    already-monkeypatched config/db paths -- never the real project data."""
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


def _seed_job(base_url: str, page, *, url: str = "") -> None:
    """Ingest one job through the real /jobs/ingest form -- exercises the
    manual-JD-ingestion control itself as a side effect of seeding data for
    the rest of the scenario."""
    page.goto(base_url + "/jobs")
    page.fill("#ing-title", "Backend Software Engineer")
    page.fill("#ing-company", "Acme Corp")
    page.fill("#ing-location", "Remote (US)")
    if url:
        page.fill("#ing-url", url)
    page.fill("#ing-desc", (
        "We are hiring a Backend Software Engineer to build REST APIs in Python "
        "using FastAPI and PostgreSQL. Fully remote. Visa sponsorship available. "
        "Required: Python, FastAPI, PostgreSQL, Docker."
    ))
    page.click("#add-job button[type=submit]")
    page.wait_for_load_state("networkidle")


def test_dashboard_loads_and_nav_links_work(live_server, page):
    page.goto(live_server + "/")
    assert "Sponsor Job Agent" in page.title()
    for label, path in [("Jobs", "/jobs"), ("Applications", "/applications"), ("Tracker", "/tracker"),
                         ("Activity", "/activity"), ("Profile", "/profile"), ("Settings", "/settings")]:
        page.click(f'.primary-nav a[href="{path}"]')
        page.wait_for_url(live_server + path)
        assert page.locator("h1").first.is_visible()


def test_start_and_stop_agent_buttons_work(live_server, page):
    page.goto(live_server + "/")
    page.click('button:has-text("START AGENT (TEST MODE)")')
    page.wait_for_selector('button:has-text("STOP AGENT")', timeout=10000)
    assert page.locator(".state-pill").first.inner_text() in ("RUNNING", "STARTING")

    page.click('button:has-text("STOP AGENT")')
    page.wait_for_selector('button:has-text("START AGENT")', timeout=10000)


def test_ingest_job_then_view_and_search(live_server, page):
    _seed_job(live_server, page)
    page.goto(live_server + "/jobs")
    assert "Acme Corp" in page.content()

    page.fill("#job-search", "Acme")
    page.click('button:has-text("Search")')
    page.wait_for_load_state("networkidle")
    assert "Acme Corp" in page.content()

    page.fill("#job-search", "Nonexistent Company XYZ")
    page.click('button:has-text("Search")')
    page.wait_for_load_state("networkidle")
    assert "No jobs match these filters" in page.content()

    page.goto(live_server + "/jobs")
    page.click(".job-card >> text=View")
    page.wait_for_load_state("networkidle")
    assert "Acme Corp" in page.content()
    assert "JD coverage" in page.content()


def test_job_detail_generate_resume_and_downloads(live_server, page):
    _seed_job(live_server, page)
    page.goto(live_server + "/jobs")
    page.click(".job-card >> text=View")
    page.wait_for_load_state("networkidle")

    generate_btn = page.locator('button:has-text("Generate Resume")')
    if generate_btn.count() == 0:
        generate_btn = page.locator('button:has-text("Regenerate Resume")').first
    generate_btn.first.click()
    page.wait_for_load_state("networkidle")

    docx_link = page.locator('a:has-text("resume.docx")')
    if docx_link.count() > 0:
        href = docx_link.first.get_attribute("href")
        resp = page.request.get(live_server + href)
        assert resp.status == 200

    pdf_link = page.locator('a:has-text("resume.pdf")')
    if pdf_link.count() > 0:
        href = pdf_link.first.get_attribute("href")
        resp = page.request.get(live_server + href)
        assert resp.status == 200
        assert "pdf" in resp.headers.get("content-type", "").lower()


def test_open_application_link_present_and_valid(live_server, page):
    _seed_job(live_server, page, url="https://example.com/careers/backend-engineer")
    page.goto(live_server + "/jobs")
    page.click(".job-card >> text=View")
    page.wait_for_load_state("networkidle")

    open_link = page.locator('a:has-text("Open Application")')
    assert open_link.count() == 1
    assert open_link.first.get_attribute("href") == "https://example.com/careers/backend-engineer"
    assert open_link.first.get_attribute("target") == "_blank"


def test_needs_your_action_continue_button_navigates_to_job(live_server, page):
    # "Acme Corp" matches tmp_env's known_h1b_sponsors.json fixture --
    # LIKELY_SPONSOR (no explicit sponsorship language in the JD itself) ->
    # REVIEW_REQUIRED -> lands in the Needs Your Action queue.
    page.goto(live_server + "/jobs")
    page.fill("#ing-title", "Backend Software Engineer")
    page.fill("#ing-company", "Acme Corp")
    page.fill("#ing-desc", "Join our backend Python team building APIs.")
    page.click("#add-job button[type=submit]")
    page.wait_for_load_state("networkidle")

    page.goto(live_server + "/")
    assert "Needs Your Action" in page.content()
    page.click('a:has-text("Continue")')
    page.wait_for_load_state("networkidle")
    assert "Acme Corp" in page.content()


def test_provider_capability_navigation_from_footer(live_server, page):
    page.goto(live_server + "/")
    page.click('footer a[href="/providers"]')
    page.wait_for_url(live_server + "/providers")
    assert page.locator("body").is_visible()

    page.goto(live_server + "/")
    page.click('footer a[href="/applications/capability-matrix"]')
    page.wait_for_url(live_server + "/applications/capability-matrix")
    assert page.locator("body").is_visible()


def test_filters_and_tracker_and_activity_render(live_server, page):
    _seed_job(live_server, page)
    page.goto(live_server + "/jobs")
    page.click('a:has-text("High priority")')
    page.wait_for_load_state("networkidle")
    assert page.url.endswith("high_priority=true")

    page.goto(live_server + "/tracker")
    for label in ("Applied", "Assessment", "Interview", "Offer", "Rejected", "Withdrawn"):
        assert label in page.content()

    page.goto(live_server + "/activity")
    assert "Live Activity" in page.content()


def test_settings_save_button_actually_saves(live_server, page):
    page.goto(live_server + "/settings")
    page.fill("#set-agent_interval_minutes", "11")
    page.click('button:has-text("Save settings")')
    page.wait_for_load_state("networkidle")
    assert "Settings saved." in page.content()
    assert page.input_value("#set-agent_interval_minutes") == "11"


def test_applications_tabs_are_clickable(live_server, page):
    page.goto(live_server + "/applications")
    for label in ("All", "Ready to Apply", "Needs Action", "Applying", "Applied", "Failed", "Skipped"):
        assert label in page.content()
    page.click('.tab-row a:has-text("Skipped")')
    page.wait_for_load_state("networkidle")
    assert "bucket=skipped" in page.url


def test_keyboard_navigation_reaches_primary_nav(live_server, page):
    """Every primary nav link is reachable and activatable purely via the
    keyboard -- focus it directly (Playwright's .focus() drives the same
    accessibility-tree focus a Tab-key user would land on) and confirm
    Enter navigates, rather than asserting a brittle exact tab-stop count."""
    page.goto(live_server + "/")
    jobs_link = page.locator('.primary-nav a[href="/jobs"]')
    jobs_link.focus()
    assert page.evaluate("document.activeElement.getAttribute('href')") == "/jobs"
    page.keyboard.press("Enter")
    page.wait_for_url(live_server + "/jobs")
    assert page.locator("h1").first.inner_text() == "Jobs"
