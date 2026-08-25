"""CLAUDE.md Phase 11 section 24: additional 'decline to self-identify'
phrase variants, added conservatively -- verifies both the new matches and
that legitimate real demographic answer choices never overmatch."""

from app.applications.browser_runtime import _decline_option
from app.applications.schema import DECLINE_TO_SELF_IDENTIFY_PHRASES


def _normalize(c: str) -> str:
    return c.lower().replace("'", "")


def test_new_variants_match():
    for phrase in ("Prefer not to disclose", "Rather not say", "Rather not disclose",
                    "Do not wish to disclose", "Choose not to answer", "Choose not to disclose"):
        assert any(p in _normalize(phrase) for p in DECLINE_TO_SELF_IDENTIFY_PHRASES), phrase


def test_decline_option_selects_new_variant_from_choice_list():
    choices = ["Male", "Female", "Prefer not to disclose"]
    assert _decline_option(choices) == "Prefer not to disclose"


def test_legitimate_answers_never_overmatch():
    legitimate = [
        "Male", "Female", "Non-binary", "Hispanic or Latino", "White (Not Hispanic or Latino)",
        "Asian", "Black or African American", "Veteran", "Not a veteran", "Yes", "No",
    ]
    for choice in legitimate:
        matched = any(p in _normalize(choice) for p in DECLINE_TO_SELF_IDENTIFY_PHRASES)
        assert not matched, f"'{choice}' incorrectly matched a decline-to-self-identify phrase"


def test_uncontracted_do_not_want_form_matches():
    """Real Provider Execution V1 regression: "I do not want to answer" is
    the exact choice string on the real Greenhouse EEOC payload this project
    captured live, and it previously matched NOTHING -- only the contracted
    "i dont want to answer" and the different-verb "i do not wish to answer"
    were listed."""
    assert any(p in _normalize("I do not want to answer") for p in DECLINE_TO_SELF_IDENTIFY_PHRASES)
    choices = ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"]
    assert _decline_option(choices) == "I do not want to answer"
