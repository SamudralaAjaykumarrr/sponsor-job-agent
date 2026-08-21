from app.discovery.dedup import fingerprint, normalize_str
from app.matching.compensation import evaluate_compensation, extract_salary_from_text
from app.matching.employment_type import is_full_time
from app.matching.geography import is_us_location
from app.matching.seniority import evaluate_seniority, extract_min_years_required


# --- seniority ---------------------------------------------------------------

def test_seniority_passes_for_compatible_title_and_years():
    ok, reason, years = evaluate_seniority("Backend Software Engineer", "2-4 years of experience required.")
    assert ok
    assert years == 2


def test_seniority_hard_skips_on_7_plus_years():
    ok, reason, years = evaluate_seniority("Software Engineer", "7+ years of experience required.")
    assert not ok
    assert years == 7


def test_seniority_hard_skips_senior_title_without_compatible_years():
    ok, reason, years = evaluate_seniority("Principal Engineer", "We're looking for a great engineer.")
    assert not ok


def test_seniority_allows_senior_title_with_compatible_years_evidence():
    ok, reason, years = evaluate_seniority("Staff Engineer", "3+ years of experience welcome to apply.")
    assert ok


def test_extract_min_years_required_none_when_absent():
    assert extract_min_years_required("We build great software.") is None


# --- compensation --------------------------------------------------------------

def test_compensation_passes_when_unpublished():
    ok, reason = evaluate_compensation(None, None)
    assert ok


def test_compensation_rejects_low_published_max():
    ok, reason = evaluate_compensation(50000, 60000)
    assert not ok
    assert "60,000" in reason or "$60,000" in reason


def test_compensation_passes_when_max_meets_threshold():
    ok, _ = evaluate_compensation(70000, 90000)
    assert ok


def test_extract_salary_from_text():
    lo, hi = extract_salary_from_text("Compensation: $90,000 - $120,000 per year")
    assert lo == 90000
    assert hi == 120000


def test_extract_salary_from_text_k_suffix():
    lo, hi = extract_salary_from_text("Pay range $90k-$120k")
    assert lo == 90000
    assert hi == 120000


# --- employment type -----------------------------------------------------------

def test_full_time_default_passes():
    assert is_full_time("", "We are hiring a backend engineer.")


def test_part_time_rejected():
    assert not is_full_time("Part-time", "")


def test_internship_rejected():
    assert not is_full_time("", "This is an internship position for students.")


# --- geography -------------------------------------------------------------------

def test_us_location_state_abbrev():
    assert is_us_location("Austin, TX")


def test_us_location_remote_us():
    assert is_us_location("Remote (US)")


def test_non_us_location_rejected():
    assert not is_us_location("London, United Kingdom")


def test_ambiguous_location_allowed():
    assert is_us_location("Remote")
    assert is_us_location("")


# --- dedup fingerprint -----------------------------------------------------------

def test_fingerprint_is_stable_for_same_normalized_inputs():
    f1 = fingerprint("Acme Corp", "Backend Engineer", "Remote, US")
    f2 = fingerprint("  ACME  corp ", "backend engineer", "remote, us")
    assert f1 == f2


def test_fingerprint_differs_for_different_role():
    f1 = fingerprint("Acme Corp", "Backend Engineer", "Remote")
    f2 = fingerprint("Acme Corp", "Frontend Engineer", "Remote")
    assert f1 != f2


def test_normalize_str_strips_punctuation_and_case():
    assert normalize_str("Acme, Corp.") == "acme corp"
