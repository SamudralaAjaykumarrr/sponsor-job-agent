"""CLAUDE.md Phase 11 section 61: deterministic, browser-free tests for
app.applications.apply_entry's classification tables. No Playwright import
anywhere in this file -- these must run under plain `pytest`, always."""

from app.applications.apply_entry import (
    ApplyControlClassification,
    EntryDetectionResult,
    EntryStage,
    NavControlKind,
    StepConfidence,
    classify_apply_control,
    classify_nav_control,
    classify_stage,
    detect_entry_result,
    is_confirmation_page_text,
    is_review_page_text,
    parse_step_progress,
)


# --- classify_apply_control ---------------------------------------------------

def test_apply_now_is_navigation_safe():
    assert classify_apply_control("Apply Now") == ApplyControlClassification.NAVIGATION_SAFE


def test_apply_for_this_job_is_navigation_safe():
    assert classify_apply_control("Apply for this Job") == ApplyControlClassification.NAVIGATION_SAFE


def test_start_application_is_navigation_safe():
    assert classify_apply_control("Start Application") == ApplyControlClassification.NAVIGATION_SAFE


def test_continue_application_is_navigation_safe():
    assert classify_apply_control("Continue Application") == ApplyControlClassification.NAVIGATION_SAFE


def test_submit_application_is_final_submit_never_navigation_safe():
    """CLAUDE.md Phase 11 section 5: the exact bug this phase's fix targets
    -- 'Submit Application' must never be treated as a safe apply-entry
    click, and 'Apply Now' (Phase 10's browser_runtime._SUBMIT_BUTTON_PHRASES
    incorrectly included it) must never be treated as a final submit."""
    assert classify_apply_control("Submit Application") == ApplyControlClassification.FINAL_SUBMIT
    assert classify_apply_control("Send Application") == ApplyControlClassification.FINAL_SUBMIT
    assert classify_apply_control("Complete Application") == ApplyControlClassification.FINAL_SUBMIT
    assert classify_apply_control("Apply Now") != ApplyControlClassification.FINAL_SUBMIT


def test_login_trigger_classified_distinctly():
    assert classify_apply_control("Sign In to Apply") == ApplyControlClassification.LOGIN_TRIGGER
    assert classify_apply_control("Create Account") == ApplyControlClassification.LOGIN_TRIGGER


def test_external_redirect_by_host_mismatch():
    result = classify_apply_control(
        "Apply Now", href="https://ads.example-tracker.com/click?x=1", current_host="jobs.smartrecruiters.com",
    )
    assert result == ApplyControlClassification.EXTERNAL_REDIRECT


def test_relative_href_never_treated_as_external_redirect():
    result = classify_apply_control("Apply Now", href="/apply/123", current_host="jobs.smartrecruiters.com")
    assert result == ApplyControlClassification.NAVIGATION_SAFE


def test_same_host_href_not_external():
    result = classify_apply_control(
        "Apply Now", href="https://jobs.smartrecruiters.com/apply/123", current_host="jobs.smartrecruiters.com",
    )
    assert result == ApplyControlClassification.NAVIGATION_SAFE


def test_unrecognized_text_is_unknown_never_guessed():
    assert classify_apply_control("Learn More About Our Culture") == ApplyControlClassification.UNKNOWN
    assert classify_apply_control("") == ApplyControlClassification.UNKNOWN


# --- classify_nav_control -----------------------------------------------------

def test_nav_control_next_vs_submit_vs_back_vs_save():
    assert classify_nav_control("Next") == NavControlKind.NEXT_SAFE
    assert classify_nav_control("Continue") == NavControlKind.NEXT_SAFE
    assert classify_nav_control("Back") == NavControlKind.BACK_SAFE
    assert classify_nav_control("Save and Continue Later") == NavControlKind.SAVE_CONTINUE
    assert classify_nav_control("Submit Application") == NavControlKind.FINAL_SUBMIT
    assert classify_nav_control("Something Unrelated") == NavControlKind.UNKNOWN


# --- detect_entry_result ------------------------------------------------------

def test_entry_result_form_already_visible_wins_over_apply_control():
    result = detect_entry_result(
        has_apply_control=True, apply_control_classification=ApplyControlClassification.NAVIGATION_SAFE,
        has_form_fields=True, login_wall_present=False,
    )
    assert result == EntryDetectionResult.FORM_ALREADY_VISIBLE


def test_entry_result_login_wall_always_wins():
    result = detect_entry_result(
        has_apply_control=True, apply_control_classification=ApplyControlClassification.NAVIGATION_SAFE,
        has_form_fields=True, login_wall_present=True,
    )
    assert result == EntryDetectionResult.LOGIN_REQUIRED


def test_entry_result_navigation_safe_control_is_entry_ready():
    result = detect_entry_result(
        has_apply_control=True, apply_control_classification=ApplyControlClassification.NAVIGATION_SAFE,
        has_form_fields=False, login_wall_present=False,
    )
    assert result == EntryDetectionResult.ENTRY_READY


def test_entry_result_unknown_control_needs_user_action():
    result = detect_entry_result(
        has_apply_control=True, apply_control_classification=ApplyControlClassification.UNKNOWN,
        has_form_fields=False, login_wall_present=False,
    )
    assert result == EntryDetectionResult.USER_ACTION_REQUIRED


def test_entry_result_no_control_no_form_is_unsupported():
    result = detect_entry_result(
        has_apply_control=False, apply_control_classification=None, has_form_fields=False,
        login_wall_present=False,
    )
    assert result == EntryDetectionResult.UNSUPPORTED


def test_entry_result_external_redirect_control():
    result = detect_entry_result(
        has_apply_control=True, apply_control_classification=ApplyControlClassification.EXTERNAL_REDIRECT,
        has_form_fields=False, login_wall_present=False,
    )
    assert result == EntryDetectionResult.REDIRECT_REQUIRED


# --- classify_stage ------------------------------------------------------------

def test_stage_landing_page_when_only_apply_control():
    assert classify_stage(
        has_form_fields=False, has_apply_control=True, is_review_page=False, is_confirmation_page=False,
    ) == EntryStage.LANDING_PAGE


def test_stage_application_form_when_fields_present():
    assert classify_stage(
        has_form_fields=True, has_apply_control=False, is_review_page=False, is_confirmation_page=False,
    ) == EntryStage.APPLICATION_FORM


def test_stage_final_review_wins_over_form_fields():
    assert classify_stage(
        has_form_fields=True, has_apply_control=False, is_review_page=True, is_confirmation_page=False,
    ) == EntryStage.FINAL_REVIEW


def test_stage_confirmation_wins_over_everything():
    assert classify_stage(
        has_form_fields=True, has_apply_control=True, is_review_page=True, is_confirmation_page=True,
    ) == EntryStage.CONFIRMATION


def test_stage_application_entry_when_nothing_detected():
    assert classify_stage(
        has_form_fields=False, has_apply_control=False, is_review_page=False, is_confirmation_page=False,
    ) == EntryStage.APPLICATION_ENTRY


# --- review/confirmation text detection ---------------------------------------

def test_review_page_text_detected():
    assert is_review_page_text("Please review your application before submitting.")
    assert not is_review_page_text("Tell us about your experience.")


def test_confirmation_page_text_detected():
    assert is_confirmation_page_text("Thank you for applying! We've received your application.")
    assert not is_confirmation_page_text("Submit your application to receive confirmation.")


# --- parse_step_progress --------------------------------------------------------

def test_step_of_pattern_is_exact():
    current, total, confidence = parse_step_progress("Step 2 of 4")
    assert (current, total, confidence) == (2, 4, StepConfidence.EXACT)


def test_slash_pattern_is_exact():
    current, total, confidence = parse_step_progress("Progress: 3 / 5 sections complete")
    assert (current, total, confidence) == (3, 5, StepConfidence.EXACT)


def test_bare_step_no_total_is_inferred_never_invents_total():
    current, total, confidence = parse_step_progress("Step 2 -- Education")
    assert current == 2
    assert total is None
    assert confidence == StepConfidence.INFERRED


def test_no_pattern_is_unknown():
    current, total, confidence = parse_step_progress("Tell us about yourself")
    assert (current, total, confidence) == (None, None, StepConfidence.UNKNOWN)


def test_unrelated_date_like_ratio_never_treated_as_step_progress():
    """CLAUDE.md Phase 11 section 19: a real live-Chromium run against
    GitLab's genuine Greenhouse posting caught this exact bug -- an
    unrelated date ("Posted 7/31") elsewhere on the page must never be
    misread as 'step 7 of 31'."""
    current, total, confidence = parse_step_progress("Application deadline: 7/31. Please apply soon.")
    assert (current, total, confidence) == (None, None, StepConfidence.UNKNOWN)


def test_invalid_slash_ratio_falls_back_to_unknown():
    # "5 / 2" (current > total) is not a valid step progress reading --
    # never trust an inverted ratio as EXACT.
    current, total, confidence = parse_step_progress("Score: 5 / 2")
    assert confidence == StepConfidence.UNKNOWN
