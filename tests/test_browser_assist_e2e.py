"""CLAUDE.md Phase 10 sections 55-56, 71 (acceptance A/D/E/F/G/H/K/L): real
Chromium-driven end-to-end tests against the local mock-ATS HTML sandbox
(tests/browser_fixtures.py). Marked `browser` -- skipped automatically
unless Playwright AND its Chromium binary are actually launchable
(`pytest -m browser`); never runs by default, never touches a real website
or requires internet access (every URL is a local `file://` fixture)."""

import json

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


@pytest.fixture
def _prepared(tmp_env, sample_profile):
    """One FULL_TIME + CONFIRMED_SPONSOR job with resume artifacts and an
    active execution, ready for browser_assist.start_session()."""
    import json as _json

    from app.candidate.profile import save_profile
    from app.applications import repo as executions_repo
    from app.jobs_repo import get_job, insert_job, update_job
    from app.models import ApplicationState, Job, SponsorshipStatus

    save_profile(sample_profile)

    def _make(url: str):
        job = Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description="Full-time role. H-1B sponsorship is available.", employment_type="full_time",
            sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, technical_match_score=80.0,
            application_state=ApplicationState.READY_TO_APPLY, provider="never_configured_e2e",
            canonical_url=url, url=url,
        )
        job_id = insert_job(job)
        job_dir = tmp_env["output_dir"] / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "resume.pdf").write_bytes(b"%PDF-1.4 fake resume")
        (job_dir / "resume.docx").write_bytes(b"fake docx")
        (job_dir / "application_answers.json").write_text(_json.dumps({
            "full_name": "Test Candidate", "email": "test.candidate@example.com", "phone": "555-000-1111",
            "do_you_require_sponsorship": "No",
        }))
        update_job(job_id, resume_pdf_path=str(job_dir / "resume.pdf"), resume_docx_path=str(job_dir / "resume.docx"),
                   application_answers_path=str(job_dir / "application_answers.json"))
        execution_id = executions_repo.create_execution(job_id, provider="never_configured_e2e", mode="ASSIST")
        return get_job(job_id), execution_id

    return _make


def _close(session_id: str) -> None:
    from app.applications import browser_assist

    browser_assist.close_session(session_id)


def test_full_time_confirmed_reaches_ready_for_final_submit(tmp_path, _prepared):
    """Acceptance A."""
    from tests.browser_fixtures import simple_form_page
    from app.applications import browser_assist

    job, execution_id = _prepared(simple_form_page(tmp_path))
    try:
        result = browser_assist.start_session(execution_id)
        assert result["created"] is True
        session = result["session"]
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        assert session["mapped_field_count"] == 2
        assert session["unresolved_field_count"] == 0
    finally:
        _close(result["session"]["session_id"])


def test_login_wall_pauses_session_never_submits(tmp_path, _prepared):
    """Acceptance D."""
    from tests.browser_fixtures import login_page
    from app.applications import browser_assist

    job, execution_id = _prepared(login_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    try:
        assert result["session"]["status"] == "PAUSED_LOGIN_REQUIRED"
        assert result["session"]["needs_user_action"] == 1
    finally:
        _close(result["session"]["session_id"])


def test_captcha_pauses_session_no_bypass_attempted(tmp_path, _prepared):
    """Acceptance E."""
    from tests.browser_fixtures import captcha_page
    from app.applications import browser_assist

    job, execution_id = _prepared(captcha_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    try:
        assert result["session"]["status"] == "PAUSED_CAPTCHA"
    finally:
        _close(result["session"]["session_id"])


def test_unknown_legal_question_pauses_session(tmp_path, _prepared):
    """Acceptance F."""
    from tests.browser_fixtures import legal_question_page
    from app.applications import browser_assist

    job, execution_id = _prepared(legal_question_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    try:
        assert result["session"]["status"] == "PAUSED_LEGAL_QUESTION"
    finally:
        _close(result["session"]["session_id"])


def test_unknown_generic_field_pauses_session(tmp_path, _prepared):
    from tests.browser_fixtures import unknown_field_page
    from app.applications import browser_assist

    job, execution_id = _prepared(unknown_field_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    try:
        assert result["session"]["status"] == "PAUSED_UNKNOWN_FIELD"
    finally:
        _close(result["session"]["session_id"])


def test_conditional_sponsorship_question_answered_correctly(tmp_path, _prepared):
    """Acceptance H: the shared sample_profile fixture truthfully has
    requires_sponsorship=True (H-1B) -- "Yes" must be selected (never
    misrepresented), which reveals the conditional visa-type field, which
    must then ALSO get filled correctly from the verified profile value."""
    from tests.browser_fixtures import conditional_sponsorship_page
    from app.applications import browser_assist, browser_runtime

    job, execution_id = _prepared(conditional_sponsorship_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
        # Playwright's sync API is thread-affine -- any direct inspection of
        # `live.page` from the test's own thread must go through the same
        # dedicated single-thread executor browser_runtime itself uses
        # (live.run), never called directly, or Playwright raises
        # "Cannot switch to a different thread".
        live = browser_runtime._REGISTRY[session_id]
        checked_value = live.run(lambda: live.page.evaluate(
            "() => { const el = document.querySelector('input[name=\"sponsorship_q\"]:checked'); "
            "return el ? el.value : null; }"
        ))
        assert checked_value == "Yes"
        visa_visible = live.run(lambda: live.page.evaluate(
            "() => document.getElementById('visa-type-wrap').style.display"
        ))
        assert visa_visible == "block"
        visa_value = live.run(lambda: live.page.locator("#visa-type").input_value())
        assert visa_value == "H-1B"
    finally:
        _close(session_id)


def test_multi_step_form_advance_reaches_step_two(tmp_path, _prepared):
    """Acceptance G."""
    from tests.browser_fixtures import multi_step_pages
    from app.applications import browser_assist

    page1, _page2 = multi_step_pages(tmp_path)
    job, execution_id = _prepared(page1)
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "ACTIVE"
        assert result["session"]["current_step"] == 1

        advance_result = browser_assist.advance_step(session_id)
        assert advance_result["ok"] is True
        session = advance_result["session"]
        assert session["current_step"] == 2
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        # School/Degree map to sample_profile's real education[0] entry.
        assert session["mapped_field_count"] == 2
    finally:
        _close(session_id)


def test_form_changed_detected_on_resume(tmp_path, _prepared):
    """Acceptance L: never reuse a stale mapping when the form has changed."""
    from tests.browser_fixtures import simple_form_page, unknown_field_page
    from app.applications import browser_assist, browser_runtime

    job, execution_id = _prepared(simple_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"

        # Simulate the ATS changing the form under us while the window stayed open.
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(unknown_field_page(tmp_path)))

        resumed = browser_assist.resume_session(session_id)
        assert resumed["session"]["status"] == "PAUSED_FORM_CHANGED"
    finally:
        _close(session_id)


def test_manual_submit_confirmation_capture_marks_applied(tmp_path, _prepared):
    """Acceptance K."""
    from tests.browser_fixtures import simple_form_page, success_page
    from app.applications import browser_assist, browser_runtime, browser_session
    from app.applications import repo as executions_repo

    job, execution_id = _prepared(simple_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
        browser_session.update_session(session_id, status="AWAITING_USER_SUBMIT")

        # Simulate the candidate clicking submit themselves in the visible window.
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(success_page(tmp_path)))

        reconcile_result = browser_assist.attempt_user_submit_reconciliation(session_id)
        assert reconcile_result["ok"] is True
        assert reconcile_result["session"]["status"] == "CONFIRMED"
        assert reconcile_result["session"]["confirmation_id"] == "ABC-1234-XYZ"

        execution = executions_repo.get_execution(execution_id)
        assert execution["status"] == "APPLIED"
    finally:
        _close(session_id)


def test_file_upload_field_prepared(tmp_path, _prepared):
    """CLAUDE.md Phase 10 section 15: real file-upload preparation."""
    from tests.browser_fixtures import form_with_file_upload_page
    from app.applications import browser_assist, browser_runtime

    job, execution_id = _prepared(form_with_file_upload_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
        live = browser_runtime._REGISTRY[session_id]
        file_value = live.run(lambda: live.page.evaluate("() => document.getElementById('resume').files.length"))
        assert file_value == 1
    finally:
        _close(session_id)


def test_never_clicks_submit_button(tmp_path, _prepared):
    """CLAUDE.md Phase 10 section 29: the runtime may locate a submit
    button, it must never click it -- verified here by checking the page
    never navigated away and the button is still present."""
    from tests.browser_fixtures import simple_form_page
    from app.applications import browser_assist, browser_runtime

    original_url = simple_form_page(tmp_path)
    job, execution_id = _prepared(original_url)
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        current_url = live.run(lambda: live.page.url)
        assert current_url == original_url
        button_count = live.run(lambda: live.page.locator("button[type=submit]").count())
        assert button_count == 1
    finally:
        _close(session_id)
