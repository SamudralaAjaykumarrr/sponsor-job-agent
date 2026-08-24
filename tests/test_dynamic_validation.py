"""Workday/SmartRecruiters/Workable browser-assist hardening (2026-08-22):
unit tests for the pure, dependency-free classifier in
app.applications.dynamic_validation -- no browser/DB required."""

from app.applications.dynamic_validation import (
    AdvanceAttempt,
    AdvanceOutcome,
    classify_advance_attempt,
    has_validation_error_text,
)


def test_route_change_is_always_advanced_regardless_of_other_signals():
    attempt = AdvanceAttempt(route_changed=True, fields_changed=False,
                              validation_error_elements=3, body_text="this field is required")
    assert classify_advance_attempt(attempt) == AdvanceOutcome.ADVANCED


def test_fields_change_is_always_advanced_regardless_of_other_signals():
    attempt = AdvanceAttempt(route_changed=False, fields_changed=True,
                              validation_error_elements=1, body_text="please fill this field")
    assert classify_advance_attempt(attempt) == AdvanceOutcome.ADVANCED


def test_no_change_with_validation_error_element_is_blocked():
    attempt = AdvanceAttempt(route_changed=False, fields_changed=False, validation_error_elements=1, body_text="")
    assert classify_advance_attempt(attempt) == AdvanceOutcome.VALIDATION_BLOCKED


def test_no_change_with_validation_error_phrase_is_blocked():
    attempt = AdvanceAttempt(route_changed=False, fields_changed=False, validation_error_elements=0,
                              body_text="School is required")
    assert classify_advance_attempt(attempt) == AdvanceOutcome.VALIDATION_BLOCKED


def test_no_change_with_no_evidence_is_unknown_never_guessed():
    attempt = AdvanceAttempt(route_changed=False, fields_changed=False, validation_error_elements=0,
                              body_text="Welcome to the application form.")
    assert classify_advance_attempt(attempt) == AdvanceOutcome.NO_CHANGE_UNKNOWN


def test_has_validation_error_text_matches_known_phrases():
    assert has_validation_error_text("Full Name is required")
    assert has_validation_error_text("Please select an option")
    assert not has_validation_error_text("")


def test_has_validation_error_text_does_not_match_unrelated_required_mentions():
    """A JD-style mention of 'required skills' must never look like a
    validation error -- only the specific completed-phrase table counts,
    matching this project's existing (apply_entry/confirmation_evidence)
    'never a bare substring like a single word' convention."""
    assert not has_validation_error_text("Required skills: Python, SQL, AWS.")
