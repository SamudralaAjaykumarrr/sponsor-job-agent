"""CLAUDE.md Phase 10 sections 1-2, 6-8, 10, 40-42, 51: the session-based
browser-assist orchestration layer (app.applications.browser_assist's
start_session/resume_session/mark_user_action_complete/advance_step/
close_session/attempt_user_submit_reconciliation). app.applications.
browser_runtime is mocked throughout -- these tests exercise the STATE
MACHINE and GATE ENFORCEMENT, not real Chromium (see
tests/test_browser_assist_e2e.py, marked `browser`, for that)."""

import json

import pytest

from app import config
from app.applications import browser_assist, browser_runtime, browser_session
from app.applications import repo as executions_repo
from app.candidate.profile import save_profile
from app.jobs_repo import get_job, insert_job, update_job
from app.models import ApplicationState, Job, SponsorshipStatus


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)


def _prepared_execution(tmp_env, sample_profile, *, employment_type="FULL_TIME",
                         sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
                         application_state=ApplicationState.READY_TO_APPLY,
                         provider="never_configured_provider", canonical_url="https://boards.greenhouse.io/acme/jobs/1",
                         with_resume=True) -> tuple[Job, str]:
    save_profile(sample_profile)
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="We are hiring a Backend Software Engineer. Full-time. H-1B sponsorship is available.",
        employment_type=employment_type, sponsorship_status=sponsorship_status,
        technical_match_score=80.0, application_state=application_state,
        provider=provider, external_job_id="1", canonical_url=canonical_url, url=canonical_url,
    )
    job_id = insert_job(job)

    if with_resume:
        job_dir = tmp_env["output_dir"] / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
        (job_dir / "resume.docx").write_bytes(b"fake docx")
        (job_dir / "application_answers.json").write_text(json.dumps({
            "full_name": "Test Candidate", "email": "test.candidate@example.com", "phone": "555-000-1111",
            "do_you_require_sponsorship": "No",
        }))
        update_job(
            job_id, resume_pdf_path=str(job_dir / "resume.pdf"), resume_docx_path=str(job_dir / "resume.docx"),
            application_answers_path=str(job_dir / "application_answers.json"),
        )

    # Create the execution row directly (rather than via queue_application)
    # so these tests exercise start_session's OWN independent
    # re-verification of eligibility -- the same defense-in-depth pattern
    # app.applications.executor.process_execution already uses -- rather
    # than relying on queue_application's earlier gate, which would make it
    # impossible to even get an execution_id for the negative-gate scenarios.
    execution_id = executions_repo.create_execution(job_id, provider=provider, mode="ASSIST")
    return get_job(job_id), execution_id


def _simple_form_outcome(**overrides) -> browser_runtime.DiscoveryOutcome:
    defaults = dict(
        pause_reason=None, current_url="https://boards.greenhouse.io/acme/jobs/1",
        fields=[
            {"index": 0, "label": "Full Name", "name": "full_name", "type": "text", "required": True, "choices": []},
            {"index": 1, "label": "Email", "name": "email", "type": "text", "required": True, "choices": []},
        ],
        fingerprint="fp-1", submit_button={"text": "Submit Application", "id": "submit-btn"},
        next_button=None, total_steps_hint=1,
    )
    defaults.update(overrides)
    return browser_runtime.DiscoveryOutcome(**defaults)


def _fill_outcome(**overrides) -> browser_runtime.FillOutcome:
    defaults = dict(filled=["Full Name", "Email"], unresolved=[], uploads=[])
    defaults.update(overrides)
    return browser_runtime.FillOutcome(**defaults)


# --- gate enforcement (acceptance scenarios B/C, CLAUDE.md Phase 10 section 53-54) --

def test_contract_job_never_gets_a_browser_session(tmp_env, sample_profile):
    job, execution_id = _prepared_execution(tmp_env, sample_profile, employment_type="CONTRACT")
    result = browser_assist.start_session(execution_id)
    assert result["created"] is False
    assert browser_session.get_active_session_for_job(job.id) is None


def test_part_time_job_never_gets_a_browser_session(tmp_env, sample_profile):
    job, execution_id = _prepared_execution(tmp_env, sample_profile, employment_type="Part-time")
    result = browser_assist.start_session(execution_id)
    assert result["created"] is False
    assert browser_session.get_active_session_for_job(job.id) is None


def test_no_sponsorship_job_never_gets_a_browser_session(tmp_env, sample_profile):
    job, execution_id = _prepared_execution(tmp_env, sample_profile, sponsorship_status=SponsorshipStatus.NO_SPONSORSHIP)
    result = browser_assist.start_session(execution_id)
    assert result["created"] is False
    assert browser_session.get_active_session_for_job(job.id) is None


def test_unknown_sponsorship_job_never_gets_a_browser_session(tmp_env, sample_profile):
    job, execution_id = _prepared_execution(tmp_env, sample_profile, sponsorship_status=SponsorshipStatus.UNKNOWN)
    result = browser_assist.start_session(execution_id)
    assert result["created"] is False


def test_likely_sponsor_job_may_start_a_session(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile, sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    result = browser_assist.start_session(execution_id)
    assert result["created"] is True


def test_missing_resume_artifact_blocks_session_start(tmp_env, sample_profile):
    job, execution_id = _prepared_execution(tmp_env, sample_profile, with_resume=False)
    result = browser_assist.start_session(execution_id)
    assert result["created"] is False
    assert "resume" in result["reason"].lower()


# --- happy path / pause reasons ---------------------------------------------

def test_full_time_confirmed_job_reaches_ready_for_final_submit(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())

    result = browser_assist.start_session(execution_id)
    assert result["created"] is True
    session = result["session"]
    assert session["status"] == "READY_FOR_FINAL_SUBMIT"
    assert session["mapped_field_count"] == 2
    assert session["unresolved_field_count"] == 0


def test_starting_a_session_twice_reuses_the_existing_one(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())

    first = browser_assist.start_session(execution_id)
    second = browser_assist.start_session(execution_id)
    assert first["session"]["session_id"] == second["session"]["session_id"]


def test_captcha_pauses_session(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(pause_reason="CAPTCHA_PRESENT", fields=[]))
    result = browser_assist.start_session(execution_id)
    assert result["session"]["status"] == "PAUSED_CAPTCHA"
    assert result["session"]["needs_user_action"] == 1
    assert result["session"]["user_action_reason"] == "CAPTCHA_PRESENT"


def test_login_required_pauses_session(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(pause_reason="LOGIN_REQUIRED", fields=[]))
    result = browser_assist.start_session(execution_id)
    assert result["session"]["status"] == "PAUSED_LOGIN_REQUIRED"


def test_platform_restricted_pauses_session_on_unexpected_domain(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(pause_reason="PLATFORM_POLICY_RESTRICTED", fields=[]))
    result = browser_assist.start_session(execution_id)
    assert result["session"]["status"] == "PAUSED_PLATFORM_RESTRICTED"


def test_unresolved_legal_field_pauses_with_legal_question_reason(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    outcome = _simple_form_outcome(fields=[
        {"index": 0, "label": "Have you ever been convicted of a felony", "name": "criminal_history",
         "type": "text", "required": True, "choices": []},
    ])
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: outcome)
    monkeypatch.setattr(browser_runtime, "fill_fields",
                         lambda *a, **k: _fill_outcome(filled=[], unresolved=["Have you ever been convicted of a felony"]))
    result = browser_assist.start_session(execution_id)
    assert result["session"]["status"] == "PAUSED_LEGAL_QUESTION"


def test_unresolved_generic_field_pauses_with_unknown_field_reason(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    outcome = _simple_form_outcome(fields=[
        {"index": 0, "label": "Referral Source", "name": "referral_source", "type": "text",
         "required": True, "choices": []},
    ])
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: outcome)
    monkeypatch.setattr(browser_runtime, "fill_fields",
                         lambda *a, **k: _fill_outcome(filled=[], unresolved=["Referral Source"]))
    result = browser_assist.start_session(execution_id)
    assert result["session"]["status"] == "PAUSED_UNKNOWN_FIELD"


def test_no_submit_button_detected_leaves_session_active(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(submit_button=None))
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    result = browser_assist.start_session(execution_id)
    assert result["session"]["status"] == "ACTIVE"


# --- resume / mark-user-action-complete / crash recovery --------------------

def test_resume_session_live_rediscovers_current_page(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(pause_reason="LOGIN_REQUIRED", fields=[]))
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    assert start_result["session"]["status"] == "PAUSED_LOGIN_REQUIRED"

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: True)
    monkeypatch.setattr(browser_runtime, "rediscover", lambda sid: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())

    result = browser_assist.resume_session(session_id)
    assert result["ok"] is True
    assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"


def test_mark_user_action_complete_only_valid_from_paused_status(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    assert start_result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"

    result = browser_assist.mark_user_action_complete(session_id)
    assert result["ok"] is False


def test_resume_not_live_restarts_fresh_browser_when_safe(tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md Phase 10 section 51, acceptance I: pre-submission crash
    recovery -- a fresh browser window at the same URL is safe."""
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(pause_reason="CAPTCHA_PRESENT", fields=[]))
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: False)
    reopen_calls = []

    def _reopen(sid, *, provider, url):
        reopen_calls.append((sid, provider, url))
        return _simple_form_outcome()

    monkeypatch.setattr(browser_runtime, "open_session", _reopen)
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())

    result = browser_assist.resume_session(session_id)
    assert result["ok"] is True
    assert len(reopen_calls) == 1
    assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"


def test_resume_not_live_while_awaiting_submit_becomes_status_unknown(tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md Phase 10 section 51, acceptance J: a browser lost while a
    submission may have been in flight is NEVER guessed -- it becomes
    SUBMISSION_STATUS_UNKNOWN."""
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]

    browser_session.update_session(session_id, status="AWAITING_USER_SUBMIT")
    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: False)

    result = browser_assist.resume_session(session_id)
    assert result["ok"] is False
    assert result["session"]["status"] == "SUBMISSION_STATUS_UNKNOWN"


def test_resume_on_terminal_session_is_a_no_op(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    browser_assist.close_session(session_id)

    result = browser_assist.resume_session(session_id)
    assert result["ok"] is True
    assert result["session"]["status"] == "CLOSED"


def test_form_changed_pauses_session_instead_of_silently_remapping(tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md Phase 10 section 33: never reuse a stale mapping blindly."""
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome(fingerprint="fp-A"))
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    assert start_result["session"]["form_fingerprint"] == "fp-A"

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: True)
    monkeypatch.setattr(browser_runtime, "rediscover", lambda sid: _simple_form_outcome(fingerprint="fp-B"))

    result = browser_assist.resume_session(session_id)
    assert result["session"]["status"] == "PAUSED_FORM_CHANGED"
    assert result["session"]["user_action_reason"] == "FORM_CHANGED"


# --- multi-step advance ------------------------------------------------------

def test_advance_step_requires_live_browser(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(submit_button=None, next_button={"text": "Next", "id": "next"}))
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    assert start_result["session"]["status"] == "ACTIVE"

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: False)
    result = browser_assist.advance_step(session_id)
    assert result["ok"] is False


def test_advance_step_progresses_current_step_and_rediscovers(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session",
                         lambda *a, **k: _simple_form_outcome(submit_button=None, next_button={"text": "Next", "id": "next"}))
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: True)
    monkeypatch.setattr(browser_runtime, "advance_step", lambda sid: {"advanced": True, "current_step": 2})
    monkeypatch.setattr(browser_runtime, "rediscover", lambda sid: _simple_form_outcome())

    result = browser_assist.advance_step(session_id)
    assert result["ok"] is True
    assert result["session"]["current_step"] == 2
    assert result["session"]["status"] == "READY_FOR_FINAL_SUBMIT"


# --- post-manual-submit confirmation capture --------------------------------

def test_reconciliation_confirms_execution_applied(tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md Phase 10 sections 40-42, acceptance K."""
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    browser_session.update_session(session_id, status="AWAITING_USER_SUBMIT")

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: True)
    monkeypatch.setattr(browser_runtime, "capture_confirmation", lambda sid: browser_runtime.ConfirmationOutcome(
        confirmed=True, current_url="https://boards.greenhouse.io/acme/jobs/1/confirmation",
        confirmation_id="CONF-123", confirmation_text_fingerprint="abc123",
    ))
    monkeypatch.setattr(browser_runtime, "close_session", lambda sid: None)

    result = browser_assist.attempt_user_submit_reconciliation(session_id)
    assert result["ok"] is True
    assert result["session"]["status"] == "CONFIRMED"

    execution = executions_repo.get_execution(execution_id)
    assert execution["status"] == "APPLIED"
    assert execution["confirmation_id"] == "CONF-123"
    refreshed_job = get_job(job.id)
    assert refreshed_job.application_state == ApplicationState.APPLIED


def test_reconciliation_no_evidence_yet_leaves_session_unchanged(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    browser_session.update_session(session_id, status="AWAITING_USER_SUBMIT")

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: True)
    monkeypatch.setattr(browser_runtime, "capture_confirmation",
                         lambda sid: browser_runtime.ConfirmationOutcome(confirmed=False))

    result = browser_assist.attempt_user_submit_reconciliation(session_id)
    assert result["ok"] is False
    assert browser_session.get_session(session_id)["status"] == "AWAITING_USER_SUBMIT"


def test_reconciliation_browser_not_live_marks_status_unknown(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]
    browser_session.update_session(session_id, status="AWAITING_USER_SUBMIT")

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: False)
    result = browser_assist.attempt_user_submit_reconciliation(session_id)
    assert result["ok"] is False
    assert result["session"]["status"] == "SUBMISSION_STATUS_UNKNOWN"


# --- close / expire ----------------------------------------------------------

def test_close_session_always_safe(tmp_env, sample_profile, monkeypatch):
    job, execution_id = _prepared_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: _simple_form_outcome())
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: _fill_outcome())
    start_result = browser_assist.start_session(execution_id)
    session_id = start_result["session"]["session_id"]

    closed = browser_assist.close_session(session_id, reason="test close")
    assert closed["status"] == "CLOSED"
    # Calling close again must never raise.
    browser_assist.close_session(session_id)
