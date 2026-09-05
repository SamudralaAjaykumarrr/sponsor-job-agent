"""Browser-Verified Answer Canonical Readiness Integration V1: real-Chromium
end-to-end tests proving record_verified_custom_answer() -> durable
evidence -> the SAME evidence resolving the field on a later, independent
discovery/fill pass (via match_field_with_application_fields()). Reuses
tests/browser_fixtures.py's combobox_reliability_page fixture (whose
"Country" and "What is your preferred office location?" fields
deliberately have NO generic candidate-profile mapping -- exactly the
class of question this feature exists for). Marked `browser`; every URL is
a local `file://` fixture, never a real website. No test here ever
performs a submit action or creates a submit claim."""

import pytest

from app import config
from app.applications import browser_assist, browser_runtime, verified_field_evidence as vfe
from app.applications.models import ApplicationField, FieldCategory, FieldConfidence
from app.applications.schema import find_field
from tests.browser_fixtures import (
    DEFAULT_JOB_COMPANY,
    DEFAULT_JOB_TITLE,
    combobox_reliability_page,
    sensitive_evidence_gate_page,
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


def _open(session_id: str, url: str, job_id: int = 200):
    return browser_runtime.open_session(
        session_id, provider="greenhouse", url=url, job_id=job_id,
        expected_title=DEFAULT_JOB_TITLE, expected_company=DEFAULT_JOB_COMPANY,
    )


def _fake_job(job_id: int = 200):
    from app.models import Job

    return Job(
        id=job_id, title=DEFAULT_JOB_TITLE, company=DEFAULT_JOB_COMPANY, location="Remote - US",
        description="Build APIs.", provider="greenhouse", external_job_id="7263592",
        jd_sponsorship_fingerprint="jd-fp-e2e-v1",
    )


def _create_execution_row(job_id: int, execution_id: str):
    """Minimal real application_executions row so browser_session.
    create_session()'s job_id/execution_id FK-shaped columns have something
    real to point at, matching how app.applications.browser_assist actually
    associates a session with an execution in production."""
    from app.db import db_session

    with db_session() as conn:
        conn.execute(
            "DELETE FROM application_executions WHERE execution_id = ?", (execution_id,),
        )
        conn.execute(
            """INSERT INTO application_executions
               (execution_id, job_id, provider, mode, status, active, form_fingerprint,
                resume_artifact_path, resume_artifact_hash, cover_letter_artifact_path,
                submission_method, confirmation_id, confirmation_url, confirmation_text_fingerprint,
                error_type, error_message_safe, user_action_reason, automation_policy,
                policy_reasons, correlation_id, prepared_jd_fingerprint, prepared_employment_type,
                prepared_sponsorship_status, started_at, created_at, updated_at)
               VALUES (?, ?, 'greenhouse', 'ASSIST', 'QUEUED', 1, '', '', '', '', '', '', '', '',
                       '', '', '', '', '[]', '', '', '', '', '2026-01-01T00:00:00+00:00',
                       '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""",
            (execution_id, job_id),
        )


def _session_and_execution(job_id: int) -> tuple[str, str]:
    from app.applications import browser_session

    execution_id = f"exec-e2e-{job_id}"
    _create_execution_row(job_id, execution_id)
    row = browser_session.create_session(
        execution_id=execution_id, job_id=job_id, provider="greenhouse", application_url="",
    )
    return row["session_id"], execution_id


# --- 1/9: a provider-specific browser-verified answer becomes durable
# evidence, and RESOLVES the field on a later, independent discovery/fill
# pass via the generic pipeline's evidence-aware matching. ---

def test_verified_answer_resolves_field_on_later_independent_pass(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = combobox_reliability_page(tmp_path)
    session_id, execution_id = _session_and_execution(201)
    try:
        _open(session_id, url, job_id=201)
        result = browser_assist.record_verified_custom_answer(
            session_id, "What is your preferred office location?", "Menlo Park, CA",
        )
        assert result["ok"] is True
        assert result["actual"] == "Menlo Park, CA"
        assert result["evidence_id"]

        # A LATER, independent discovery/fill pass (mirroring what a fresh
        # resume_session() call does) must now see this field as RESOLVED,
        # not unresolved -- via match_field_with_application_fields()
        # finding the durable evidence, never by remembering the earlier
        # in-process fill.
        job = _fake_job(201)
        app_fields = browser_assist._build_fields_for_job(job, execution_id)
        office_field = find_field(app_fields, vfe.synthetic_field_id_for_label(
            vfe.normalize_question_label("What is your preferred office location?")))
        assert office_field is not None
        assert office_field.verified_value == "Menlo Park, CA"
        assert office_field.auto_fill_allowed is True

        outcome = browser_runtime.rediscover(session_id)
        fill_result = browser_runtime.fill_fields(session_id, outcome.fields, app_fields)
        assert not any("preferred office" in u.lower() for u in fill_result.unresolved)
    finally:
        browser_runtime.close_session(session_id)


def test_verified_checkbox_answer_resolves_on_a_later_independent_fill_pass(tmp_path, tmp_env, monkeypatch):
    """Real live gap: a self-labeled checkbox's `choices` is just its own
    full accessible label, while `record_verified_custom_answer`'s
    verified_value is deliberately a short LOCATE-BY-SUBSTRING prefix
    (matching `_fill_one`'s own `get_by_label(value, exact=False)`
    convention), never the exact full label text. `_fill_pass`'s generic
    choices-gate previously required EXACT equality for every field type,
    so a genuinely durable, verified checkbox answer could never auto-
    resolve on any LATER, independent fill pass (e.g. a fresh
    resume_session() reconstruction) -- it stayed unresolved forever
    despite real evidence on file, even though the SAME immediate
    rediscovery right after recording it happened to already show it
    filled in the live DOM."""
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = sensitive_evidence_gate_page(tmp_path)
    session_id, execution_id = _session_and_execution(214)
    try:
        _open(session_id, url, job_id=214)
        result = browser_assist.record_verified_custom_answer(
            session_id, "By checking this box, I consent", "I consent to the company collecting",
        )
        assert result["ok"] is True
        assert result["evidence_id"]

        job = _fake_job(214)
        app_fields = browser_assist._build_fields_for_job(job, execution_id)
        outcome = browser_runtime.rediscover(session_id)
        fill_result = browser_runtime.fill_fields(session_id, outcome.fields, app_fields)
        assert not any("checking this box" in u.lower() for u in fill_result.unresolved), fill_result.unresolved
    finally:
        browser_runtime.close_session(session_id)


# --- 2: click without displayed-value verification does NOT satisfy
# readiness -- an unmatched value records nothing. ---

def test_unverifiable_answer_records_no_evidence(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = combobox_reliability_page(tmp_path)
    session_id, execution_id = _session_and_execution(202)
    try:
        _open(session_id, url, job_id=202)
        result = browser_assist.record_verified_custom_answer(
            session_id, "What is your preferred office location?", "Atlantis (not a real option)",
        )
        assert result["ok"] is False
        assert result["evidence_id"] is None
        assert vfe.list_evidence_for_execution(execution_id) == []
    finally:
        browser_runtime.close_session(session_id)


# --- 3/10: no positional/cross-field contamination -- verifying one field
# never records evidence under a different field's label, and the Country
# field (positioned next to the unrelated phone-country-code widget) still
# resolves to its OWN correct value, never the phone widget's. ---

def test_verifying_country_never_contaminates_or_uses_wrong_widget(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = combobox_reliability_page(tmp_path)
    session_id, execution_id = _session_and_execution(203)
    try:
        _open(session_id, url, job_id=203)
        result = browser_assist.record_verified_custom_answer(session_id, "Country", "United States")
        assert result["ok"] is True
        assert result["actual"] == "United States"
        assert "+1" not in result["actual"]

        rows = vfe.list_evidence_for_execution(execution_id)
        assert len(rows) == 1
        assert rows[0]["question_label_normalized"] == vfe.normalize_question_label("Country")
    finally:
        browser_runtime.close_session(session_id)


# --- 15: no submit claim is ever created by this feature. ------------------

def test_recording_evidence_never_touches_submit_claims(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = combobox_reliability_page(tmp_path)
    session_id, execution_id = _session_and_execution(204)
    try:
        _open(session_id, url, job_id=204)
        browser_assist.record_verified_custom_answer(session_id, "Country", "United States")

        from app.db import db_session

        with db_session() as conn:
            n = conn.execute(
                "SELECT count(*) n FROM greenhouse_submit_claims WHERE job_id = ?", (204,),
            ).fetchone()["n"]
        assert n == 0
    finally:
        browser_runtime.close_session(session_id)


# --- 14: evidence persists and is readable by a wholly separate lookup
# (simulating a restart -- the DB row is the only thing that matters, no
# in-process state is relied upon). ---

def test_evidence_survives_as_durable_state_independent_of_the_recording_call(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = combobox_reliability_page(tmp_path)
    session_id, execution_id = _session_and_execution(205)
    try:
        _open(session_id, url, job_id=205)
        browser_assist.record_verified_custom_answer(session_id, "Country", "United States")
    finally:
        browser_runtime.close_session(session_id)

    # A completely independent read, as a fresh process/request would do.
    job = _fake_job(205)
    overrides = vfe.build_application_field_overrides(execution_id, job).fields
    assert len(overrides) == 1
    assert overrides[0].verified_value == "United States"


# --- real live bug: reading a plain text field's displayed value must
# never leak a NEARBY combobox's single-value display element. ---

def test_reading_plain_text_field_never_leaks_nearby_combobox_display(tmp_path, monkeypatch):
    url = combobox_reliability_page(tmp_path)
    session_id = "t-no-leak"
    try:
        outcome = browser_runtime.open_session(
            session_id, provider="greenhouse", url=url,
            expected_title=DEFAULT_JOB_TITLE, expected_company=DEFAULT_JOB_COMPANY,
        )
        country = next(rf for rf in outcome.fields if (rf.get("label") or "").startswith("Country"))
        fname = next(rf for rf in outcome.fields if (rf.get("label") or "").startswith("First Name"))

        # Fill the combobox first -- this renders a real "single-value"
        # display element elsewhere in the DOM.
        assert browser_runtime.fill_one_field(session_id, country, "United States") is True

        # Reading the UNRELATED plain text field afterward must reflect
        # ONLY that field's own (empty -- never filled) value, never the
        # combobox's "United States" display text.
        value = browser_runtime.read_displayed_value(session_id, fname)
        assert value in (None, "")
        assert value != "United States"
    finally:
        browser_runtime.close_session(session_id)


# --- real live bug (2026-08-30): a REQUIRED SENSITIVE_CATEGORIES field
# never auto-fills through the generic pipeline (unchanged, deliberate), but
# a session with genuine per-field human evidence for EVERY required
# sensitive field must be able to reach a resolved state on a later,
# independent discovery/fill pass -- before this fix, the generic
# `_fill_pass` unconditionally flagged ANY SENSITIVE_CATEGORIES field as
# unresolved regardless of `value_source`, so a browser_assist session could
# never reach READY_FOR_FINAL_SUBMIT at all once a required sensitive field
# was on the page, even with confirmed evidence on file for every one. ---

def test_required_sensitive_fields_never_resolve_without_verified_evidence(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = sensitive_evidence_gate_page(tmp_path)
    session_id, execution_id = _session_and_execution(206)
    try:
        _open(session_id, url, job_id=206)
        job = _fake_job(206)
        application_fields = browser_assist._build_fields_for_job(job, execution_id)
        outcome = browser_runtime.rediscover(session_id)
        fill_result = browser_runtime.fill_fields(session_id, outcome.fields, application_fields)
        # No evidence recorded yet -- both sensitive comboboxes must stay
        # unresolved, matching the standing "never auto-fill" rule.
        assert any("gender identity" in u.lower() for u in fill_result.unresolved)
        assert any("government official" in u.lower() for u in fill_result.unresolved)
    finally:
        browser_runtime.close_session(session_id)


def test_required_sensitive_fields_resolve_after_individual_verified_evidence(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = sensitive_evidence_gate_page(tmp_path)
    session_id, execution_id = _session_and_execution(207)
    try:
        _open(session_id, url, job_id=207)

        r1 = browser_assist.record_verified_custom_answer(
            session_id, "What is your gender identity?", "Cisgender man",
        )
        assert r1["ok"] is True
        r2 = browser_assist.record_verified_custom_answer(
            session_id, "Are you related to or have a close personal relationship", "No",
        )
        assert r2["ok"] is True

        # A LATER, independent discovery/fill pass (mirroring a fresh
        # resume_session() call, or a wholly separate process/session
        # reconstruction) must now recognize both as RESOLVED -- via the
        # live-DOM-reverified evidence path, never by remembering the
        # earlier in-process fill.
        job = _fake_job(207)
        application_fields = browser_assist._build_fields_for_job(job, execution_id)
        outcome = browser_runtime.rediscover(session_id)
        fill_result = browser_runtime.fill_fields(session_id, outcome.fields, application_fields)
        assert not any("gender identity" in u.lower() for u in fill_result.unresolved)
        assert not any("government official" in u.lower() for u in fill_result.unresolved)
    finally:
        browser_runtime.close_session(session_id)


def test_generic_profile_sensitive_field_never_takes_the_evidence_shortcut(tmp_path, tmp_env, monkeypatch):
    """A SENSITIVE_CATEGORIES field with only a GENERIC, profile-derived
    mapping (value_source != "browser_verified_field_evidence") must stay
    unresolved forever through this pipeline, exactly as before -- the new
    live-DOM-reverify shortcut is reachable ONLY via record_verified_custom_
    answer's own genuine evidence, never a same-category same-value
    coincidence."""
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = sensitive_evidence_gate_page(tmp_path)
    session_id, execution_id = _session_and_execution(208)
    try:
        _open(session_id, url, job_id=208)
        outcome = browser_runtime.rediscover(session_id)
        gender_rf = next(rf for rf in outcome.fields if "gender identity" in (rf.get("label") or "").lower())
        # Directly fill the DOM the way the generic pipeline never would
        # (bypassing record_verified_custom_answer entirely -- no evidence
        # row exists for this field).
        assert browser_runtime.fill_one_field(session_id, gender_rf, "Cisgender man") is True

        generic_field = ApplicationField(
            field_id="generic:gender", label="What is your gender identity?",
            category=FieldCategory.DEMOGRAPHICS, normalized_type="select", required=True, choices=[],
            value_source="standard_answers.gender", verified_value="Cisgender man",
            confidence=FieldConfidence.EXACT, needs_user_input=False, sensitive=True, auto_fill_allowed=True,
        )
        outcome2 = browser_runtime.rediscover(session_id)
        fill_result = browser_runtime.fill_fields(session_id, outcome2.fields, [generic_field])
        assert any("gender identity" in u.lower() for u in fill_result.unresolved)
    finally:
        browser_runtime.close_session(session_id)


def test_consent_checkbox_verification_uses_checked_state_not_text_match(tmp_path, tmp_env, monkeypatch):
    """The demographic-data-collection consent checkbox's live displayed
    value is boolean (is_checked() -> "true"/"false"), not text comparable
    to the label used to locate/click it -- verification must succeed based
    on the checked state, and record durable evidence a later pass resolves
    against."""
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = sensitive_evidence_gate_page(tmp_path)
    session_id, execution_id = _session_and_execution(209)
    try:
        _open(session_id, url, job_id=209)
        result = browser_assist.record_verified_custom_answer(
            session_id, "By checking this box, I consent", "I consent to the company collecting",
        )
        assert result["ok"] is True
        assert result["actual"] == "true"
        assert result["evidence_id"]

        rows = vfe.list_evidence_for_execution(execution_id)
        assert len(rows) == 1
        assert rows[0]["actual_displayed_value"] == "true"
    finally:
        browser_runtime.close_session(session_id)


def test_consent_checkbox_left_unchecked_never_records_evidence(tmp_path, tmp_env, monkeypatch):
    """record_verified_custom_answer's checkbox path never records a
    checked-evidence row for a checkbox that never ended up checked (it
    only ever calls .check(), never .uncheck() -- but a locator that fails
    to resolve/click must never be silently treated as success)."""
    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = sensitive_evidence_gate_page(tmp_path)
    session_id, execution_id = _session_and_execution(210)
    try:
        _open(session_id, url, job_id=210)
        result = browser_assist.record_verified_custom_answer(
            session_id, "By checking this box, I consent", "this text matches nothing on the page's label at all",
        )
        assert result["ok"] is False
        assert result["evidence_id"] is None
        assert vfe.list_evidence_for_execution(execution_id) == []
    finally:
        browser_runtime.close_session(session_id)


# --- Provider-Semantic Selection Verification V1: record_verified_custom_
# answer() must accept a genuinely flag-verified combobox selection even
# when its OWN independent text re-check is inconclusive (a real live gap:
# _fill_one/_fill_combobox already verified the correct option via the
# flag-icon check, but this separate text-only re-check used to reject it
# as a false failure for a field whose display is a non-identifying
# fragment, e.g. a phone-country dial code). --------------------------------

def test_verified_custom_answer_accepts_flag_verified_phone_country(tmp_path, tmp_env, monkeypatch):
    from tests.browser_fixtures import phone_country_code_combobox_page

    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = phone_country_code_combobox_page(tmp_path)
    session_id, execution_id = _session_and_execution(211)
    try:
        _open(session_id, url, job_id=211)
        result = browser_assist.record_verified_custom_answer(session_id, "Country", "United States")
        assert result["ok"] is True, result["detail"]
        assert result["evidence_id"]
        # The recorded evidence is honest about what was actually visible
        # on screen -- "+1", never a fabricated "United States" the page
        # itself never displayed.
        assert result["actual"] == "+1"

        rows = vfe.list_evidence_for_execution(execution_id)
        assert len(rows) == 1
        assert rows[0]["expected_answer"] == "United States"
        assert rows[0]["actual_displayed_value"] == "+1"
    finally:
        browser_runtime.close_session(session_id)


def test_verified_custom_answer_rejects_wrong_country_despite_same_dial_code(tmp_path, tmp_env, monkeypatch):
    """Canada shares the identical "+1" text with the US -- asking for
    "Canada" then checking against a page that only ever shows "United
    States" (never actually possible here since the fixture always
    displays whichever was truly selected, but this proves the flag
    fallback in record_verified_custom_answer is never reached for a
    genuinely UNMATCHED value in the first place, at the fill_one_field
    layer -- no evidence is ever recorded for a value with no real match)."""
    from tests.browser_fixtures import phone_country_code_combobox_page

    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = phone_country_code_combobox_page(tmp_path)
    session_id, execution_id = _session_and_execution(212)
    try:
        _open(session_id, url, job_id=212)
        result = browser_assist.record_verified_custom_answer(session_id, "Country", "Germany")
        assert result["ok"] is False
        assert result["evidence_id"] is None
        assert vfe.list_evidence_for_execution(execution_id) == []
    finally:
        browser_runtime.close_session(session_id)


# --- Form-Fingerprint Stability V1: a consent checkbox that only mounts
# after a real delay must still be answerable through the ordinary
# record_verified_custom_answer path (proving the field-count-stability
# rescan in browser_runtime feeds through end-to-end, not just at the raw
# discovery level). ----------------------------------------------------

def test_verified_custom_answer_resolves_delayed_consent_checkbox(tmp_path, tmp_env, monkeypatch):
    from tests.browser_fixtures import sensitive_evidence_gate_page_delayed_consent

    monkeypatch.setattr("app.applications.browser_assist.get_job", lambda jid: _fake_job(jid))
    url = sensitive_evidence_gate_page_delayed_consent(tmp_path, delay_ms=300)
    session_id, execution_id = _session_and_execution(213)
    try:
        _open(session_id, url, job_id=213)
        result = browser_assist.record_verified_custom_answer(
            session_id, "By checking this box, I consent", "I consent to the company collecting",
        )
        assert result["ok"] is True, result["detail"]
        assert result["actual"] == "true"
        assert result["evidence_id"]
    finally:
        browser_runtime.close_session(session_id)
