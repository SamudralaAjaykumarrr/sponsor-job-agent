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
