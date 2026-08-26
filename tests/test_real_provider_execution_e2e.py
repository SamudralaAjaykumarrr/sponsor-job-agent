"""Real Provider Execution V1: real-Chromium E2E coverage for the Greenhouse,
Lever, Workday, and Ashby ASSIST execution flows.

Every URL is a LOCAL `file://` fixture (tests/browser_fixtures.py's
`greenhouse_like_*` / `lever_like_*` / `workday_like_*` / `ashby_like_*`
pages, shaped from each provider's genuine documented/observed field names).
No real employer is ever contacted, no network access is required, and
nothing is ever submitted anywhere -- the whole point of this suite is
proving the flow stops safely before that.

Workday + Ashby Provider Execution V1 added `workday`/`ashby` to `PROVIDERS`
below: this whole suite was already written provider-agnostically (it drives
`app.applications.browser_runtime`'s genuinely provider-agnostic DOM engine,
never per-provider code), so extending coverage to two more providers is
purely a matter of adding their fixture pages -- no test logic changes.

Marked `browser`: skipped automatically unless Playwright AND its Chromium
binary are genuinely launchable.
"""

import json as _json

import pytest

from app import config

pytestmark = pytest.mark.browser

PROVIDERS = ("greenhouse", "lever", "workday", "ashby")


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
    """A FULL_TIME, CONFIRMED_SPONSOR job with a real generated resume
    artifact on disk and a queued execution -- the only shape that is
    allowed a browser session at all."""
    from app.applications import repo as executions_repo
    from app.candidate.profile import save_profile
    from app.jobs_repo import get_job, insert_job, update_job
    from app.models import ApplicationState, Job, SponsorshipStatus

    save_profile(sample_profile)
    counter = {"n": 0}

    def _make(url: str, *, provider: str = "greenhouse", title="Backend Software Engineer",
              company="Acme Corp", sponsorship=SponsorshipStatus.CONFIRMED_SPONSOR,
              employment_type="full_time", variant_id: str = "var_e2e"):
        counter["n"] += 1
        job = Job(
            title=title, company=company, location="Remote - US",
            description="Full-time role. H-1B sponsorship is available.", employment_type=employment_type,
            sponsorship_status=sponsorship, technical_match_score=80.0,
            application_state=ApplicationState.READY_TO_APPLY, provider=provider,
            external_job_id=f"rpe-{counter['n']}", company_identifier="acme",
            canonical_url=url, url=url,
        )
        job_id = insert_job(job)
        job_dir = tmp_env["output_dir"] / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "resume.pdf").write_bytes(f"%PDF-1.4 tailored resume for job {job_id}".encode())
        (job_dir / "resume.docx").write_bytes(b"fake docx")
        (job_dir / "application_answers.json").write_text(_json.dumps({"full_name": "Test Candidate"}))
        update_job(job_id, resume_pdf_path=str(job_dir / "resume.pdf"),
                   resume_docx_path=str(job_dir / "resume.docx"),
                   application_answers_path=str(job_dir / "application_answers.json"),
                   promoted_resume_variant_id=variant_id)
        execution_id = executions_repo.create_execution(job_id, provider=provider, mode="ASSIST")
        return get_job(job_id), execution_id

    return _make


def _close(session_id: str) -> None:
    from app.applications import browser_assist

    browser_assist.close_session(session_id)


def _fixture(provider: str, kind: str):
    import tests.browser_fixtures as fixtures

    return getattr(fixtures, f"{provider}_like_{kind}")


# Each provider's real resume-upload field `name` and its unknown-question
# field `id`, per tests/browser_fixtures.py's `_provider_form_page` layouts --
# the field *names* genuinely differ per provider (the normalized form model
# matches on LABEL text, never on these), so a handful of DOM-querying tests
# below must look up the right one per provider rather than assuming
# Greenhouse/Lever's specific names.
_RESUME_FIELD_NAME = {"greenhouse": "resume", "lever": "resume", "workday": "resumeAttachment",
                       "ashby": "_systemfield_resume"}
_UNKNOWN_FIELD_ID = {"greenhouse": "gh_unknown", "lever": "lever_unknown", "workday": "wd_unknown",
                      "ashby": "ashby_unknown"}


# =============================================================================
# Form discovery + standard/known field mapping
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_real_provider_form_is_discovered_and_standard_fields_are_filled(tmp_path, _prepared, provider):
    from app.applications import browser_assist

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        assert session["unresolved_field_count"] == 0
        # name/email(/phone) + resume upload + the KNOWN sponsorship question
        assert session["mapped_field_count"] >= 4
        assert session["form_fingerprint"]
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_known_custom_sponsorship_question_is_answered_truthfully(tmp_path, _prepared, provider):
    """The fixture candidate genuinely requires sponsorship. The radio GROUP's
    question lives in its fieldset legend (as on the real forms), and the
    answer selected must be the truthful one, never the convenient one."""
    from app.applications import browser_assist, browser_runtime

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        checked = live.run(lambda: live.page.evaluate(
            "() => Array.from(document.querySelectorAll('input[type=radio]'))"
            ".filter(el => el.checked).map(el => el.value)"
        ))
        assert checked == ["Yes"]
    finally:
        _close(session_id)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_resume_upload_field_is_populated_on_the_real_form(tmp_path, _prepared, provider):
    from app.applications import browser_assist, browser_runtime

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        field_name = _RESUME_FIELD_NAME[provider]
        uploaded = live.run(lambda: live.page.evaluate(
            "(name) => { const el = document.querySelector(`input[type=file][name=${name}]`);"
            "return el && el.files.length ? el.files[0].name : ''; }",
            field_name,
        ))
        assert uploaded == "resume.pdf"
    finally:
        _close(session_id)


# =============================================================================
# DOCUMENT UPLOAD binding -- the exact artifact, provably
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_exact_job_specific_resume_artifact_is_bound_on_upload(tmp_path, _prepared, provider):
    import hashlib

    from app.applications import browser_assist, document_binding

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider, variant_id="var_bound")
    session = browser_assist.start_session(execution_id)["session"]
    try:
        bindings = document_binding.list_bindings_for_session(session["session_id"])
        resume_bindings = [b for b in bindings if b["document_kind"] == "RESUME"]
        assert len(resume_bindings) == 1
        binding = resume_bindings[0]
        expected = hashlib.sha256(open(job.resume_pdf_path, "rb").read()).hexdigest()
        assert binding["job_id"] == job.id
        assert binding["artifact_sha256"] == expected
        assert binding["artifact_filename"] == "resume.pdf"
        assert binding["provider_field_id"] == _RESUME_FIELD_NAME[provider]
        assert binding["resume_variant_id"] == "var_bound"
        assert binding["provider"] == provider
        assert binding["execution_id"] == execution_id
        assert binding["verified"] == 1
        assert binding["checkpoint"].startswith("browser_assist:")
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_exact_job_specific_cover_letter_artifact_is_bound_on_upload(tmp_path, tmp_env, _prepared, provider):
    """Same guarantee as the resume binding test above, for the cover-letter
    document kind -- "do the same for cover letter when present"."""
    import hashlib

    from app.applications import browser_assist, document_binding
    from app.jobs_repo import update_job

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider, variant_id="var_cover")
    # Mirrors the resume artifact's own /<job_id>/ path-ownership convention
    # (app.applications.document_binding.verify_artifact_matches_job).
    job_dir = tmp_env["output_dir"] / str(job.id)
    cover_path = job_dir / "cover_letter.pdf"
    cover_path.write_bytes(b"%PDF-1.4 tailored cover letter")
    update_job(job.id, cover_letter_path=str(cover_path))
    session = browser_assist.start_session(execution_id)["session"]
    try:
        bindings = document_binding.list_bindings_for_session(session["session_id"])
        cover_bindings = [b for b in bindings if b["document_kind"] == "COVER_LETTER"]
        assert len(cover_bindings) == 1
        binding = cover_bindings[0]
        expected = hashlib.sha256(cover_path.read_bytes()).hexdigest()
        assert binding["job_id"] == job.id
        assert binding["artifact_sha256"] == expected
        assert binding["artifact_filename"] == "cover_letter.pdf"
        assert binding["provider"] == provider
        assert binding["execution_id"] == execution_id
        assert binding["verified"] == 1
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_another_jobs_resume_is_never_bound_as_verified(tmp_path, _prepared, provider):
    """The "never silently substitute another resume" guard, end to end: if
    a job's resume path somehow points at a DIFFERENT job's artifact, the
    binding is recorded UNVERIFIED with the reason rather than silently
    accepted (or silently dropped)."""
    from app.applications import browser_assist, document_binding
    from app.jobs_repo import update_job

    url_a = _fixture(provider, "application_page")(tmp_path)
    job_a, _exec_a = _prepared(url_a, provider=provider)
    job_b, execution_b = _prepared(url_a, provider=provider)
    update_job(job_b.id, resume_pdf_path=job_a.resume_pdf_path)

    result = browser_assist.start_session(execution_b)
    # The pre-session ownership check refuses outright -- no session at all.
    assert result["created"] is False
    assert "does not correspond to this job" in result["reason"]
    assert document_binding.list_bindings_for_job(job_b.id) == []


# =============================================================================
# Unknown question -> NEEDS_USER_INPUT
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_unknown_employer_question_pauses_for_user_input(tmp_path, _prepared, provider):
    from app.applications import blockers, browser_assist
    from app.applications import repo as executions_repo

    url = _fixture(provider, "application_page")(tmp_path, with_unknown_question=True)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        assert session["status"] == "PAUSED_UNKNOWN_FIELD"
        assert session["needs_user_action"] == 1
        assert session["unresolved_field_count"] >= 1
        blocker = blockers.get_active_blocker_for_execution(execution_id)
        assert blocker is not None
        assert blocker["blocker_code"] == "NEEDS_USER_INPUT"
        assert blockers.blocker_class_for(
            blockers.BlockerCode.NEEDS_USER_INPUT) == blockers.BlockerClass.RESUMABLE
        assert executions_repo.get_execution(execution_id) is not None
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_unknown_question_is_never_answered_with_a_fabricated_value(tmp_path, _prepared, provider):
    from app.applications import browser_assist, browser_runtime

    url = _fixture(provider, "application_page")(tmp_path, with_unknown_question=True)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        field_id = _UNKNOWN_FIELD_ID[provider]
        value = live.run(lambda: live.page.evaluate(
            "(id) => { const el = document.getElementById(id);"
            "return el ? el.value : null; }",
            field_id,
        ))
        assert value == ""
    finally:
        _close(session_id)


# =============================================================================
# CAPTCHA / auth / expired -- never bypassed, always a durable blocker
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_captcha_pauses_with_a_needs_captcha_blocker(tmp_path, _prepared, provider):
    from app.applications import blockers, browser_assist

    url = _fixture(provider, "captcha_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        assert session["status"] == "PAUSED_CAPTCHA"
        assert session["needs_user_action"] == 1
        blocker = blockers.get_active_blocker_for_execution(execution_id)
        assert blocker["blocker_code"] == "NEEDS_CAPTCHA"
        assert blocker["blocker_class"] == "RESUMABLE"
        # Nothing was filled -- the flow stopped before touching the form.
        assert (session["mapped_field_count"] or 0) == 0
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_otp_challenge_pauses_with_a_needs_otp_blocker(tmp_path, _prepared, provider):
    """OTP/CAPTCHA -> human intervention only. Distinct from the plain
    password sign-in wall: this exercises the MFA-phrase detection path
    (`app.applications.browser_runtime._MFA_PHRASES`), proven here to be
    genuinely provider-agnostic across all four providers, not something
    added only for Workday/Ashby."""
    from app.applications import blockers, browser_assist

    url = _fixture(provider, "otp_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        assert session["status"] == "PAUSED_MFA_REQUIRED"
        assert session["needs_user_action"] == 1
        blocker = blockers.get_active_blocker_for_execution(execution_id)
        assert blocker["blocker_code"] == "NEEDS_OTP"
        assert blockers.blocker_class_for(blockers.BlockerCode.NEEDS_OTP) == blockers.BlockerClass.RESUMABLE
        # Nothing was filled and no code was ever typed in.
        assert (session["mapped_field_count"] or 0) == 0
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_login_wall_pauses_with_a_needs_auth_blocker(tmp_path, _prepared, provider):
    from app.applications import blockers, browser_assist

    url = _fixture(provider, "login_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        assert session["status"] == "PAUSED_LOGIN_REQUIRED"
        blocker = blockers.get_active_blocker_for_execution(execution_id)
        assert blocker["blocker_code"] == "NEEDS_AUTH"
        assert blocker["required_action"] == "SIGN_IN_AND_CONTINUE"
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_no_password_is_ever_typed_into_a_login_wall(tmp_path, _prepared, provider):
    from app.applications import browser_assist, browser_runtime

    url = _fixture(provider, "login_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        values = live.run(lambda: live.page.evaluate(
            "() => Array.from(document.querySelectorAll('input')).map(el => el.value)"
        ))
        assert all(v == "" for v in values)
    finally:
        _close(session_id)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_expired_posting_page_never_reaches_a_form(tmp_path, _prepared, provider):
    """A closed posting has no form and no safe apply control -- the session
    surfaces exactly why instead of reporting an empty, healthy-looking
    session."""
    from app.applications import browser_assist

    url = _fixture(provider, "expired_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        assert session["status"].startswith("PAUSED_")
        assert session["needs_user_action"] == 1
        assert (session["mapped_field_count"] or 0) == 0
    finally:
        _close(session["session_id"])


# =============================================================================
# Form drift / stale authorization
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_form_change_pauses_and_invalidates_a_recorded_approval(tmp_path, _prepared, provider):
    """The employer changed the form after we mapped it. Two things must
    happen: the session pauses PAUSED_FORM_CHANGED, and any approval
    recorded against the OLD fingerprint stops being current."""
    from app.applications import approval as approval_mod
    from app.applications import browser_assist, browser_runtime
    from app.applications import repo as executions_repo
    from app.applications.models import ExecutionStatus
    from app.jobs_repo import get_job

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        original_fingerprint = session["form_fingerprint"]
        assert original_fingerprint

        executions_repo.update_execution(
            execution_id, job.id, ExecutionStatus.SUBMISSION_READY,
            form_fingerprint=original_fingerprint, answers_version=7,
        )
        execution = executions_repo.get_execution(execution_id)
        approval_mod._record_approval_row(get_job(job.id), execution, provider_submission_supported=False)
        approval = approval_mod.get_latest_approval(execution_id)
        valid, reasons = approval_mod.is_current_valid(get_job(job.id), execution, approval)
        assert valid is True, reasons

        # The ATS changes the form under us.
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(_fixture(provider, "form_changed_page")(tmp_path)))
        resumed = browser_assist.resume_session(session_id)
        assert resumed["session"]["status"] == "PAUSED_FORM_CHANGED"
        changed_fingerprint = resumed["session"]["form_fingerprint"]
        assert changed_fingerprint != original_fingerprint

        executions_repo.update_execution(execution_id, job.id, ExecutionStatus.SUBMISSION_READY,
                                          form_fingerprint=changed_fingerprint)
        execution = executions_repo.get_execution(execution_id)
        valid, reasons = approval_mod.is_current_valid(get_job(job.id), execution, approval)
        assert valid is False
        assert any("form changed since approval" in r for r in reasons)
    finally:
        _close(session_id)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_job_identity_mismatch_pauses_before_any_form_is_filled(tmp_path, _prepared, provider):
    from tests.browser_fixtures import provider_like_identity_mismatch_pages
    from app.applications import browser_assist, browser_runtime

    session_url, other_url = provider_like_identity_mismatch_pages(tmp_path, provider=provider)
    job, execution_id = _prepared(session_url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(other_url))
        resumed = browser_assist.resume_session(session_id)
        assert resumed["session"]["status"] == "PAUSED_JOB_IDENTITY_MISMATCH"
        assert (resumed["session"]["mapped_field_count"] or 0) == 0
    finally:
        _close(session_id)


# =============================================================================
# Blocker -> resolve -> resume -> revalidate
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_blocker_resolution_revalidates_the_current_page_state(tmp_path, _prepared, provider):
    """The brief's blocker/resume requirement: after the user resolves the
    blocking condition, the flow re-discovers the CURRENT page rather than
    replaying a stale mapping -- and never restarts from scratch when the
    window is still live."""
    from app.applications import blockers, browser_assist, browser_runtime

    captcha_url = _fixture(provider, "captcha_page")(tmp_path)
    job, execution_id = _prepared(captcha_url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        assert session["status"] == "PAUSED_CAPTCHA"
        assert blockers.get_active_blocker_for_execution(execution_id)["blocker_code"] == "NEEDS_CAPTCHA"

        # The candidate solves the challenge themselves in the visible
        # window; the page becomes the ordinary application form.
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(_fixture(provider, "application_page")(tmp_path)))

        resumed = browser_assist.mark_user_action_complete(session_id)
        assert resumed["ok"] is True
        assert resumed["session"]["status"] == "READY_FOR_FINAL_SUBMIT"
        assert resumed["session"]["mapped_field_count"] >= 4
        # The same session continued -- never reconstructed from scratch.
        assert (resumed["session"]["reconstructed_count"] or 0) == 0
        assert blockers.get_active_blocker_for_execution(execution_id) is None
    finally:
        _close(session_id)


# =============================================================================
# Confirmation -- and the boundary that must never be crossed
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_confirmation_page_is_parsed_and_records_a_receipt(tmp_path, _prepared, provider):
    from app.applications import browser_assist, browser_runtime, receipts
    from app.applications import repo as executions_repo

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(_fixture(provider, "confirmation_page")(tmp_path)))

        result = browser_assist.attempt_user_submit_reconciliation(session_id)
        assert result["ok"] is True
        assert result["session"]["status"] == "CONFIRMED"
        assert result["session"]["confirmation_evidence_strength"] in ("STRONG", "MODERATE")

        execution = executions_repo.get_execution(execution_id)
        assert execution["status"] == "APPLIED"
        receipt = receipts.get_latest_receipt_for_execution(execution_id)
        assert receipt is not None
        assert receipt["provider"] == provider
        assert receipt["submitted_via"] == f"browser_assist:{provider}"
        assert receipt["raw_message_fingerprint"]
    finally:
        _close(session_id)


def test_greenhouse_confirmation_id_is_captured_when_the_page_shows_one(tmp_path, _prepared):
    from app.applications import browser_assist, browser_runtime
    from app.applications import repo as executions_repo

    url = _fixture("greenhouse", "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider="greenhouse")
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(_fixture("greenhouse", "confirmation_page")(tmp_path)))
        browser_assist.attempt_user_submit_reconciliation(session_id)
        execution = executions_repo.get_execution(execution_id)
        assert execution["confirmation_id"] == "GH-2026-88134"
    finally:
        _close(session_id)


def test_lever_post_apply_page_without_an_id_never_fabricates_one(tmp_path, _prepared):
    """Regression guard: "Application received" must NOT yield "received" as
    a confirmation id, and with no id the grade stays MODERATE.

    This test's NAME matters: pytest derives `tmp_path` from it, `tmp_path`
    becomes part of the `file://` URL, and
    `app.applications.confirmation_evidence.url_looks_like_confirmation()`
    reads that URL -- so a name containing "confirmation"/"thank"/"success"/
    "received"/"complete" would itself supply the corroborating signal and
    silently upgrade the result to STRONG."""
    from app.applications import browser_assist, browser_runtime
    from app.applications import repo as executions_repo

    url = _fixture("lever", "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider="lever")
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        live = browser_runtime._REGISTRY[session_id]
        live.run(lambda: live.page.goto(_fixture("lever", "confirmation_page")(tmp_path)))
        result = browser_assist.attempt_user_submit_reconciliation(session_id)
        assert result["session"]["confirmation_evidence_strength"] == "MODERATE"
        execution = executions_repo.get_execution(execution_id)
        assert execution["confirmation_id"] == ""
    finally:
        _close(session_id)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_reaching_the_form_never_marks_the_execution_applied(tmp_path, _prepared, provider):
    """The single most important negative: a fully prepared, submit-button-
    located session is NEVER an application. `READY_FOR_FINAL_SUBMIT` means
    "report it and stop"."""
    from app.applications import browser_assist, receipts
    from app.applications import repo as executions_repo

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        assert session["status"] == "READY_FOR_FINAL_SUBMIT"
        execution = executions_repo.get_execution(execution_id)
        assert execution["status"] != "APPLIED"
        assert execution["status"] != "SUBMITTED"
        assert receipts.list_receipts_for_execution(execution_id) == []
    finally:
        _close(session["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_final_submit_control_is_located_but_never_clicked(tmp_path, _prepared, provider):
    from app.applications import browser_assist, browser_runtime

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    session_id = session["session_id"]
    try:
        outcome = browser_runtime.rediscover(session_id)
        assert outcome.submit_button is not None
        assert "submit" in outcome.submit_button["text"].lower()
        # Still on the form page -- the control was found, never activated.
        live = browser_runtime._REGISTRY[session_id]
        still_on_form = live.run(lambda: live.page.evaluate(
            "() => !!document.getElementById('application-form')"))
        assert still_on_form is True
    finally:
        _close(session_id)


# =============================================================================
# Hard gates that a real provider flow must never weaken
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_non_full_time_job_never_gets_a_browser_session(tmp_path, _prepared, provider):
    from app.applications import browser_assist

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider, employment_type="contract",
                                   title="Backend Engineer (Contract, C2C)")
    result = browser_assist.start_session(execution_id)
    assert result["created"] is False
    assert result["hard_skip"] is True


@pytest.mark.parametrize("provider", PROVIDERS)
def test_no_sponsorship_job_never_gets_a_browser_session(tmp_path, _prepared, provider):
    from app.applications import browser_assist
    from app.models import SponsorshipStatus

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider, sponsorship=SponsorshipStatus.NO_SPONSORSHIP)
    result = browser_assist.start_session(execution_id)
    assert result["created"] is False
    assert result["hard_skip"] is True


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_second_concurrent_session_for_the_same_job_is_prevented(tmp_path, _prepared, provider):
    from app.applications import browser_assist, browser_session

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    first = browser_assist.start_session(execution_id)["session"]
    try:
        second = browser_assist.start_session(execution_id)
        assert second["session"]["session_id"] == first["session_id"]
        active = browser_session.get_active_session_for_job(job.id)
        assert active["session_id"] == first["session_id"]
    finally:
        _close(first["session_id"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_no_candidate_pii_is_written_into_the_session_row(tmp_path, _prepared, provider):
    from app.applications import browser_assist, browser_session

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        row = browser_session.get_session(session["session_id"])
        blob = _json.dumps({k: str(v) for k, v in row.items()})
        for secret in ("test.candidate@example.com", "555-000-1111", "Test Candidate", "password"):
            assert secret not in blob, secret
    finally:
        _close(session["session_id"])


# =============================================================================
# Doctor stays clean through a full real-provider flow
# =============================================================================

@pytest.mark.parametrize("provider", PROVIDERS)
def test_application_doctor_reports_nothing_serious_after_a_full_flow(tmp_path, _prepared, provider):
    from app.applications import browser_assist
    from app.applications.doctor import run_doctor

    url = _fixture(provider, "application_page")(tmp_path)
    job, execution_id = _prepared(url, provider=provider)
    session = browser_assist.start_session(execution_id)["session"]
    try:
        report = run_doctor()
        assert report.serious_count == 0, [i.detail for i in report.issues if i.severity == "serious"]
    finally:
        _close(session["session_id"])
