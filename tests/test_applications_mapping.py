"""CLAUDE.md Phase 8 section 14: deterministic field-mapping engine, and the
"never fuzzy-match legal/demographic fields" rule."""

from app.applications.mapping import match_field, normalize_label
from app.applications.models import FieldConfidence


def test_exact_alias_match_first_name():
    field_id, confidence = match_field("First Name")
    assert field_id == "first_name"
    assert confidence == FieldConfidence.EXACT


def test_exact_alias_match_legal_first_name_variant():
    field_id, confidence = match_field("Legal First Name")
    assert field_id == "first_name"
    assert confidence == FieldConfidence.EXACT


def test_sponsorship_question_alias_variants():
    for label in [
        "Will you now or in the future require sponsorship?",
        "Do you now or in future need employment sponsorship?",
    ]:
        field_id, confidence = match_field(label)
        assert field_id == "future_sponsorship_required"
        assert confidence == FieldConfidence.EXACT


def test_name_attribute_fallback_high_confidence():
    field_id, confidence = match_field("Q123", name="email")
    assert field_id == "email"
    assert confidence == FieldConfidence.HIGH


def test_medium_confidence_token_overlap():
    field_id, confidence = match_field("Your Current Employer Name")
    assert field_id == "current_employer"
    assert confidence == FieldConfidence.MEDIUM


def test_no_match_returns_none_low():
    field_id, confidence = match_field("Favorite programming language anecdote")
    assert field_id is None
    assert confidence == FieldConfidence.LOW


def test_legal_field_never_fuzzy_matched():
    # A label that shares tokens with a legal alias but isn't an exact phrase
    # must NOT resolve via the token-overlap fallback -- legal fields are
    # EXACT-alias-only.
    field_id, confidence = match_field("Security related notes about your background")
    assert field_id != "security_clearance"


def test_legal_field_exact_alias_still_matches():
    field_id, confidence = match_field("Do you hold a security clearance?")
    assert field_id == "security_clearance"
    assert confidence == FieldConfidence.EXACT


def test_demographic_field_never_fuzzy_matched():
    field_id, confidence = match_field("What is your favorite gender-neutral color scheme?")
    assert field_id != "gender"


def test_normalize_label_strips_punctuation():
    assert normalize_label("What's your E-Mail?") == "what s your e mail"
