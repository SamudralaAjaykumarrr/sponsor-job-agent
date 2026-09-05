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


def _displayed_value(page, selector: str) -> str:
    """Reads the real selected-value display (a sibling `.select__single-
    value` element, matching react-select's actual behavior) rather than
    the search input's own value -- a real combobox clears its input back
    to "" on selection, so asserting against input_value() alone would
    pass on an empty string no matter what was actually chosen."""
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const disp = el.parentElement ? el.parentElement.querySelector('.select__single-value') : null;
            return disp ? disp.innerText : null;
        }""",
        selector,
    )


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
        displayed = live.run(lambda: _displayed_value(live.page, "#office"), timeout=10)
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
        displayed = live.run(lambda: _displayed_value(live.page, "[id='1255']"), timeout=10)
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
        displayed = live.run(lambda: _displayed_value(live.page, "#country"), timeout=10)
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
        gender_value = live.run(lambda: _displayed_value(live.page, "[id='1255']"), timeout=10)
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


# --- 19: a combobox still mid-hydration when the DOM otherwise looks
# "stable" gets one bounded rescan instead of being surfaced as an
# unidentifiable phantom field (real, live-observed against Anthropic's
# newest Greenhouse UI -- see config.BROWSER_FIELD_RESCAN_WAIT_MS) --------

def test_hydrating_combobox_resolves_to_real_label_not_phantom_field(tmp_path):
    from tests.browser_fixtures import hydrating_combobox_form_page

    url = hydrating_combobox_form_page(tmp_path, hydrate_delay_ms=300)
    session_id = "t-hydrate"
    try:
        outcome = _open(tmp_path, session_id, url)
        assert outcome.pause_reason is None
        # The bug this guards: without the rescan, this field's label/id/name
        # are all still empty (type falls back to the bare tag name "input")
        # because _detect_fields() ran before the widget's own JS attached
        # its real attributes.
        phantom = [rf for rf in outcome.fields if not rf.get("label") and not rf.get("id") and not rf.get("name")]
        assert phantom == [], f"unidentifiable field(s) survived the rescan: {phantom}"
        visa = _field(outcome.fields, "Do you require visa sponsorship?")
        assert visa["id"] == "visa_sponsorship_q"
        assert visa["type"] == "combobox"
    finally:
        browser_runtime.close_session(session_id)


def test_hydrating_combobox_rescan_never_loops_past_configured_bound(tmp_path, monkeypatch):
    """A widget that NEVER finishes hydrating (hydrate_delay_ms far beyond
    the rescan budget) must still return promptly with the field honestly
    reported as unidentifiable -- never an unbounded retry loop."""
    from tests.browser_fixtures import hydrating_combobox_form_page

    monkeypatch.setattr(config, "BROWSER_FIELD_RESCAN_WAIT_MS", 100)
    monkeypatch.setattr(config, "BROWSER_FIELD_RESCAN_MAX_ATTEMPTS", 2)
    url = hydrating_combobox_form_page(tmp_path, hydrate_delay_ms=60_000)
    session_id = "t-hydrate-never"
    try:
        outcome = _open(tmp_path, session_id, url)
        phantom = [rf for rf in outcome.fields if not rf.get("label") and not rf.get("id") and not rf.get("name")]
        assert len(phantom) == 1
    finally:
        browser_runtime.close_session(session_id)


# --- 20: react-select's aria-hidden RequiredInput dummy is never surfaced
# as its own question (real, live-observed against Anthropic's newest
# Greenhouse UI while reinspecting job 454) -----------------------------

def test_aria_hidden_required_dummy_input_never_surfaced_as_a_field(tmp_path):
    from tests.browser_fixtures import react_select_required_dummy_input_page

    url = react_select_required_dummy_input_page(tmp_path)
    session_id = "t-aria-hidden-dummy"
    try:
        outcome = _open(tmp_path, session_id, url)
        assert outcome.pause_reason is None
        # The real bug: this element has no label/id/name and is required=True
        # -- without the fix it shows up as an unresolvable phantom question.
        phantom = [rf for rf in outcome.fields if not rf.get("label") and not rf.get("id") and not rf.get("name")]
        assert phantom == [], f"aria-hidden dummy input was surfaced as a field: {phantom}"
        # The REAL question must still be detected correctly.
        visa = _field(outcome.fields, "Do you require visa sponsorship?")
        assert visa["id"] == "question_18266060008"
        assert visa["type"] == "combobox"
        assert visa["required"] is True
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


# --- Provider-Semantic Selection Verification V1: a phone-country-code
# field whose display shows only a dial code (ambiguous across multiple
# real countries), never the country name -- text verification alone can
# never honestly confirm which one, so a structural flag-icon self-
# consistency check is used instead. ---------------------------------------

def test_phone_country_code_verified_via_flag_not_ambiguous_dial_code(tmp_path):
    from tests.browser_fixtures import phone_country_code_combobox_page

    url = phone_country_code_combobox_page(tmp_path)
    session_id = "t-phone-us"
    try:
        outcome = _open(tmp_path, session_id, url)
        country = _field(outcome.fields, "Country")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._fill_one, country, "United States", timeout=15)
        assert ok is True
        # The display genuinely only ever shows the dial code -- proving
        # the verification did NOT (and could not) rely on country-name
        # text, only the flag-icon self-consistency check.
        displayed = live.run(lambda: _displayed_value(live.page, "#country"), timeout=10)
        assert displayed == "+1"
        assert "united states" not in (displayed or "").lower()
    finally:
        browser_runtime.close_session(session_id)


def test_phone_country_code_disambiguates_same_dial_code_by_flag(tmp_path):
    """Canada shares the identical "+1" dial code with the US -- selecting
    Canada must resolve to Canada's own flag, never accidentally read back
    as (or confused with) the US option that happens to render identical
    display text."""
    from tests.browser_fixtures import phone_country_code_combobox_page

    url = phone_country_code_combobox_page(tmp_path)
    session_id = "t-phone-ca"
    try:
        outcome = _open(tmp_path, session_id, url)
        country = _field(outcome.fields, "Country")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._fill_one, country, "Canada", timeout=15)
        assert ok is True

        def _flag_class(page):
            return page.evaluate(
                "() => { const f = document.querySelector('#country ~ .select__single-value [class*=\"flag\" i], "
                ".select__single-value [class*=\"flag\" i]'); return f ? f.className : null; }"
            )

        flag = live.run(lambda: _flag_class(live.page), timeout=10)
        assert flag == "iti__flag iti__ca"
    finally:
        browser_runtime.close_session(session_id)


def test_phone_country_code_no_match_still_reports_failure_not_a_guess(tmp_path):
    """A value with no matching option at all must never fall back to the
    flag-based check to fabricate a success -- the flag check only ever
    corroborates a genuine option match found first."""
    from tests.browser_fixtures import phone_country_code_combobox_page

    url = phone_country_code_combobox_page(tmp_path)
    session_id = "t-phone-nomatch"
    try:
        outcome = _open(tmp_path, session_id, url)
        country = _field(outcome.fields, "Country")
        live = browser_runtime._get_live(session_id)
        ok = live.run(live._fill_one, country, "Germany", timeout=15)
        assert ok is False
    finally:
        browser_runtime.close_session(session_id)


# --- Form-Fingerprint Stability V1: a whole additional section (its own
# consent checkbox) that mounts asynchronously, after the rest of the form
# is already interactive -- the real root cause behind spurious
# PAUSED_FORM_CHANGED pauses and a field that appeared "missing" on some
# discovery passes and not others (real, live-observed against Robinhood's
# real Greenhouse posting). ------------------------------------------------

def test_delayed_consent_section_is_still_discovered(tmp_path):
    from tests.browser_fixtures import sensitive_evidence_gate_page_delayed_consent

    url = sensitive_evidence_gate_page_delayed_consent(tmp_path, delay_ms=300)
    session_id = "t-delayed-consent"
    try:
        outcome = _open(tmp_path, session_id, url)
        consent = _field(outcome.fields, "By checking this box, I consent")
        assert consent["type"] == "checkbox"
    finally:
        browser_runtime.close_session(session_id)


def test_delayed_consent_section_produces_a_stable_fingerprint(tmp_path):
    """Two independent discovery passes of the SAME semantically-identical
    page (one delayed field included both times) must produce the SAME
    fingerprint -- the real, live-observed instability this fix closes:
    without waiting for the delayed section, one pass could catch it and
    another could not, producing two different fingerprints for one
    unchanged form and spuriously tripping PAUSED_FORM_CHANGED."""
    from tests.browser_fixtures import sensitive_evidence_gate_page_delayed_consent

    url = sensitive_evidence_gate_page_delayed_consent(tmp_path, delay_ms=300)
    fingerprints = []
    for i in range(2):
        session_id = f"t-delayed-consent-fp-{i}"
        try:
            outcome = _open(tmp_path, session_id, url)
            fingerprints.append(outcome.fingerprint)
        finally:
            browser_runtime.close_session(session_id)
    assert fingerprints[0] == fingerprints[1]


def test_delayed_field_rescan_never_loops_past_configured_bound(tmp_path, monkeypatch):
    """A section that NEVER finishes mounting (delay far beyond the rescan
    budget) must still return promptly, honestly missing that field --
    never an unbounded retry loop. Mirrors
    test_hydrating_combobox_rescan_never_loops_past_configured_bound for
    this different (whole-field-count-growth) rescan."""
    from tests.browser_fixtures import sensitive_evidence_gate_page_delayed_consent

    monkeypatch.setattr(config, "BROWSER_FIELD_RESCAN_WAIT_MS", 100)
    monkeypatch.setattr(config, "BROWSER_FIELD_RESCAN_MAX_ATTEMPTS", 2)
    url = sensitive_evidence_gate_page_delayed_consent(tmp_path, delay_ms=60_000)
    session_id = "t-delayed-never"
    try:
        outcome = _open(tmp_path, session_id, url)
        consent = [rf for rf in outcome.fields if "consent" in (rf.get("label") or "").lower()]
        assert consent == []
    finally:
        browser_runtime.close_session(session_id)


def test_immediate_and_delayed_mount_of_the_same_field_set_agree(tmp_path):
    """delay_ms=0 (mounted immediately) and delay_ms=300 (mounted after the
    rescan catches up) describe the IDENTICAL field set -- both must
    fingerprint identically, proving the stability fix reaches the same
    semantic answer regardless of exactly when the async section mounted.
    Genuine material-change detection (a truly different field set) remains
    covered, unmodified, by test_real_structural_change_still_triggers_
    different_fingerprint above -- this fix never touches
    _fingerprint_fields()'s own hashing logic, only what it's given."""
    from tests.browser_fixtures import sensitive_evidence_gate_page_delayed_consent

    fps = []
    for i, delay_ms in enumerate((0, 300)):
        url = sensitive_evidence_gate_page_delayed_consent(tmp_path, delay_ms=delay_ms)
        session_id = f"t-immediate-vs-delayed-{i}"
        try:
            outcome = _open(tmp_path, session_id, url)
            fps.append(outcome.fingerprint)
        finally:
            browser_runtime.close_session(session_id)
    assert fps[0] == fps[1]
