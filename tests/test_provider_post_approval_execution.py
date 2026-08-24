"""Provider Post-Approval Execution V1:
  - app.applications.receipts (durable, append-only submission evidence)
  - app.applications.post_approval (the APPROVE & APPLY -> browser-assist
    session bridge for real providers with no verified automated
    final-submission capability)
  - the three new application-doctor checks this build adds

Exercises the real, unmodified executor/approval pipeline plus the
deterministic mock_ats fixture -- no real network, no real browser (the
bridge itself is monkeypatched at the browser_assist entry-point boundary,
matching this project's existing pattern for testing browser-assist
orchestration without a real Chromium instance -- see
tests/test_browser_assist_orchestration.py)."""

import json

import pytest

from app import config
from app.applications import approval as applications_approval
from app.applications import post_approval, receipts
from app.applications import repo as applications_repo
from app.applications.doctor import run_doctor
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.applications import provider_registry
from app.candidate.profile import save_profile
from app.db import db_session
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)


def _mock_job(external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )


def _prepare_ready_for_approval(job: Job) -> dict:
    result = queue_application(job.id, mode="ASSIST")
    assert result.queued
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value
    return execution


# --- receipts module ---------------------------------------------------------

def test_record_and_list_receipt_roundtrip(tmp_env):
    row = receipts.record_receipt(
        execution_id="exec_x", job_id=1, provider="mock_ats", submitted_via="headless_provider:mock_ats",
        confirmation_id="conf-1", sanitized_url="https://example.test/confirm", evidence_strength="STRONG",
        raw_message_fingerprint="fp123", approval_id="appr_x",
    )
    assert row["receipt_id"].startswith("rcpt_")
    assert row["execution_id"] == "exec_x"
    assert row["evidence_strength"] == "STRONG"

    fetched = receipts.get_receipt(row["receipt_id"])
    assert fetched == row

    latest = receipts.get_latest_receipt_for_execution("exec_x")
    assert latest["receipt_id"] == row["receipt_id"]

    listed = receipts.list_receipts(provider="mock_ats")
    assert any(r["receipt_id"] == row["receipt_id"] for r in listed)


def test_receipts_never_store_secrets_only_expected_columns(tmp_env):
    row = receipts.record_receipt(
        execution_id="exec_y", job_id=1, provider="greenhouse", submitted_via="browser_assist:greenhouse",
    )
    allowed = {
        "id", "receipt_id", "execution_id", "job_id", "provider", "submitted_via", "confirmation_id",
        "sanitized_url", "evidence_strength", "raw_message_fingerprint", "session_id", "approval_id", "created_at",
    }
    assert set(row.keys()) <= allowed


# --- executor headless path writes a receipt --------------------------------

def test_mock_ats_applied_flow_records_a_receipt(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    job = ingest_and_process(_mock_job("post-appr-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.APPLIED.value

    receipt = receipts.get_latest_receipt_for_execution(execution["execution_id"])
    assert receipt is not None
    assert receipt["submitted_via"] == "headless_provider:mock_ats"
    assert receipt["job_id"] == job.id
    assert receipt["evidence_strength"] in ("STRONG", "MODERATE")


def test_approve_and_apply_reaches_applied_and_records_receipt(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("post-appr-2"))
    _prepare_ready_for_approval(job)

    result = applications_approval.approve_and_apply(job.id)

    assert result.ok is True
    assert result.execution["status"] == ExecutionStatus.APPLIED.value

    receipt = receipts.get_latest_receipt_for_execution(result.execution_id)
    assert receipt is not None
    assert receipt["approval_id"] == result.approval_id
    # mock_ats reaches APPLIED synchronously inside process_execution --
    # the bridge is a no-op here (nothing to bridge; execution isn't APPROVED).
    assert result.browser_assist is None


# --- post-approval bridge ----------------------------------------------------

def test_bridge_noop_when_browser_assist_disabled(tmp_env, sample_profile):
    save_profile(sample_profile)

    fixture_payload = {
        "questions": [
            {"label": "First Name", "required": True,
             "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
            {"label": "Last Name", "required": True,
             "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
            {"label": "Email", "required": True,
             "fields": [{"name": "email", "type": "input_text", "values": []}]},
            {"label": "Resume/CV", "required": True,
             "fields": [{"name": "resume", "type": "input_file", "values": []}]},
        ],
    }
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture_payload)

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_provider = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id="gh-post-appr-1", company_identifier="acme", mode=ApplicationMode.ASSIST,
        ))
        _prepare_ready_for_approval(job)

        result = applications_approval.approve_and_apply(job.id)

        assert result.ok is True
        assert result.execution["status"] == ExecutionStatus.APPROVED.value
        assert result.browser_assist == {
            "attempted": False, "started": False, "session": None,
            "reason": "BROWSER_ASSIST_ENABLED is false -- browser assist was not auto-started",
        }
        # No browser-assist session/receipt for an ASSIST_ONLY provider that
        # never got past APPROVED -- never fabricated.
        assert post_approval.active_session_for_job(job.id) is None
        assert receipts.get_latest_receipt_for_execution(result.execution_id) is None
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original_provider


def test_bridge_starts_browser_session_when_enabled(tmp_env, sample_profile, monkeypatch):
    """The bridge itself is exercised end-to-end; the actual Playwright
    browser call (app.applications.browser_assist.start_session) is
    monkeypatched -- this test proves the WIRING (post_approval calls
    browser_assist.start_session with the right execution id, exactly once,
    only when the execution is genuinely APPROVED), not browser mechanics,
    which the existing Playwright/E2E suites already cover."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)

    calls = []

    def fake_start_session(execution_id):
        calls.append(execution_id)
        return {"created": True, "session": {"session_id": "sess_fake_1", "status": "ACTIVE"}}

    import app.applications.browser_assist as browser_assist_module

    monkeypatch.setattr(browser_assist_module, "start_session", fake_start_session)

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "questions": [
                {"label": "Email", "required": True,
                 "fields": [{"name": "email", "type": "input_text", "values": []}]},
            ],
        })

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_provider = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id="gh-post-appr-2", company_identifier="acme", mode=ApplicationMode.ASSIST,
        ))
        _prepare_ready_for_approval(job)

        result = applications_approval.approve_and_apply(job.id)

        assert result.execution["status"] == ExecutionStatus.APPROVED.value
        assert calls == [result.execution_id]
        assert result.browser_assist["attempted"] is True
        assert result.browser_assist["started"] is True
        assert result.browser_assist["session"]["session_id"] == "sess_fake_1"
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original_provider


def test_bridge_never_raises_into_approval_on_browser_assist_failure(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)

    import app.applications.browser_assist as browser_assist_module

    def failing_start_session(execution_id):
        raise RuntimeError("playwright not installed")

    monkeypatch.setattr(browser_assist_module, "start_session", failing_start_session)

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "questions": [
                {"label": "Email", "required": True,
                 "fields": [{"name": "email", "type": "input_text", "values": []}]},
            ],
        })

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_provider = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id="gh-post-appr-3", company_identifier="acme", mode=ApplicationMode.ASSIST,
        ))
        _prepare_ready_for_approval(job)

        # Must not raise -- the approval itself already fully succeeded
        # before the bridge is ever reached.
        result = applications_approval.approve_and_apply(job.id)
        assert result.ok is True
        assert result.execution["status"] == ExecutionStatus.APPROVED.value
        assert result.browser_assist["started"] is False
        assert "playwright not installed" in result.browser_assist["reason"]
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original_provider


def test_bridge_does_nothing_for_non_approved_execution(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    job = ingest_and_process(_mock_job("post-appr-4"))
    execution = _prepare_ready_for_approval(job)

    result = post_approval.advance_after_approval(execution["execution_id"])
    assert result["attempted"] is False
    assert "not in the APPROVED state" in result["reason"]


# --- doctor checks ------------------------------------------------------------

def test_doctor_clean_for_normal_receipt_flow(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    job = ingest_and_process(_mock_job("post-appr-doc-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.APPLIED.value

    report = run_doctor()
    assert report.serious_count == 0
    codes = {i.check for i in report.issues}
    assert "applied_execution_missing_receipt" not in codes
    assert "receipt_without_applied_execution" not in codes
    assert "named_real_provider_capability_inflated" not in codes


def test_doctor_catches_applied_execution_missing_receipt(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    job = ingest_and_process(_mock_job("post-appr-doc-2"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.APPLIED.value

    # Simulate the receipt-recording best-effort path silently failing --
    # delete the receipt it just wrote.
    with db_session() as conn:
        conn.execute("DELETE FROM application_receipts WHERE execution_id = ?", (execution["execution_id"],))

    report = run_doctor()
    codes = {i.check for i in report.issues}
    assert "applied_execution_missing_receipt" in codes
    matching = [i for i in report.issues if i.check == "applied_execution_missing_receipt"]
    assert matching[0].severity == "warning"


def test_doctor_catches_receipt_without_applied_execution(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("post-appr-doc-3"))
    result = queue_application(job.id, mode="ASSIST")
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value

    # A receipt should never exist for a non-APPLIED execution -- inject one
    # directly to simulate a bug outside the two sanctioned call sites.
    receipts.record_receipt(
        execution_id=execution["execution_id"], job_id=job.id, provider="mock_ats",
        submitted_via="headless_provider:mock_ats", evidence_strength="STRONG",
    )

    report = run_doctor()
    matching = [i for i in report.issues if i.check == "receipt_without_applied_execution"]
    assert len(matching) == 1
    assert matching[0].severity == "serious"


def test_doctor_catches_named_real_provider_capability_inflated(tmp_env, monkeypatch):
    original = GreenhouseApplicationProvider.capabilities
    from dataclasses import replace

    monkeypatch.setattr(GreenhouseApplicationProvider, "capabilities", replace(original, submission_supported=True))
    try:
        report = run_doctor()
        matching = [i for i in report.issues if i.check == "named_real_provider_capability_inflated"]
        assert len(matching) == 1
        assert "greenhouse" in matching[0].detail
        assert matching[0].severity == "serious"
    finally:
        monkeypatch.setattr(GreenhouseApplicationProvider, "capabilities", original)
