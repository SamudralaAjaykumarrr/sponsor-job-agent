"""CLAUDE.md Phase 11 acceptance scenarios A-J: real Chromium-driven E2E
tests for the apply-first-click / step-progress / reconstruction / ownership
/ duplicate-confirmation hardening added this phase. Marked `browser` --
skipped automatically unless Playwright AND its Chromium binary are actually
launchable; every URL is a local `file://` fixture (tests/browser_fixtures.py
+ this phase's additions), never a real website."""

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


def test_landing_page_apply_click_reaches_form(tmp_path, _prepared):
    """Acceptance A: SmartRecruiters-like landing page -> Apply safe-
    navigation click -> form discovered -> fields mapped."""
    from tests.browser_fixtures import landing_page_with_apply_click
    from app.applications import browser_assist

    landing_url, _form_url = landing_page_with_apply_click(tmp_path)
    job, execution_id = _prepared(landing_url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        assert session["apply_entry_clicked"] == 1
        assert session["stage"] == "APPLICATION_FORM"
        assert session["mapped_field_count"] == 2
    finally:
        _close(session["session_id"])


def test_final_submit_lookalike_never_clicked_as_apply_entry(tmp_path, _prepared):
    """Acceptance B: a 'Submit Application'-labeled control on a landing
    page must never be mistaken for a safe apply-entry navigation click."""
    from tests.browser_fixtures import landing_page_with_final_submit_lookalike
    from app.applications import browser_assist, browser_runtime

    url = landing_page_with_final_submit_lookalike(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["apply_entry_clicked"] == 0
        # No NAVIGATION_SAFE (or even ambiguous UNKNOWN-but-apply-shaped)
        # control was found -- only a FINAL_SUBMIT lookalike, which
        # _detect_apply_entry_control deliberately never returns as an
        # apply-entry candidate at all. PAUSED_UNSUPPORTED_SUBMISSION (never
        # a click) is exactly the safe, honest outcome.
        assert session["status"] == "PAUSED_UNSUPPORTED_SUBMISSION"
        live = browser_runtime._REGISTRY[session["session_id"]]
        current_url = live.run(lambda: live.page.url)
        assert current_url == url
    finally:
        _close(session["session_id"])


def test_workday_like_login_gate_reached_via_apply_click(tmp_path, _prepared):
    """Acceptance C: job details -> Apply -> account/login start page ->
    pause for the user; the apply-entry click itself is safe/automatic, the
    login wall is not."""
    from tests.browser_fixtures import workday_like_login_gate_page
    from app.applications import browser_assist

    landing_url, _login_url = workday_like_login_gate_page(tmp_path)
    job, execution_id = _prepared(landing_url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["apply_entry_clicked"] == 1
        assert session["status"] == "PAUSED_LOGIN_REQUIRED"
    finally:
        _close(session["session_id"])


def test_step_progress_parsed_as_exact(tmp_path, _prepared):
    """Acceptance D: a genuinely-displayed 'Step 2 of 4' is parsed exactly,
    never inferred or invented."""
    from tests.browser_fixtures import step_progress_form_page
    from app.applications import browser_assist

    job, execution_id = _prepared(step_progress_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["step_confidence"] == "EXACT"
        assert session["current_step"] == 2
        assert session["total_steps_if_known"] == 4
    finally:
        _close(session["session_id"])


def test_intentional_step_advance_no_false_form_changed(tmp_path, _prepared):
    """Acceptance E: preserved from Phase 10 -- included here to lock the
    behavior in against the new apply-entry/stage plumbing added this
    phase."""
    from tests.browser_fixtures import multi_step_pages
    from app.applications import browser_assist

    page1, _page2 = multi_step_pages(tmp_path)
    job, execution_id = _prepared(page1)
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        advance_result = browser_assist.advance_step(session_id)
        assert advance_result["ok"] is True
        assert advance_result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
    finally:
        _close(session_id)


def test_conditional_question_reveals_genuinely_new_field(tmp_path, _prepared):
    """Acceptance F: the visa-type field does not exist in the DOM at all
    until the sponsorship radio's change handler INSERTS it -- exercises
    the Phase 11 rediscovery pass, not merely an unhide."""
    from tests.browser_fixtures import conditional_new_field_page
    from app.applications import browser_assist, browser_runtime

    job, execution_id = _prepared(conditional_new_field_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
        live = browser_runtime._REGISTRY[session_id]
        visa_value = live.run(lambda: live.page.locator("#visa-type").input_value())
        assert visa_value == "H-1B"
    finally:
        _close(session_id)


def test_worker_dies_while_paused_new_worker_reconstructs_no_duplicate(tmp_path, _prepared):
    """Acceptance G: simulates a crashed worker (the live browser registry
    entry is gone) -- resume_session() reconstructs a fresh browser at the
    saved URL rather than guessing, and never creates a second session/
    execution for the same job."""
    from tests.browser_fixtures import login_page
    from app.applications import browser_assist, browser_runtime, browser_session

    job, execution_id = _prepared(login_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    assert result["session"]["status"] == "PAUSED_LOGIN_REQUIRED"

    # Simulate the owning process crashing: discard the live registry entry
    # directly WITHOUT calling close_session (which would cleanly release
    # everything) -- this is what an actual process crash leaves behind.
    browser_runtime._discard(session_id)
    assert browser_runtime.is_live(session_id) is False

    try:
        resumed = browser_assist.resume_session(session_id)
        assert resumed["session"]["reconstructed_count"] == 1
        assert "reconstruct" in resumed["detail"].lower()

        # No duplicate session/execution was created for this job.
        assert browser_session.get_active_session_for_job(job.id)["session_id"] == session_id
    finally:
        _close(session_id)


def test_owner_conflict_blocks_second_caller(tmp_path, _prepared, monkeypatch):
    """CLAUDE.md Phase 11 section 26: two concurrent orchestration calls for
    the SAME session never both drive the browser -- the loser is told the
    session is owned elsewhere and never touches browser_runtime."""
    from tests.browser_fixtures import login_page
    from app.applications import browser_assist, browser_session

    job, execution_id = _prepared(login_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        # Simulate a DIFFERENT worker already owning the lease.
        browser_session.claim_session(session_id, worker_id="other-worker-xyz", lease_seconds=600)

        resumed = browser_assist.resume_session(session_id)
        assert resumed["ok"] is False
        assert "owned by another worker" in resumed["detail"]
    finally:
        browser_session.release_session_lease(session_id)
        _close(session_id)


def test_unexpected_redirect_blocked(tmp_path, _prepared):
    """Acceptance H: navigating off the allowed domain pauses the session
    for review rather than continuing to interact with an unverified page."""
    from tests.browser_fixtures import simple_form_page
    from app.applications import browser_assist, browser_runtime

    job, execution_id = _prepared(simple_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto("https://example.com"))

        resumed = browser_assist.resume_session(session_id)
        assert resumed["session"]["status"] == "PAUSED_PLATFORM_RESTRICTED"
    finally:
        _close(session_id)


def test_manual_submit_duplicate_application_detected(tmp_path, _prepared):
    """CLAUDE.md Phase 11 section 36: 'you already applied' evidence is
    handled distinctly from a fresh CONFIRMED success -- never marks the
    execution APPLIED from this evidence alone."""
    from tests.browser_fixtures import already_applied_page, simple_form_page
    from app.applications import browser_assist, browser_runtime, browser_session
    from app.applications import repo as executions_repo

    job, execution_id = _prepared(simple_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
        browser_session.update_session(session_id, status="AWAITING_USER_SUBMIT")

        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(already_applied_page(tmp_path)))

        reconcile_result = browser_assist.attempt_user_submit_reconciliation(session_id)
        assert reconcile_result["ok"] is False
        assert reconcile_result["session"]["status"] == "DUPLICATE_APPLICATION_DETECTED"

        execution = executions_repo.get_execution(execution_id)
        assert execution["status"] != "APPLIED"
    finally:
        _close(session_id)


def test_false_confirmation_phrase_not_counted(tmp_path, _prepared):
    """CLAUDE.md Phase 11 section 35: 'Submit your application to receive
    confirmation' must never count as genuine confirmation evidence."""
    from tests.browser_fixtures import false_confirmation_mention_page, simple_form_page
    from app.applications import browser_assist, browser_runtime, browser_session

    job, execution_id = _prepared(simple_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        browser_session.update_session(session_id, status="AWAITING_USER_SUBMIT")
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(false_confirmation_mention_page(tmp_path)))

        reconcile_result = browser_assist.attempt_user_submit_reconciliation(session_id)
        assert reconcile_result["ok"] is False
        assert browser_session.get_session(session_id)["status"] == "AWAITING_USER_SUBMIT"
    finally:
        _close(session_id)


def test_final_review_page_reaches_ready_for_final_submit(tmp_path, _prepared):
    """CLAUDE.md Phase 11 section 33: a review/summary page with no more
    fillable fields still reaches READY_FOR_FINAL_SUBMIT (not a false
    'unsupported' pause) when a submit control is present."""
    from tests.browser_fixtures import review_page
    from app.applications import browser_assist

    job, execution_id = _prepared(review_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["stage"] == "FINAL_REVIEW"
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
    finally:
        _close(session["session_id"])
