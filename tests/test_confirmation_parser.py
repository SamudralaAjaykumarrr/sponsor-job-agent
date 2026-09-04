"""Real Provider Execution V1: the shared, pure confirmation parser.

These tests exercise the EXACT phrase tables the live browser runtime uses
(`app.applications.browser_runtime` imports them from
`app.applications.confirmation_parser`, never keeping its own copy) against
the same local Greenhouse/Lever confirmation fixtures the browser E2E tests
open -- so the parser is proven without needing Chromium, and any future
divergence between the two would fail here first.
"""

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from app.applications.confirmation_evidence import (
    ConfirmationEvidenceStrength,
    classify_confirmation_evidence,
)
from app.applications.confirmation_parser import (
    DUPLICATE_APPLICATION_PHRASES,
    SUCCESS_PHRASES,
    extract_confirmation_id,
    find_duplicate_application_phrase,
    find_success_phrase,
    parse_confirmation_text,
)


def _fixture_text(file_url: str) -> str:
    """Reads a `file://` fixture's HTML and reduces it to visible-ish text --
    close enough for phrase matching, which is all this parser does. The
    browser E2E suite exercises the same fixtures through real Chromium."""
    html = Path(unquote(urlparse(file_url).path)).read_text()
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- table invariants ---------------------------------------------------------

def test_success_and_duplicate_tables_are_disjoint():
    assert set(SUCCESS_PHRASES).isdisjoint(set(DUPLICATE_APPLICATION_PHRASES))


def test_every_phrase_is_a_specific_completed_action_phrase():
    for phrase in SUCCESS_PHRASES + DUPLICATE_APPLICATION_PHRASES:
        assert phrase == phrase.lower().strip()
        assert len(phrase) >= 8
    # A bare noun must never be a success phrase on its own.
    assert "confirmation" not in SUCCESS_PHRASES
    assert "success" not in SUCCESS_PHRASES


def test_greenhouse_default_template_confirmation_phrase_is_recognized():
    """Real bug caught live (2026-08-31, job 200/Robinhood's first real
    Greenhouse canary submission): Greenhouse's own default confirmation
    template phrases the BODY text as "Thank you for your interest in
    joining our world-class team at Robinhood!" -- none of the pre-existing
    SUCCESS_PHRASES matched it (the page's separate <title> element does say
    "Thank you for applying", but only <body> inner text is ever scanned).
    The added phrase is deliberately company-name-free so it generalizes to
    any Greenhouse-hosted employer using this same default template, not
    just Robinhood."""
    body_text = (
        "Thank you for your interest in joining our world-class team at Robinhood! "
        "What happens now? We will review your application and contact you if there "
        "is a good match. If you are not contacted, be assured that your resume will "
        "remain in our database for future openings. Sincerely, The Robinhood "
        "Recruiting Team."
    )
    parsed = parse_confirmation_text(body_text)
    assert parsed.phrase_matched is True
    assert parsed.matched_phrase == "thank you for your interest in joining"
    assert parsed.already_applied is False

    grade = classify_confirmation_evidence(
        phrase_matched=parsed.phrase_matched, confirmation_id=parsed.confirmation_id,
        current_url="https://job-boards.greenhouse.io/robinhood/jobs/7263592/confirmation?gh_src=gh_src%3D",
    )
    assert grade.confirms()


def test_browser_runtime_uses_this_module_rather_than_its_own_tables():
    """The single-source rule: browser_runtime must not have reintroduced a
    private phrase table."""
    source = Path("app/applications/browser_runtime.py").read_text()
    assert "_SUCCESS_PHRASES = (" not in source
    assert "_DUPLICATE_APPLICATION_PHRASES = (" not in source
    assert "from app.applications.confirmation_parser import parse_confirmation_text" in source


# --- provider fixtures --------------------------------------------------------

def test_greenhouse_confirmation_fixture_parses_as_strong_evidence(tmp_path):
    from tests.browser_fixtures import greenhouse_like_confirmation_page

    parsed = parse_confirmation_text(_fixture_text(greenhouse_like_confirmation_page(tmp_path)))
    assert parsed.phrase_matched is True
    assert parsed.already_applied is False
    assert parsed.confirmation_id == "GH-2026-88134"
    assert parsed.text_fingerprint
    grade = classify_confirmation_evidence(
        phrase_matched=parsed.phrase_matched, confirmation_id=parsed.confirmation_id,
        current_url="file:///tmp/greenhouse_confirmation.html",
    )
    assert grade.strength == ConfirmationEvidenceStrength.STRONG
    assert grade.confirms() is True


def test_lever_confirmation_fixture_parses_as_moderate_evidence(tmp_path):
    """No confirmation id and a plain URL -- a trusted phrase alone is
    MODERATE, which still confirms, but is honestly the weaker grade."""
    from tests.browser_fixtures import lever_like_confirmation_page

    parsed = parse_confirmation_text(_fixture_text(lever_like_confirmation_page(tmp_path)))
    assert parsed.phrase_matched is True
    assert parsed.confirmation_id == ""
    grade = classify_confirmation_evidence(
        phrase_matched=parsed.phrase_matched, confirmation_id=parsed.confirmation_id,
        current_url="file:///tmp/lever_done.html",
    )
    assert grade.strength == ConfirmationEvidenceStrength.MODERATE
    assert grade.confirms() is True


@pytest.mark.parametrize("fixture_name", ["greenhouse_like_application_page", "lever_like_application_page"])
def test_an_ordinary_application_form_never_parses_as_confirmed(tmp_path, fixture_name):
    import tests.browser_fixtures as fixtures

    parsed = parse_confirmation_text(_fixture_text(getattr(fixtures, fixture_name)(tmp_path)))
    assert parsed.phrase_matched is False
    assert parsed.already_applied is False


def test_already_applied_fixture_is_never_a_fresh_confirmation(tmp_path):
    from tests.browser_fixtures import already_applied_page

    parsed = parse_confirmation_text(_fixture_text(already_applied_page(tmp_path)))
    assert parsed.already_applied is True
    assert parsed.phrase_matched is False
    assert parsed.matched_duplicate_phrase


def test_false_confirmation_mention_never_confirms(tmp_path):
    """"Submit your application to receive confirmation by email" mentions
    the word but describes nothing completed."""
    from tests.browser_fixtures import false_confirmation_mention_page

    parsed = parse_confirmation_text(_fixture_text(false_confirmation_mention_page(tmp_path)))
    assert parsed.phrase_matched is False
    grade = classify_confirmation_evidence(phrase_matched=False, confirmation_id=parsed.confirmation_id,
                                            current_url="file:///tmp/x.html")
    assert grade.confirms() is False


# --- ordering / precedence ----------------------------------------------------

def test_duplicate_evidence_wins_over_a_co_present_success_phrase():
    """A real "you already applied -- your application was received on ..."
    page contains BOTH. It must never be folded into a fresh confirmation."""
    text = "You have already applied to this position. Your application has been submitted on 1 Jan."
    parsed = parse_confirmation_text(text)
    assert parsed.already_applied is True
    assert parsed.phrase_matched is False


def test_confirmation_id_is_extracted_even_without_a_phrase_match():
    """A lone id is WEAK evidence the grader must still see -- and WEAK
    never confirms."""
    parsed = parse_confirmation_text("Reference Number: ZZ-9911 for your records.")
    assert parsed.phrase_matched is False
    assert parsed.confirmation_id == "ZZ-9911"
    grade = classify_confirmation_evidence(phrase_matched=False, confirmation_id=parsed.confirmation_id)
    assert grade.strength == ConfirmationEvidenceStrength.WEAK
    assert grade.confirms() is False


def test_helpers_report_which_phrase_matched():
    assert find_success_phrase("Thank you for applying to Acme!") == "thank you for applying"
    assert find_duplicate_application_phrase("You already applied.") == "you already applied"
    assert find_success_phrase("nothing here") == ""
    assert extract_confirmation_id("no id here") == ""


def test_empty_text_is_never_confirmation():
    parsed = parse_confirmation_text("")
    assert parsed.phrase_matched is False
    assert parsed.already_applied is False


def test_confirmation_id_never_captures_a_bare_english_word():
    """Regression: "Application received" / "Application submitted" used to
    yield "received"/"submitted" as a confirmation id, which would have been
    stored durably AND wrongly corroborated the evidence grade up to
    STRONG."""
    for text in ("Application received.", "Application submitted successfully.",
                 "Your application status is pending."):
        assert extract_confirmation_id(text) == "", text


def test_confirmation_id_is_found_past_an_earlier_false_positive():
    text = "Application received. Reference Number: XY-2211 for your records."
    assert extract_confirmation_id(text) == "XY-2211"


def test_real_id_shapes_are_still_extracted():
    for text, expected in (
        ("Confirmation Number: ABC-1234-XYZ", "ABC-1234-XYZ"),
        ("Confirmation Number: GH-2026-88134", "GH-2026-88134"),
        ("application id: R-12345", "R-12345"),
    ):
        assert extract_confirmation_id(text) == expected


# --- Multi-signal confirmation contract (2026-09-04, job 454/Anthropic's
# real canary returned UNRECOGNIZED_OUTCOME -- a body-text-only phrase list
# proved too brittle against real-world wording variation) -----------------

def test_new_success_phrases_are_recognized():
    """Broadened phrase table -- real-world ATS confirmation wording this
    project had not yet curated."""
    for text in (
        "You're all set! We'll be in touch.",
        "Your application is complete.",
        "We've got your application and will review it shortly.",
        "Application successfully received by our recruiting team.",
        "Your submission was successful.",
    ):
        assert find_success_phrase(text) != "", text


def test_success_wording_not_in_body_but_present_in_heading_still_confirms():
    """A page whose heading says the application succeeded, in wording this
    project's phrase table doesn't happen to cover in the BODY text, is
    still recognized -- via the separate, independent heading check."""
    heading = "You're All Set!"
    body = "We appreciate your interest and will follow up if there's a fit."
    parsed = parse_confirmation_text(body, heading_text=heading)
    assert parsed.phrase_matched is False
    assert parsed.heading_phrase_matched is True
    assert parsed.matched_heading_phrase == "you're all set"
    grade = classify_confirmation_evidence(
        phrase_matched=parsed.phrase_matched, heading_phrase_matched=parsed.heading_phrase_matched,
        confirmation_id=parsed.confirmation_id, current_url="https://job-boards.greenhouse.io/acme/jobs/1",
    )
    assert grade.confirms() is True
    assert grade.strength == ConfirmationEvidenceStrength.MODERATE


def test_duplicate_heading_short_circuits_even_without_a_duplicate_body_phrase():
    parsed = parse_confirmation_text(
        "We appreciate your continued interest in our company.",
        heading_text="You Have Already Applied",
    )
    assert parsed.already_applied is True
    assert parsed.phrase_matched is False


def test_confirmation_shaped_url_with_misleading_neutral_content_never_auto_confirms():
    """A URL alone that merely LOOKS like a confirmation route (contains
    "confirm"/"thank"/etc) must never be trusted when the actual page
    content is neutral/uninformative -- exactly the "confirmation URL with
    misleading page content" case."""
    body = "Please wait while we process your request."
    parsed = parse_confirmation_text(body, heading_text="")
    assert parsed.phrase_matched is False
    assert parsed.heading_phrase_matched is False
    grade = classify_confirmation_evidence(
        phrase_matched=parsed.phrase_matched, heading_phrase_matched=parsed.heading_phrase_matched,
        confirmation_id=parsed.confirmation_id,
        current_url="https://job-boards.greenhouse.io/acme/jobs/1/confirmation",
    )
    assert grade.confirms() is False
    assert grade.strength == ConfirmationEvidenceStrength.WEAK
