"""CLAUDE.md Phase 13 sections 49-51: confirmation evidence STRENGTH grading.
Pure, dependency-free -- never touches network/browser/DB."""

from app.applications.confirmation_evidence import (
    ConfirmationEvidenceStrength,
    classify_confirmation_evidence,
    url_looks_like_confirmation,
)


def test_phrase_and_id_is_strong():
    grade = classify_confirmation_evidence(phrase_matched=True, confirmation_id="ABC-123",
                                            current_url="https://x/thank-you")
    assert grade.strength == ConfirmationEvidenceStrength.STRONG
    assert grade.confirms() is True


def test_phrase_alone_is_moderate_and_confirms():
    grade = classify_confirmation_evidence(phrase_matched=True, confirmation_id="", current_url="https://x/apply")
    assert grade.strength == ConfirmationEvidenceStrength.MODERATE
    assert grade.confirms() is True


def test_id_or_url_alone_without_phrase_is_weak_and_never_confirms():
    grade = classify_confirmation_evidence(phrase_matched=False, confirmation_id="ABC-123", current_url="")
    assert grade.strength == ConfirmationEvidenceStrength.WEAK
    assert grade.confirms() is False


def test_confirmation_shaped_url_alone_is_weak():
    grade = classify_confirmation_evidence(phrase_matched=False, confirmation_id="", current_url="https://x/thank-you")
    assert grade.strength == ConfirmationEvidenceStrength.WEAK
    assert grade.confirms() is False


def test_nothing_observed_is_none():
    grade = classify_confirmation_evidence(phrase_matched=False, confirmation_id="", current_url="https://x/apply")
    assert grade.strength == ConfirmationEvidenceStrength.NONE
    assert grade.confirms() is False


# --- CLAUDE.md Phase 13 section 50: false-positive rejection ---------------
# These strings must never, by themselves, be treated as `phrase_matched` --
# that gate lives in app.applications.browser_runtime._SUCCESS_PHRASES,
# already curated to specific completed-action phrasing. This module's
# grading only ever runs on the OUTPUT of that phrase match, so a caller
# passing phrase_matched=False for each of these (as the real phrase table
# does) must never confirm.
def test_generic_success_stories_never_confirms():
    grade = classify_confirmation_evidence(phrase_matched=False, confirmation_id="",
                                            current_url="https://x/success-stories")
    assert grade.confirms() is False


def test_pre_submit_wording_never_confirms():
    grade = classify_confirmation_evidence(phrase_matched=False, confirmation_id="", current_url="https://x/apply")
    assert grade.confirms() is False


def test_url_looks_like_confirmation_hints():
    assert url_looks_like_confirmation("https://x/thank-you") is True
    assert url_looks_like_confirmation("https://x/confirmation") is True
    assert url_looks_like_confirmation("https://x/apply") is False
    assert url_looks_like_confirmation("") is False
