"""Workday/SmartRecruiters/Workable browser-assist hardening (2026-08-22):
real Chromium-driven E2E tests for dynamic-validation detection
(app.applications.dynamic_validation / browser_runtime._do_advance_step)
and the generic multi-step engine against a Workable-shaped 2-step flow.
Marked `browser` -- skipped automatically unless Playwright AND its
Chromium binary are actually launchable (`pytest -m browser`); every URL is
a local `file://` fixture (tests/browser_fixtures.py), never a real
website. NOT executed live in the sandbox this test file was authored in
(system Chromium missing shared libraries -- see
docs/workday-smartrecruiters-workable-browser-hardening.md); written and
reviewed against this project's own extensively-precedented E2E patterns
(tests/test_browser_assist_e2e.py, tests/test_browser_assist_phase11_e2e.py)
for a future environment with a working Chromium to actually run."""

import uuid

import pytest

from app import config

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
    monkeypatch.setattr(config, "BROWSER_DOM_STABILIZATION_TIMEOUT_MS", 3000)
    monkeypatch.setattr(config, "BROWSER_DOM_STABILIZATION_POLL_MS", 100)


def _open(url: str) -> str:
    """CLAUDE.md Phase 13 acceptance correction: `open_session()` without
    `expected_title`/`expected_company` leaves the pre-upload/pre-final-
    submit job-identity recheck (`app.applications.job_identity`) with
    nothing to compare, which is INSUFFICIENT and correctly pauses
    JOB_IDENTITY_UNVERIFIED once a step reaches a file/submit control --
    exactly the documented contract on `browser_runtime.open_session()`.
    Every fixture used by this file's tests embeds `DEFAULT_JOB_TITLE`/
    `DEFAULT_JOB_COMPANY` via `_jsonld_block()` for exactly this reason
    (see the module docstring above it), matching the same convention
    every other `tests/test_browser_assist_*_e2e.py` file already uses via
    its own `_prepared()` fixture."""
    from app.applications import browser_runtime
    from tests.browser_fixtures import DEFAULT_JOB_COMPANY, DEFAULT_JOB_TITLE

    session_id = f"bsess_hardening_{uuid.uuid4().hex[:8]}"
    browser_runtime.open_session(
        session_id, provider="never_configured_e2e", url=url,
        expected_title=DEFAULT_JOB_TITLE, expected_company=DEFAULT_JOB_COMPANY,
    )
    return session_id


def _close(session_id: str) -> None:
    from app.applications import browser_runtime

    browser_runtime.close_session(session_id)


def test_dynamic_validation_blocks_advance_when_required_field_empty(tmp_path, tmp_env):
    """Opens the fixture directly via browser_runtime (bypassing
    browser_assist's own auto-fill, which would otherwise fill 'full_name'
    from the candidate profile before advance_step is ever called) so the
    required field is genuinely left empty -- clicking Next must report
    `advanced: False, reason: 'validation_blocked'`, never a false
    `advanced: True`."""
    from tests.browser_fixtures import workday_like_dynamic_validation_wizard_page
    from app.applications import browser_runtime

    url = workday_like_dynamic_validation_wizard_page(tmp_path)
    session_id = _open(url)
    try:
        result = browser_runtime.advance_step(session_id)
        assert result["advanced"] is False
        assert result["reason"] == "validation_blocked"
        assert any("required" in e.lower() for e in result.get("validation_errors", []))

        outcome = browser_runtime.rediscover(session_id)
        assert outcome.stage == "APPLICATION_FORM"
        assert not any(f.get("type") == "file" for f in outcome.fields)  # still on step 1
    finally:
        _close(session_id)


def test_dynamic_validation_allows_advance_once_field_filled(tmp_path, tmp_env):
    from tests.browser_fixtures import workday_like_dynamic_validation_wizard_page
    from app.applications import browser_runtime

    url = workday_like_dynamic_validation_wizard_page(tmp_path)
    session_id = _open(url)
    try:
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.fill("#fname", "Test Candidate"))

        result = browser_runtime.advance_step(session_id)
        assert result["advanced"] is True
        assert result["current_step"] == 2

        outcome = browser_runtime.rediscover(session_id)
        assert any(f.get("type") == "file" for f in outcome.fields)  # now on step 2
    finally:
        _close(session_id)


def test_ordinary_route_changing_advance_still_reports_advanced_true(tmp_path, tmp_env):
    """Regression guard: the new validation-blocked check must never make a
    genuinely successful, route-changing Next click look blocked."""
    from tests.browser_fixtures import multi_step_pages
    from app.applications import browser_runtime

    page1, _page2 = multi_step_pages(tmp_path)
    session_id = _open(page1)
    try:
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.fill("#fname", "Test Candidate"))
        live.run(lambda: live.page.fill("#mail", "test@example.com"))

        result = browser_runtime.advance_step(session_id)
        assert result["advanced"] is True
        assert result["current_step"] == 2
    finally:
        _close(session_id)


def test_workable_like_multistep_form_advances_and_reaches_upload_step(tmp_path, tmp_env):
    """Multi-step form handling verified generically against a
    Workable-shaped 2-step flow (the one real Workable tenant reached live,
    apply.workable.com/flosum, was single-page)."""
    from tests.browser_fixtures import workable_like_multistep_page
    from app.applications import browser_runtime

    page1, _page2 = workable_like_multistep_page(tmp_path)
    session_id = _open(page1)
    try:
        outcome = browser_runtime.rediscover(session_id)
        assert {f.get("name") for f in outcome.fields} >= {"full_name", "email", "phone"}

        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.fill("#fname", "Test Candidate"))
        live.run(lambda: live.page.fill("#mail", "test@example.com"))

        result = browser_runtime.advance_step(session_id)
        assert result["advanced"] is True

        outcome2 = browser_runtime.rediscover(session_id)
        field_names = {f.get("name") for f in outcome2.fields}
        assert "resume" in field_names
        assert "linkedin_url" in field_names
        assert "salary_expectation" in field_names
    finally:
        _close(session_id)
