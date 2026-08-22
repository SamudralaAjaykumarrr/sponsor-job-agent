"""CLAUDE.md Phase 13 section 6: deterministic title normalization. Never
touches network/browser/DB."""

from app.applications.title_normalization import normalize_title, titles_equivalent


def test_senior_prefix_and_suffix_are_equivalent():
    assert titles_equivalent("Senior Software Engineer", "Software Engineer, Senior") is True


def test_case_and_punctuation_insensitive():
    assert titles_equivalent("SOFTWARE ENGINEER", "software engineer") is True


def test_plain_and_leveled_title_not_equivalent():
    """CLAUDE.md section 6: never equate materially different positions."""
    assert titles_equivalent("Software Engineer", "Software Engineer II") is False


def test_roman_and_arabic_level_are_equivalent():
    assert titles_equivalent("Software Engineer II", "Software Engineer 2") is False or \
        titles_equivalent("Software Engineer II", "Software Engineer II") is True
    assert titles_equivalent("Software Engineer II", "Software Engineer 2") is True


def test_backend_qualifier_is_a_different_role():
    assert titles_equivalent("Backend Software Engineer", "Software Engineer") is False


def test_senior_vs_plain_not_equivalent():
    assert titles_equivalent("Senior Software Engineer", "Software Engineer") is False


def test_empty_title_never_equivalent():
    assert titles_equivalent("", "Software Engineer") is False
    assert titles_equivalent("", "") is False


def test_normalize_title_sorts_tokens_for_order_independence():
    a = normalize_title("Platform Engineer")
    b = normalize_title("Engineer Platform")
    assert a.base_role == b.base_role


def test_normalize_title_preserves_original_raw():
    n = normalize_title("Senior Software Engineer")
    assert n.raw == "Senior Software Engineer"
    assert "senior" in n.seniority_markers
