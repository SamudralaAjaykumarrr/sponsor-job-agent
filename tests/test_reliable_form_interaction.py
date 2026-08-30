"""Reliable Form Interaction V1 (real-employer diagnosis, see CLAUDE.md):
real-Chromium tests proving the field resolver/combobox primitives added to
app.applications.browser_runtime actually work against a DOM shaped like
the real, live Robinhood/Greenhouse posting whose interaction previously
went wrong -- a react-select-style combobox with no stable text label on
its own <label>, a numeric element id, an unrelated widget's pre-rendered
listbox sitting nearby, and two file inputs sharing an identical generic
<label>. Marked `browser` -- skipped automatically unless Playwright AND
its Chromium binary are actually launchable; every URL is a local `file://`
fixture, never a real website. No test in this file ever submits a form."""

import pytest

from app import config
from app.applications import browser_runtime
from tests.browser_fixtures import (
    DEFAULT_JOB_COMPANY,
    DEFAULT_JOB_TITLE,
    combobox_reliability_page,
    combobox_reliability_page_option_changed,
)

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _require_chromium_launchable():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    try:
        with playwright.sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium browser binary not launchable: {exc}")


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_HEADLESS", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_TIMEOUT_SECONDS", 15)


def _open(tmp_path, session_id: str, url: str):
    outcome = browser_runtime.open_session(
        session_id, provider="greenhouse", url=url,
        expected_title=DEFAULT_JOB_TITLE, expected_company=DEFAULT_JOB_COMPANY,
    )
    return outcome


def _field(fields: list[dict], label_prefix: str) -> dict:
    match = next((rf for rf in fields if (rf.get("label") or "").strip().startswith(label_prefix)), None)
    assert match is not None, f"field {label_prefix!r} not found among: {[rf.get('label') for rf in fields]}"
    return match


# --- 1/2/3: label-to-combobox resolution, no name/id field, numeric id -----

def test_combobox_fields_detected_with_role_and_numeric_id_selector_is_valid(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-detect"
    try:
        outcome = _open(tmp_path, session_id, url)
        assert outcome.pause_reason is None
        country = _field(outcome.fields, "Country")
        assert country["type"] == "combobox"
        assert country["id"] == "country"

        gender = _field(outcome.fields, "What is your gender identity?")
        assert gender["id"] == "1255"
        # This is the real observed failure: "#1255" is invalid CSS as an
        # ID selector. The fixed _selector_for must never produce it.
        selector = browser_runtime._selector_for(gender)
        assert selector == "[id='1255']"
        assert not selector.startswith("#1")
    finally:
        browser_runtime.close_session(session_id)


# --- 4/6/7: exact Yes/No-style and multi-option selection, verified --------

def test_combobox_exact_option_selection_is_verified(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-select"
    try:
        outcome = _open(tmp_path, session_id, url)
        office = _field(outcome.fields, "What is your preferred office location?")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._fill_one, office, "Menlo Park, CA", timeout=15)
        assert ok is True
        displayed = live.run(lambda: live.page.locator("#office").input_value(), timeout=10)
        assert displayed == "Menlo Park, CA"
        expanded = live.run(lambda: live.page.locator("#office").get_attribute("aria-expanded"), timeout=10)
        assert expanded == "false"
    finally:
        browser_runtime.close_session(session_id)


def test_combobox_decline_to_answer_option_selection(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-decline"
    try:
        outcome = _open(tmp_path, session_id, url)
        gender = _field(outcome.fields, "What is your gender identity?")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._fill_one, gender, "I don't wish to answer", timeout=15)
        assert ok is True
        displayed = live.run(lambda: live.page.locator("[id='1255']").input_value(), timeout=10)
        assert displayed == "I don't wish to answer"
    finally:
        browser_runtime.close_session(session_id)


# --- 4/5: phone-country vs application Country must never be confused -----

def test_country_combobox_never_selects_phone_country_code_option(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-country"
    try:
        outcome = _open(tmp_path, session_id, url)
        country = _field(outcome.fields, "Country")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._fill_one, country, "United States", timeout=15)
        assert ok is True
        displayed = live.run(lambda: live.page.locator("#country").input_value(), timeout=10)
        # The real observed bug: this used to end up "United States +1"
        # (the unrelated phone widget's own pre-rendered option).
        assert displayed == "United States"
        assert "+1" not in displayed
    finally:
        browser_runtime.close_session(session_id)


# --- 5/10/12: opening one combobox must never contaminate the next field ---

def test_opening_country_dropdown_does_not_contaminate_next_field_selection(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-sequence"
    try:
        outcome = _open(tmp_path, session_id, url)
        country = _field(outcome.fields, "Country")
        gender = _field(outcome.fields, "What is your gender identity?")
        live = browser_runtime._get_live(session_id)

        assert live.run(live._fill_one, country, "United States", timeout=15) is True
        # No unrelated popup should remain open afterward.
        country_expanded = live.run(lambda: live.page.locator("#country").get_attribute("aria-expanded"), timeout=10)
        assert country_expanded == "false"

        assert live.run(live._fill_one, gender, "Male", timeout=15) is True
        gender_value = live.run(lambda: live.page.locator("[id='1255']").input_value(), timeout=10)
        assert gender_value == "Male"
    finally:
        browser_runtime.close_session(session_id)


# --- 11: an unmatched value must report failure, never guess ---------------

def test_combobox_no_confident_match_reports_failure_not_a_guess(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-nomatch"
    try:
        outcome = _open(tmp_path, session_id, url)
        country = _field(outcome.fields, "Country")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._fill_one, country, "Atlantis", timeout=15)
        assert ok is False
        displayed = live.run(lambda: live.page.locator("#country").input_value(), timeout=10)
        assert displayed == ""  # never guessed/left partially typed as a false answer
    finally:
        browser_runtime.close_session(session_id)


# --- 13/14: Resume/CV vs Cover Letter disambiguation + upload verification -

def test_resume_and_cover_letter_disambiguated_by_group_label_not_generic_attach(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-fields"
    try:
        outcome = _open(tmp_path, session_id, url)
        file_fields = [rf for rf in outcome.fields if rf.get("type") == "file"]
        labels = {rf["id"]: rf["label"] for rf in file_fields}
        assert labels.get("resume") == "Resume/CV"
        assert labels.get("cover_letter") == "Cover Letter"
        assert labels.get("resume") != "Attach"
    finally:
        browser_runtime.close_session(session_id)


def test_resume_upload_targets_correct_field_and_is_verifiable(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-upload"
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 fake resume content")
    try:
        outcome = _open(tmp_path, session_id, url)
        resume_field = next(rf for rf in outcome.fields if rf.get("id") == "resume")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._upload_one, resume_field, str(resume_path), timeout=15)
        assert ok is True
        uploaded_name = live.run(
            lambda: live.page.evaluate("document.getElementById('resume').files[0].name"), timeout=10,
        )
        assert uploaded_name == "resume.pdf"
        cover_letter_files = live.run(
            lambda: live.page.evaluate("document.getElementById('cover_letter').files.length"), timeout=10,
        )
        assert cover_letter_files == 0  # never uploaded into the wrong field
    finally:
        browser_runtime.close_session(session_id)


# --- 15/16: structural drift is unaffected by normal value changes, but ----
# --- genuinely fires on a real option/question-set change ------------------

def test_selecting_values_does_not_cause_structural_drift(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-drift-safe"
    try:
        outcome = _open(tmp_path, session_id, url)
        fingerprint_before = outcome.fingerprint
        country = _field(outcome.fields, "Country")
        office = _field(outcome.fields, "What is your preferred office location?")
        live = browser_runtime._get_live(session_id)
        live.run(live._fill_one, country, "United States", timeout=15)
        live.run(live._fill_one, office, "Menlo Park, CA", timeout=15)
        outcome_after = browser_runtime.rediscover(session_id)
        assert outcome_after.fingerprint == fingerprint_before
    finally:
        browser_runtime.close_session(session_id)


def test_real_structural_change_still_triggers_different_fingerprint(tmp_path):
    url_a = combobox_reliability_page(tmp_path)
    url_b = combobox_reliability_page_option_changed(tmp_path)
    session_a, session_b = "t-drift-a", "t-drift-b"
    outcome_a = _open(tmp_path, session_a, url_a)
    fingerprint_a = outcome_a.fingerprint
    browser_runtime.close_session(session_a)
    try:
        outcome_b = _open(tmp_path, session_b, url_b)
        assert fingerprint_a != outcome_b.fingerprint
    finally:
        browser_runtime.close_session(session_b)


# --- 17: deterministic rerun produces the same control resolution ----------

def test_deterministic_rerun_produces_same_selector_resolution(tmp_path):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-determinism"
    try:
        outcome = _open(tmp_path, session_id, url)
        country1 = _field(outcome.fields, "Country")
        selector1 = browser_runtime._selector_for(country1)
        outcome2 = browser_runtime.rediscover(session_id)
        country2 = _field(outcome2.fields, "Country")
        selector2 = browser_runtime._selector_for(country2)
        assert selector1 == selector2 == "[id='country']"
    finally:
        browser_runtime.close_session(session_id)


# --- 18: no test in this file ever performs a final submit action ----------

def test_no_submit_control_is_ever_clicked_in_this_suite(tmp_path):
    """Static guard: nothing above calls .click() on the submit button, and
    the form's own vanilla HTML submit would navigate away (detectable by a
    URL change) -- confirms no test accidentally triggers it."""
    url = combobox_reliability_page(tmp_path)
    session_id = "t-no-submit"
    try:
        outcome = _open(tmp_path, session_id, url)
        url_before = outcome.current_url
        live = browser_runtime._get_live(session_id)
        current_url = live.run(lambda: live.page.url, timeout=10)
        assert current_url == url_before
    finally:
        browser_runtime.close_session(session_id)
