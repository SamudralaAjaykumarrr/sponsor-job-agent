"""CLAUDE.md Phase 12 acceptance scenarios A, E, G, J, K, L (partial), plus
sections 36-39, 58-62: real Chromium-driven E2E tests for SPA apply-control
discovery, DOM stabilization, iframe/shadow-DOM discovery, ambiguous
apply-control handling, and job-identity verification. Marked `browser` --
skipped automatically unless Playwright AND its Chromium binary are actually
launchable; every URL is a local `file://` fixture (tests/browser_fixtures.py),
never a real website."""

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


def test_smartrecruiters_spa_landing_reaches_form_after_delay(tmp_path, _prepared):
    """Acceptance A: an SPA landing page renders 'Apply Now' late (via JS
    setTimeout) -> bounded DOM-stabilization wait detects it -> classified
    NAVIGATION_SAFE -> client-side route change (pushState) -> dynamically-
    mounted form discovered."""
    from tests.browser_fixtures import smartrecruiters_like_spa_page
    from app.applications import browser_assist

    url = smartrecruiters_like_spa_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["apply_entry_clicked"] == 1
        assert session["stage"] == "APPLICATION_FORM"
        assert session["mapped_field_count"] >= 1
    finally:
        _close(session["session_id"])


def test_smartrecruiters_spa_multi_step_resume_upload_reached(tmp_path, _prepared):
    """The SPA fixture's step 2 (resume upload, final Submit) is reachable
    via an ordinary advance_step() call -- confirms SPA route-changed pages
    are still treated as an ordinary multi-step form once discovered."""
    from tests.browser_fixtures import smartrecruiters_like_spa_page
    from app.applications import browser_assist

    url = smartrecruiters_like_spa_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        advance = browser_assist.advance_step(session_id)
        assert advance["ok"] is True
        session = advance["session"]
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
    finally:
        _close(session_id)


def test_spa_that_never_renders_times_out_cleanly(tmp_path, _prepared):
    """Acceptance: a bounded DOM-stabilization wait, never an indefinite
    hang, when the SPA apply control never actually appears."""
    from tests.browser_fixtures import smartrecruiters_like_never_renders_page
    from app.applications import browser_assist

    url = smartrecruiters_like_never_renders_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_UNSUPPORTED_SUBMISSION"
    finally:
        _close(session["session_id"])


def test_workday_wizard_progress_parsed_exact_not_the_posted_date(tmp_path, _prepared):
    """Acceptance E + J + K combined: delayed Workday-like Apply hop, then
    a genuine 'Step 2 of 3' progress indicator parsed EXACT -- and the same
    page's unrelated 'Posted 7/31' text on the LANDING page never gets
    misread as step progress."""
    from tests.browser_fixtures import workday_like_progress_wizard_page
    from app.applications import browser_assist

    landing_url, _form_url = workday_like_progress_wizard_page(tmp_path)
    job, execution_id = _prepared(landing_url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["apply_entry_clicked"] == 1
        assert session["step_confidence"] == "EXACT"
        assert session["current_step"] == 2
        assert session["total_steps_if_known"] == 3
    finally:
        _close(session["session_id"])


def test_multiple_apply_controls_same_destination_not_ambiguous(tmp_path, _prepared):
    """CLAUDE.md Phase 12 sections 36-37: top+bottom Apply buttons for the
    SAME job resolve normally, never flagged ambiguous."""
    from tests.browser_fixtures import multiple_apply_controls_same_destination_page
    from app.applications import browser_assist

    landing_url, _form_url = multiple_apply_controls_same_destination_page(tmp_path)
    job, execution_id = _prepared(landing_url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] != "PAUSED_AMBIGUOUS_APPLY_CONTROL"
        assert session["apply_entry_clicked"] == 1
    finally:
        _close(session["session_id"])


def test_multiple_apply_controls_different_destination_is_ambiguous(tmp_path, _prepared):
    """CLAUDE.md Phase 12 sections 36-37: an Apply control for this job and
    one for a different 'similar job' must never be resolved by guessing."""
    from tests.browser_fixtures import multiple_apply_controls_different_destination_page
    from app.applications import browser_assist

    landing_url, _this_form, _other_form = multiple_apply_controls_different_destination_page(tmp_path)
    job, execution_id = _prepared(landing_url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_AMBIGUOUS_APPLY_CONTROL"
        assert session["apply_entry_clicked"] == 0
    finally:
        _close(session["session_id"])


def test_iframe_same_origin_form_discovered(tmp_path, _prepared):
    """CLAUDE.md Phase 12 section 14: a real application form mounted
    inside a same-origin iframe is discovered and filled, not missed."""
    from tests.browser_fixtures import iframe_form_page
    from app.applications import browser_assist

    job, execution_id = _prepared(iframe_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        assert session["iframe_used"] == 1
        assert session["mapped_field_count"] >= 1
    finally:
        _close(session["session_id"])


def test_no_iframe_regression_still_works(tmp_path, _prepared):
    """Control case: an ordinary top-level form is unaffected by the iframe
    scan being added."""
    from tests.browser_fixtures import no_iframe_form_page
    from app.applications import browser_assist

    job, execution_id = _prepared(no_iframe_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        assert session["iframe_used"] == 0
    finally:
        _close(session["session_id"])


def test_shadow_dom_open_form_discovered(tmp_path, _prepared):
    """CLAUDE.md Phase 12 sections 15, 62: a form mounted inside an OPEN
    shadow root is discovered via the deep-query piercing scan."""
    from tests.browser_fixtures import shadow_dom_form_page
    from app.applications import browser_assist

    job, execution_id = _prepared(shadow_dom_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        assert session["shadow_dom_used"] == 1
    finally:
        _close(session["session_id"])


def test_shadow_dom_closed_form_honestly_unsupported(tmp_path, _prepared):
    """CLAUDE.md Phase 12 section 62: a CLOSED shadow root's contents are
    genuinely undiscoverable -- the correct, honest UNSUPPORTED outcome,
    never a bypass attempt and never a crash."""
    from tests.browser_fixtures import closed_shadow_dom_form_page
    from app.applications import browser_assist

    job, execution_id = _prepared(closed_shadow_dom_form_page(tmp_path))
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_UNSUPPORTED_SUBMISSION"
        assert session["shadow_dom_used"] == 0
    finally:
        _close(session["session_id"])


def test_job_identity_mismatch_pauses_session(tmp_path, _prepared):
    """CLAUDE.md Phase 12 sections 37-39: if the live page ends up showing a
    DIFFERENT job's requisition token than the session was opened for, the
    session pauses for review rather than filling the wrong form."""
    from tests.browser_fixtures import job_identity_pages
    from app.applications import browser_assist, browser_runtime

    original_url, other_job_url = job_identity_pages(tmp_path)
    job, execution_id = _prepared(original_url)
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(other_job_url))

        resumed = browser_assist.resume_session(session_id)
        assert resumed["session"]["status"] == "PAUSED_JOB_IDENTITY_MISMATCH"
    finally:
        _close(session_id)
