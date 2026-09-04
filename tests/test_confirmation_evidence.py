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


# --- Multi-signal confirmation contract (2026-09-04, added after job 454/
# Anthropic's real canary returned UNRECOGNIZED_OUTCOME) --------------------

def test_heading_phrase_alone_is_moderate_and_confirms():
    """A heading match behaves like a body phrase match when it's the only
    signal present."""
    grade = classify_confirmation_evidence(
        phrase_matched=False, heading_phrase_matched=True, confirmation_id="", current_url="https://x/apply",
    )
    assert grade.strength == ConfirmationEvidenceStrength.MODERATE
    assert grade.confirms() is True


def test_phrase_matched_in_both_body_and_heading_is_strong_without_id_or_url():
    """Two independent locations agreeing is corroboration in its own right,
    equal in weight to an id/URL match -- no confirmation id or
    confirmation-shaped URL needed."""
    grade = classify_confirmation_evidence(
        phrase_matched=True, heading_phrase_matched=True, confirmation_id="", current_url="https://x/apply",
    )
    assert grade.strength == ConfirmationEvidenceStrength.STRONG
    assert grade.confirms() is True


def test_structural_disappearance_alone_never_confirms():
    """Submit control and form fields both genuinely observed gone, but with
    NO phrase match anywhere -- WEAK only, never sufficient alone (a
    validation error or an unrelated page change could also cause this)."""
    grade = classify_confirmation_evidence(
        phrase_matched=False, confirmation_id="", current_url="https://x/apply",
        submit_control_disappeared=True, form_fields_disappeared=True,
    )
    assert grade.strength == ConfirmationEvidenceStrength.WEAK
    assert grade.confirms() is False


def test_structural_disappearance_upgrades_moderate_phrase_to_strong():
    grade = classify_confirmation_evidence(
        phrase_matched=True, confirmation_id="", current_url="https://x/apply",
        submit_control_disappeared=True, form_fields_disappeared=True,
    )
    assert grade.strength == ConfirmationEvidenceStrength.STRONG
    assert grade.confirms() is True


def test_only_submit_control_disappeared_is_not_structural_corroboration():
    """Requiring BOTH signals together is deliberately conservative -- one
    alone (e.g. the button vanished but the rest of the form is still there)
    must never count as structural corroboration."""
    grade = classify_confirmation_evidence(
        phrase_matched=True, confirmation_id="", current_url="https://x/apply",
        submit_control_disappeared=True, form_fields_disappeared=False,
    )
    assert grade.strength == ConfirmationEvidenceStrength.MODERATE


def test_structural_signals_default_none_never_fabricates_corroboration():
    """Optional[bool]=None (not observed) must contribute nothing -- this is
    the default every existing caller (browser_runtime, browser_assist) gets
    without passing the new parameters at all."""
    grade = classify_confirmation_evidence(phrase_matched=True, confirmation_id="", current_url="https://x/apply")
    assert grade.strength == ConfirmationEvidenceStrength.MODERATE
