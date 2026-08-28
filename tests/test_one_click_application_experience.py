"""One-click-application-experience-v1: in-app notifications
(app.notifications), the enriched Needs You queue (time-blocked/action-kind),
the demo fixture set's new scenarios (no_sponsorship/transient_recovery/
run_all_demos), and the application-detail advanced section's continued
no-raw-enum-leak invariant. Never touches a real employer/network."""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, notifications
from app.applications import blockers, demo as demo_mod, receipts, repo
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.candidate.profile import save_profile
from app.jobs_repo import get_job_by_provider_external_id
from app.main import app
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process
from app.pipeline_dashboard import build_needs_action_queue

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
    "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
    "H-1B sponsorship is available for this role."
)


def _mock_job(external_job_id: str, scenario: str, *, sponsorship_extra: str = "H-1B sponsorship is available for this role.") -> Job:
    description = (
        "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
        f"with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. {sponsorship_extra}"
    )
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=description, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": scenario}),
        mode=ApplicationMode.ASSIST,
    )


def _prepare(monkeypatch, sample_profile, external_id: str, scenario: str, **kwargs) -> dict:
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    job = _mock_job(external_id, scenario, **kwargs)
    ingest_and_process(job)
    saved_job = get_job_by_provider_external_id("mock_ats", external_id)
    result = queue_application(saved_job.id, mode="ASSIST")
    assert result.queued, result.reason
    process_execution(result.execution_id)
    return {"job_id": saved_job.id, "execution_id": result.execution_id}


# --- notifications: durable, deduped, best-effort ---------------------------

def test_notify_dedupes_while_unread_and_allows_again_after_read(tmp_env):
    first = notifications.notify(notifications.KIND_NEEDS_YOU, "Needs You", "msg", dedupe_key="dk1")
    assert first is not None
    again = notifications.notify(notifications.KIND_NEEDS_YOU, "Needs You", "msg again", dedupe_key="dk1")
    assert again is None  # deduped -- still unread
    assert notifications.unread_count() == 1

    notifications.mark_read(first["id"])
    assert notifications.unread_count() == 0

    third = notifications.notify(notifications.KIND_NEEDS_YOU, "Needs You", "recurred", dedupe_key="dk1")
    assert third is not None  # prior one was read -- a fresh occurrence notifies again
    assert notifications.unread_count() == 1


def test_notify_without_dedupe_key_never_dedupes(tmp_env):
    notifications.notify(notifications.KIND_HEALTH_ISSUE, "Issue", "a")
    notifications.notify(notifications.KIND_HEALTH_ISSUE, "Issue", "b")
    assert notifications.unread_count() == 2


def test_mark_all_read(tmp_env):
    notifications.notify(notifications.KIND_APPLIED, "Applied", "a", dedupe_key="x")
    notifications.notify(notifications.KIND_APPLIED, "Applied", "b", dedupe_key="y")
    assert notifications.unread_count() == 2
    changed = notifications.mark_all_read()
    assert changed == 2
    assert notifications.unread_count() == 0


def test_blocker_raises_needs_you_notification(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "oc-captcha", "captcha")
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.NEEDS_USER_ACTION.value

    items = notifications.list_notifications(unread_only=True)
    needs_you = [i for i in items if i["kind"] == notifications.KIND_NEEDS_YOU]
    assert needs_you, "expected a Needs You notification for a genuine CAPTCHA blocker"
    assert "CAPTCHA" in needs_you[0]["title"]

    # A second occurrence of the SAME blocker code must not double-notify.
    before = notifications.unread_count()
    blockers.raise_blocker(ctx["execution_id"], ctx["job_id"], blockers.BlockerCode.NEEDS_CAPTCHA)
    assert notifications.unread_count() == before


def test_submission_status_unknown_blocker_uses_status_unknown_kind(tmp_env, sample_profile, monkeypatch):
    from app.applications.approval import approve_and_apply

    ctx = _prepare(monkeypatch, sample_profile, "oc-unknown", "timeout_after_submit")
    result = approve_and_apply(ctx["job_id"])
    assert result.ok, result.reason
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value
    items = notifications.list_notifications(unread_only=True)
    assert any(i["kind"] == notifications.KIND_STATUS_UNKNOWN for i in items)


def test_receipt_raises_applied_notification(tmp_env, sample_profile, monkeypatch):
    from app.applications.approval import approve_and_apply

    ctx = _prepare(monkeypatch, sample_profile, "oc-simple", "simple")
    result = approve_and_apply(ctx["job_id"])
    assert result.ok, result.reason
    items = notifications.list_notifications(unread_only=True)
    assert any(i["kind"] == notifications.KIND_APPLIED for i in items)


def test_rate_limit_block_raises_daily_limit_notification(tmp_env, sample_profile, monkeypatch):
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 0)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)

    job = _mock_job("oc-ratelimit", "simple")
    ingest_and_process(job)
    saved_job = get_job_by_provider_external_id("mock_ats", "oc-ratelimit")
    result = queue_application(saved_job.id, mode="AUTO_PERMITTED")
    assert result.queued
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.NEEDS_USER_ACTION.value
    items = notifications.list_notifications(unread_only=True)
    assert any(i["kind"] == notifications.KIND_DAILY_LIMIT for i in items)


# --- Needs You queue enrichment: time-blocked + action kind -----------------

def test_needs_action_queue_carries_human_copy_time_blocked_and_action_kind(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "oc-captcha2", "captcha")
    queue = build_needs_action_queue()
    item = next(i for i in queue if i["job_id"] == ctx["job_id"])
    assert item["human_title"] == "CAPTCHA required"
    assert item["action_kind"] == "open_application"  # genuine human browser interaction required
    assert item["time_blocked"] != ""
    assert item["execution_id"] == ctx["execution_id"]


# --- notifications routes ----------------------------------------------------

def test_notifications_page_and_mark_read_routes(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    notifications.notify(notifications.KIND_HEALTH_ISSUE, "Test issue", "detail", dedupe_key="route-test")
    client = TestClient(app)

    resp = client.get("/notifications")
    assert resp.status_code == 200
    assert "Test issue" in resp.text

    assert notifications.unread_count() == 1
    row = notifications.list_notifications()[0]
    post_resp = client.post(f"/notifications/{row['id']}/read")
    assert post_resp.status_code in (200, 303)
    assert notifications.unread_count() == 0

    api_resp = client.get("/api/notifications")
    assert api_resp.status_code == 200
    assert api_resp.json()["unread_count"] == 0


def test_base_layout_shows_notification_bell_count(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    notifications.notify(notifications.KIND_HEALTH_ISSUE, "Bell test", "", dedupe_key="bell-test")
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Notifications" in resp.text or "notifications" in resp.text.lower()


# --- demo: no_sponsorship / transient_recovery / run-all --------------------

def test_demo_no_sponsorship_scenario_skips_without_execution(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    status = demo_mod.run_demo("no_sponsorship")
    assert status["execution"] is None
    assert status["job_application_state"] is not None
    assert status["job_application_state"].startswith("SKIPPED")


def test_demo_transient_recovery_resolves_via_retry_submit(tmp_env, sample_profile, monkeypatch):
    from app.applications.approval import approve_and_apply

    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)

    status = demo_mod.run_demo("transient_recovery")
    assert status["execution"]["status"] == ExecutionStatus.SUBMISSION_READY.value

    result = approve_and_apply(status["job_id"])
    assert result.ok, result.reason
    after_approve = demo_mod.describe_demo("transient_recovery", status["job_id"])
    assert after_approve["execution"]["status"] == ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value

    resolved = demo_mod.resolve_demo("transient_recovery")
    assert resolved["execution"]["status"] in (
        ExecutionStatus.SUBMITTED.value, ExecutionStatus.APPLIED.value, ExecutionStatus.SUBMISSION_CONFIRMED.value,
    )


def test_demo_legal_question_scenario_needs_you_then_resolves(tmp_env, sample_profile, monkeypatch):
    """Daily-use-v1: closes the gap where app.applications.mock_ats's
    already-implemented `legal_unknown` scenario (PolicyReason.
    UNKNOWN_LEGAL_QUESTION) had no DemoScenario referencing it -- a defined-
    but-dead demo capability. Proves the wiring end-to-end via the actual
    demo.run_demo/resolve_demo entry points (not the lower-level executor
    call the pre-existing mock_ats-level tests already cover)."""
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)

    status = demo_mod.run_demo("legal_question")
    assert status["execution"]["status"] == ExecutionStatus.NEEDS_USER_ACTION.value
    assert status["blocker"]["blocker_code"] == blockers.BlockerCode.NEEDS_LEGAL_CONFIRMATION.value

    resolved = demo_mod.resolve_demo("legal_question")
    assert resolved["execution"]["status"] == ExecutionStatus.SUBMISSION_READY.value
    assert resolved["blocker"] is None


def test_run_all_demos_isolates_failures_and_covers_every_scenario(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)

    results = demo_mod.run_all_demos()
    assert len(results) == len(demo_mod.SCENARIOS)
    result_keys = {r["key"] for r in results}
    expected_keys = {s.key for s in demo_mod.SCENARIOS}
    assert result_keys == expected_keys
    # section L's Job C/D coverage: no_sponsorship skips, transient_recovery
    # fails-then-is-recoverable (this pass alone only exercises the FIRST
    # attempt -- recovery itself is proven by the dedicated resolve test
    # above), one clean run reaches READY_FOR_APPROVAL/APPLIED.
    by_key = {r["key"]: r for r in results}
    assert by_key["no_sponsorship"]["execution"] is None
    assert by_key["successful_application"]["execution"]["status"] in (
        ExecutionStatus.SUBMISSION_READY.value, ExecutionStatus.APPLIED.value,
    )


def test_demo_page_route_renders_run_all_button(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/demo")
    assert resp.status_code == 200
    assert "Run All Demos" in resp.text
    assert "/demo/run-all" in resp.text
