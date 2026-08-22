"""CLAUDE.md Phase 13: real Chromium-driven E2E tests for the formal
multi-signal job-identity gate (sections 4-10), provider assist health
(sections 11-12), session checkpoints (sections 37-39), and confirmation
evidence strength (sections 49-51). Marked `browser` -- skipped
automatically unless Playwright AND its Chromium binary are actually
launchable; every URL is a local `file://` fixture
(tests/browser_fixtures.py), never a real website."""

import json as _json

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
    from app.candidate.profile import save_profile
    from app.applications import repo as executions_repo
    from app.jobs_repo import get_job, insert_job, update_job
    from app.models import ApplicationState, Job, SponsorshipStatus

    save_profile(sample_profile)

    def _make(url: str, *, title="Backend Software Engineer", company="Acme Corp", location="Remote - US"):
        job = Job(
            title=title, company=company, location=location,
            description="Full-time role. H-1B sponsorship is available.", employment_type="full_time",
            sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, technical_match_score=80.0,
            application_state=ApplicationState.READY_TO_APPLY, provider="never_configured_phase13_e2e",
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
        execution_id = executions_repo.create_execution(job_id, provider="never_configured_phase13_e2e", mode="ASSIST")
        return get_job(job_id), execution_id

    return _make


def _close(session_id: str) -> None:
    from app.applications import browser_assist

    browser_assist.close_session(session_id)


def test_provider_health_healthy_after_successful_discovery(tmp_path, _prepared):
    from tests.browser_fixtures import simple_form_page
    from app.applications import browser_assist, provider_health

    url = simple_form_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        health = provider_health.get_health("never_configured_phase13_e2e")
        assert health["health"] == provider_health.ProviderAssistHealth.HEALTHY.value
    finally:
        _close(session["session_id"])


def test_provider_health_captcha_blocked_after_captcha_page(tmp_path, _prepared):
    from tests.browser_fixtures import captcha_page
    from app.applications import browser_assist, provider_health

    url = captcha_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_CAPTCHA"
        health = provider_health.get_health("never_configured_phase13_e2e")
        assert health["health"] == provider_health.ProviderAssistHealth.CAPTCHA_BLOCKED.value
    finally:
        _close(session["session_id"])


def test_checkpoints_recorded_through_normal_flow(tmp_path, _prepared):
    """multi_step_pages' first page has a "Next" control but no submit
    button -- lands ACTIVE with fields mapped, which should record
    FORM_DISCOVERED + FIELDS_PREPARED (distinct from a form that reaches
    READY_FOR_FINAL_SUBMIT in the very first discovery pass)."""
    from tests.browser_fixtures import multi_step_pages
    from app.applications import browser_assist, checkpoints

    page1, _page2 = multi_step_pages(tmp_path)
    job, execution_id = _prepared(page1)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        rows = checkpoints.list_checkpoints(session["session_id"])
        recorded = {r["checkpoint"] for r in rows}
        assert "FORM_DISCOVERED" in recorded
        assert "FIELDS_PREPARED" in recorded
    finally:
        _close(session["session_id"])


def test_checkpoint_ready_for_final_submit_recorded(tmp_path, _prepared):
    from tests.browser_fixtures import simple_form_page
    from app.applications import browser_assist, checkpoints

    url = simple_form_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        rows = checkpoints.list_checkpoints(session["session_id"])
        assert any(r["checkpoint"] == "READY_FOR_FINAL_SUBMIT" for r in rows)
    finally:
        _close(session["session_id"])


def test_checkpoint_recorded_on_user_action_pause(tmp_path, _prepared):
    from tests.browser_fixtures import login_page
    from app.applications import browser_assist, checkpoints

    url = login_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        rows = checkpoints.list_checkpoints(session["session_id"])
        assert any(r["checkpoint"] == "USER_ACTION_REQUIRED" for r in rows)
    finally:
        _close(session["session_id"])


def test_job_identity_evidence_recorded_verified_continues(tmp_path, _prepared):
    """simple_form_page carries a matching JSON-LD JobPosting block -- two
    independent signals (title + company) agree, so the verdict is VERIFIED
    and the flow continues unattended to READY_FOR_FINAL_SUBMIT."""
    from tests.browser_fixtures import simple_form_page
    from app.applications import browser_assist, job_identity

    url = simple_form_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        rows = job_identity.list_verifications(job_id=job.id)
        assert any(r["stage"] == "PRE_FINAL_SUBMIT" for r in rows)
        assert all(r["result"] == "VERIFIED" for r in rows)
    finally:
        _close(session["session_id"])


def test_job_identity_insufficient_blocks_unattended_final_submit(tmp_path, _prepared):
    """CLAUDE.md Phase 13 acceptance correction: a page with NO identity
    signal at all (no JSON-LD, no requisition token) must never be allowed
    to reach READY_FOR_FINAL_SUBMIT unattended -- INSUFFICIENT pauses the
    session, distinctly from a confirmed MISMATCH."""
    from tests.browser_fixtures import simple_form_page_no_identity
    from app.applications import browser_assist, job_identity

    url = simple_form_page_no_identity(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_JOB_IDENTITY_UNVERIFIED"
        assert session["needs_user_action"] == 1
        rows = job_identity.list_verifications(job_id=job.id)
        assert any(r["stage"] == "PRE_FINAL_SUBMIT" and r["result"] == "INSUFFICIENT" for r in rows)
    finally:
        _close(session["session_id"])


def test_job_identity_probable_blocks_unattended_final_submit(tmp_path, _prepared):
    """CLAUDE.md Phase 13 acceptance correction: exactly one signal
    available and it matches (PROBABLE) is still not enough confidence to
    continue unattended -- only VERIFIED (2+ independent signals, or a
    matching requisition id) may."""
    from tests.browser_fixtures import jsonld_job_posting_page
    from app.applications import browser_assist, job_identity

    # Matching company only. The job needs a real, CS/STEM-classified title
    # to be eligible at all (an empty title fails role classification), so
    # instead the fixture's JSON-LD leaves `title` BLANK -- observed title
    # is then empty and title comparison is skipped entirely (never
    # mismatched), leaving company as the ONLY comparable, matching signal.
    url = jsonld_job_posting_page(tmp_path, title="", company="Acme Corp")
    job, execution_id = _prepared(url, title="Backend Software Engineer", company="Acme Corp")
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_JOB_IDENTITY_UNVERIFIED"
        assert session["needs_user_action"] == 1
        rows = job_identity.list_verifications(job_id=job.id)
        assert any(r["result"] == "PROBABLE" for r in rows)
    finally:
        _close(session["session_id"])


def test_job_identity_ambiguous_blocks_unattended_final_submit(tmp_path, _prepared):
    """CLAUDE.md Phase 13 acceptance correction: only the WEAK, non-
    corroborating `location` signal is comparable -- the job needs a real,
    CS/STEM-classified title/company to be eligible at all, so instead the
    fixture's JSON-LD leaves title/company BLANK (observed side empty ->
    those comparisons are skipped entirely, never mismatched) while
    location matches on both sides. Some very weak circumstantial evidence,
    never enough to be PROBABLE, and must never continue unattended."""
    from tests.browser_fixtures import jsonld_job_posting_page
    from app.applications import browser_assist, job_identity

    url = jsonld_job_posting_page(tmp_path, title="", company="", location="Remote - US")
    job, execution_id = _prepared(url, title="Backend Software Engineer", company="Acme Corp",
                                   location="Remote - US")
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_JOB_IDENTITY_UNVERIFIED"
        assert session["needs_user_action"] == 1
        rows = job_identity.list_verifications(job_id=job.id)
        assert any(r["result"] == "AMBIGUOUS" for r in rows)
    finally:
        _close(session["session_id"])


def test_job_identity_mismatch_from_jsonld_pauses_before_upload(tmp_path, _prepared):
    """CLAUDE.md Phase 13 sections 8-9: a real page's JSON-LD JobPosting
    naming a DIFFERENT company than the job this session was opened for
    stops the flow before the resume-upload field is ever filled."""
    from tests.browser_fixtures import jsonld_job_posting_page
    from app.applications import browser_assist, job_identity

    url = jsonld_job_posting_page(tmp_path, title="Backend Software Engineer", company="Totally Different Company")
    job, execution_id = _prepared(url, title="Backend Software Engineer", company="Acme Corp")
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] == "PAUSED_JOB_IDENTITY_MISMATCH"
        rows = job_identity.list_verifications(job_id=job.id)
        assert any(r["result"] == "MISMATCH" for r in rows)
        mismatch_row = next(r for r in rows if r["result"] == "MISMATCH")
        assert "company" in mismatch_row["signals_mismatched"]
    finally:
        _close(session["session_id"])


def test_job_identity_verified_from_matching_jsonld_continues(tmp_path, _prepared):
    """The mirror-image case: matching JSON-LD company/title lets the flow
    continue all the way to READY_FOR_FINAL_SUBMIT."""
    from tests.browser_fixtures import jsonld_job_posting_page
    from app.applications import browser_assist, job_identity

    url = jsonld_job_posting_page(tmp_path, title="Backend Software Engineer", company="Acme Corp")
    job, execution_id = _prepared(url, title="Backend Software Engineer", company="Acme Corp")
    result = browser_assist.start_session(execution_id)
    session = result["session"]
    try:
        assert session["status"] != "PAUSED_JOB_IDENTITY_MISMATCH"
        rows = job_identity.list_verifications(job_id=job.id)
        assert any(r["result"] == "VERIFIED" for r in rows)
    finally:
        _close(session["session_id"])


def test_confirmation_evidence_strength_recorded_as_strong(tmp_path, _prepared):
    from tests.browser_fixtures import simple_form_page, success_page
    from app.applications import browser_assist, browser_runtime, browser_session

    url = simple_form_page(tmp_path)
    job, execution_id = _prepared(url)
    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]
    try:
        # Simulate the candidate manually navigating to and submitting the
        # real form (this project never clicks final submit itself) --
        # point the live page at a success page carrying both a trusted
        # phrase and a confirmation number, then reconcile.
        live = browser_runtime._get_live(session_id)
        live.run(live.page.goto, success_page(tmp_path), timeout=15000)
        outcome = browser_assist.attempt_user_submit_reconciliation(session_id)
        assert outcome["ok"] is True
        session = browser_session.get_session(session_id)
        assert session["confirmation_evidence_strength"] == "STRONG"
    finally:
        _close(session_id)
