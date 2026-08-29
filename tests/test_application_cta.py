"""Unit coverage for the universal Apply CTA view-model
(application-action-experience-v1, app.applications.cta.compute_apply_cta).
Pure-function tests -- no DB, no TestClient -- proving every branch maps a
real internal state to the plain-language label the build brief specifies,
and that a raw internal enum name is never leaked into a label."""

from app.applications.browser_session import BrowserSessionStatus
from app.applications.cta import STYLE_NONE, STYLE_PRIMARY, STYLE_PROGRESS, STYLE_SECONDARY, STYLE_SUCCESS, STYLE_WAITING, compute_apply_cta
from app.applications.models import ExecutionStatus
from app.models import ApplicationState


def _exec(status: ExecutionStatus, **extra) -> dict:
    return {"status": status.value, "active": 1, **extra}


def test_skipped_states_have_no_cta_but_do_have_a_reason():
    for state in (
        ApplicationState.SKIPPED, ApplicationState.SKIPPED_NO_SPONSORSHIP,
        ApplicationState.SKIPPED_SENIORITY, ApplicationState.SKIPPED_COMPENSATION,
        ApplicationState.SKIPPED_POOR_MATCH,
    ):
        cta = compute_apply_cta(1, state.value)
        assert cta.style == STYLE_NONE
        assert cta.label == ""
        assert cta.reason
        assert cta.action["type"] == "none"


def test_no_execution_yet_shows_preparing_automatically():
    cta = compute_apply_cta(1, ApplicationState.DISCOVERED.value)
    assert cta.style == STYLE_WAITING
    assert cta.label == "Preparing automatically..."
    assert cta.action["type"] == "none"


def test_no_execution_review_required_shows_sponsorship_warning_reason():
    cta = compute_apply_cta(1, ApplicationState.REVIEW_REQUIRED.value)
    assert cta.style == STYLE_WAITING
    assert "sponsorship" in cta.reason.lower()


def test_no_execution_applied_state_shows_applied_checkmark():
    cta = compute_apply_cta(1, ApplicationState.APPLIED.value)
    assert cta.style == STYLE_SUCCESS
    assert "APPLIED" in cta.label


def test_queued_shows_preparing_application():
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.QUEUED))
    assert cta.style == STYLE_WAITING
    assert cta.label == "Preparing application..."


def test_form_stages_show_filling_application():
    for status in (ExecutionStatus.FORM_DISCOVERED, ExecutionStatus.FORM_MAPPED, ExecutionStatus.FORM_FILLED):
        cta = compute_apply_cta(1, None, execution=_exec(status))
        assert cta.style == STYLE_WAITING
        assert cta.label == "Filling application..."


def test_submission_ready_is_the_one_primary_approve_cta():
    cta = compute_apply_cta(42, None, execution=_exec(ExecutionStatus.SUBMISSION_READY))
    assert cta.style == STYLE_PRIMARY
    assert cta.label == "APPROVE & APPLY"
    assert cta.action == {"type": "approve", "job_id": 42, "href": "/jobs/42/applications/approve"}


def test_approved_without_browser_session_shows_ready_for_final_review():
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED))
    assert cta.style == STYLE_SECONDARY
    assert cta.label == "READY FOR FINAL REVIEW"
    assert "not verified" in cta.reason


def test_browser_session_unsupported_submission_pause_shows_ready_for_final_review():
    """Tsenta-parity-closure-v1 regression: a real gap the audit found --
    PAUSED_UNSUPPORTED_SUBMISSION/PAUSED_PLATFORM_RESTRICTED used to fall
    into the generic paused-session branch and show the misleading
    "ANSWER & CONTINUE" label, even though there is no question to answer --
    only a provider capability that hasn't been earned."""
    for status in (BrowserSessionStatus.PAUSED_UNSUPPORTED_SUBMISSION, BrowserSessionStatus.PAUSED_PLATFORM_RESTRICTED):
        session = {"session_id": "s1", "status": status.value}
        cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED), browser_session=session)
        assert cta.label == "READY FOR FINAL REVIEW"
        assert "not verified" in cta.reason


def test_approved_with_active_browser_session_shows_filling():
    session = {"session_id": "s1", "status": BrowserSessionStatus.ACTIVE.value}
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED), browser_session=session)
    assert cta.style == STYLE_WAITING
    assert cta.label == "Filling application..."


def test_approved_with_ready_for_final_submit_session_shows_continue_application():
    session = {"session_id": "s1", "status": BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value}
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED), browser_session=session)
    assert cta.label == "CONTINUE APPLICATION"
    assert cta.action["href"] == "/applications/browser-sessions/s1"


def test_browser_session_captcha_pause_maps_to_complete_captcha():
    session = {"session_id": "s1", "status": BrowserSessionStatus.PAUSED_CAPTCHA.value}
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED), browser_session=session)
    assert cta.label == "COMPLETE CAPTCHA"


def test_browser_session_login_pause_maps_to_sign_in_and_continue():
    session = {"session_id": "s1", "status": BrowserSessionStatus.PAUSED_LOGIN_REQUIRED.value}
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED), browser_session=session)
    assert cta.label == "SIGN IN & CONTINUE"


def test_browser_session_legal_pause_maps_to_review_and_confirm():
    session = {"session_id": "s1", "status": BrowserSessionStatus.PAUSED_LEGAL_QUESTION.value}
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED), browser_session=session)
    assert cta.label == "REVIEW & CONFIRM"


def test_browser_session_generic_pause_maps_to_answer_and_continue():
    session = {"session_id": "s1", "status": BrowserSessionStatus.PAUSED_UNKNOWN_FIELD.value}
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.APPROVED), browser_session=session)
    assert cta.label == "ANSWER & CONTINUE"


def test_needs_user_action_with_captcha_policy_reason():
    execution = _exec(ExecutionStatus.NEEDS_USER_ACTION, policy_reasons='["CAPTCHA_PRESENT"]')
    cta = compute_apply_cta(1, None, execution=execution)
    assert cta.label == "COMPLETE CAPTCHA"


def test_needs_user_action_with_auth_policy_reason():
    execution = _exec(ExecutionStatus.NEEDS_USER_ACTION, policy_reasons='["AUTH_REQUIRED"]')
    cta = compute_apply_cta(1, None, execution=execution)
    assert cta.label == "SIGN IN & CONTINUE"


def test_needs_user_action_with_legal_policy_reason():
    execution = _exec(ExecutionStatus.NEEDS_USER_ACTION, policy_reasons='["UNKNOWN_LEGAL_QUESTION"]')
    cta = compute_apply_cta(1, None, execution=execution)
    assert cta.label == "REVIEW & CONFIRM"


def test_needs_user_action_with_no_specific_policy_reason_is_generic():
    execution = _exec(ExecutionStatus.NEEDS_USER_ACTION, policy_reasons="[]")
    cta = compute_apply_cta(1, None, execution=execution)
    assert cta.label == "ANSWER & CONTINUE"


def test_submitting_and_submitted_show_applying_in_progress():
    for status in (ExecutionStatus.SUBMITTING, ExecutionStatus.SUBMITTED):
        cta = compute_apply_cta(1, None, execution=_exec(status))
        assert cta.style == STYLE_PROGRESS
        assert cta.label == "APPLYING..."


def test_submission_status_unknown_shows_check_application_status():
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.SUBMISSION_STATUS_UNKNOWN))
    assert cta.label == "CHECK APPLICATION STATUS"


def test_confirmed_and_applied_show_applied_checkmark():
    for status in (ExecutionStatus.SUBMISSION_CONFIRMED, ExecutionStatus.APPLIED):
        cta = compute_apply_cta(1, None, execution=_exec(status))
        assert cta.style == STYLE_SUCCESS
        assert cta.label == "APPLIED ✓"


def test_failed_terminal_statuses_have_no_cta_but_a_specific_reason():
    cta = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.PERMANENT_SUBMISSION_FAILURE))
    assert cta.style == STYLE_NONE
    assert "permanently" in cta.reason.lower()

    cta2 = compute_apply_cta(1, None, execution=_exec(ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED))
    assert cta2.style == STYLE_NONE
    assert "duplicate" in cta2.reason.lower()


def test_no_raw_enum_name_ever_appears_verbatim_in_a_label():
    """Never expose a raw internal enum name to the user (build brief
    section 1) -- spot-check the statuses most likely to leak their own
    SCREAMING_SNAKE_CASE name into the label unchanged."""
    checks = [
        _exec(ExecutionStatus.SUBMISSION_READY),
        _exec(ExecutionStatus.FORM_DISCOVERED),
        _exec(ExecutionStatus.SUBMISSION_STATUS_UNKNOWN),
        _exec(ExecutionStatus.APPROVED),
    ]
    for execution in checks:
        cta = compute_apply_cta(1, None, execution=execution)
        assert execution["status"] not in cta.label
