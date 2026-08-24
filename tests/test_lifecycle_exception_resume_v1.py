"""Application-lifecycle-exception-resume-v1: the durable blocker model
(app.applications.blockers), the wiring into the existing executor/browser-
assist/reconcile detection points, and the new consumer board/detail/demo
surfaces. Covers the 13 acceptance scenarios from the feature's build brief."""

import json

from fastapi.testclient import TestClient

from app import config
from app.agent import state as agent_state
from app.applications import blockers, board, repo
from app.applications.approval import approve_and_apply, check_approval_freshness
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.reconcile import reconcile_execution
from app.candidate.profile import save_profile
from app.jobs_repo import get_job, update_job
from app.main import app
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
    "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
    "H-1B sponsorship is available for this role."
)


def _mock_job(external_job_id: str, scenario: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": scenario}),
        mode=ApplicationMode.ASSIST,
    )


def _setup(monkeypatch, sample_profile):
    agent_state.set_enabled(False)
    save_profile(sample_profile)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)


def _prepare(monkeypatch, sample_profile, external_id: str, scenario: str) -> dict:
    _setup(monkeypatch, sample_profile)
    job = _mock_job(external_id, scenario)
    ingest_and_process(job)
    from app.jobs_repo import get_job_by_provider_external_id

    saved_job = get_job_by_provider_external_id("mock_ats", external_id)
    result = queue_application(saved_job.id, mode="ASSIST")
    assert result.queued, result.reason
    process_execution(result.execution_id)
    return {"job_id": saved_job.id, "execution_id": result.execution_id}


# --- 1/9: successful preparation -> approval -> mock submit -> receipt -----

def test_successful_preparation_to_approval_to_receipt(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-1", "simple")
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value
    assert blockers.get_active_blocker_for_execution(ctx["execution_id"]) is None

    result = approve_and_apply(ctx["job_id"])
    assert result.ok, result.reason
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.APPLIED.value
    assert blockers.get_active_blocker_for_execution(ctx["execution_id"]) is None

    from app.applications.receipts import get_latest_receipt_for_execution

    receipt = get_latest_receipt_for_execution(ctx["execution_id"])
    assert receipt is not None
    assert receipt["provider"] == "mock_ats"
    assert receipt["confirmation_id"]


# --- 2/11: job expires during preparation -----------------------------------

def test_job_expired_raises_terminal_blocker_and_never_submits(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-2", "job_expired")
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.JOB_NO_LONGER_ACTIVE.value
    assert execution["active"] == 0

    blocker = blockers.get_active_blocker_for_execution(ctx["execution_id"])
    assert blocker is not None
    assert blocker["blocker_code"] == blockers.BlockerCode.JOB_EXPIRED.value
    assert blocker["blocker_class"] == blockers.BlockerClass.TERMINAL.value

    audit = repo.list_audit_log(execution_id=ctx["execution_id"])
    assert not any(e["event_type"] == "submit_attempted" for e in audit)

    from app.db import db_session

    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM mock_ats_server_records WHERE job_id = ?", (ctx["job_id"],),
        ).fetchone()
    assert row is None  # provider.submit() was genuinely never called

    # Terminal blockers cannot be incorrectly resumed into submission (#11).
    resumed = process_execution(ctx["execution_id"])
    assert resumed["status"] == ExecutionStatus.JOB_NO_LONGER_ACTIVE.value
    assert resumed["active"] == 0


# --- 3: CAPTCHA -> NEEDS_CAPTCHA -> submission blocked ----------------------

def test_captcha_blocker_blocks_submission(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-3", "captcha")
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.NEEDS_USER_ACTION.value

    blocker = blockers.get_active_blocker_for_execution(ctx["execution_id"])
    assert blocker["blocker_code"] == blockers.BlockerCode.NEEDS_CAPTCHA.value
    assert blocker["blocker_class"] == blockers.BlockerClass.RESUMABLE.value

    # approve_and_apply must never bypass a live CAPTCHA blocker.
    result = approve_and_apply(ctx["job_id"])
    assert not result.ok


# --- 4: unknown employer question -> NEEDS_USER_INPUT -> answer -> resume --

def test_unknown_question_resolve_and_checkpoint_resume(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-4", "unknown_question")
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.NEEDS_USER_ACTION.value
    blocker = blockers.get_active_blocker_for_execution(ctx["execution_id"])
    assert blocker["blocker_code"] == blockers.BlockerCode.NEEDS_USER_INPUT.value
    assert blocker["required_action"] == "ANSWER_AND_CONTINUE"
    created_at = blocker["created_at"]

    # Candidate answers out-of-band -- simulated here exactly as
    # app.applications.demo.resolve_demo("unknown_question") does: the
    # form's own shape never changes (that would be a genuine, separately-
    # detected FORM_SCHEMA_CHANGED condition, not "the user answered").
    update_job(ctx["job_id"], provider_metadata=json.dumps(
        {"mock_scenario": "unknown_question", "demo_answered": True}))
    process_execution(ctx["execution_id"])

    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value
    assert blockers.get_active_blocker_for_execution(ctx["execution_id"]) is None

    history = blockers.list_blockers_for_execution(ctx["execution_id"])
    assert len(history) == 1
    assert history[0]["created_at"] == created_at
    assert history[0]["resolved_at"] is not None


# --- 5: auth blocker -> resolve -> checkpoint resume ------------------------

def test_auth_blocker_resolve_and_resume(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-5", "login_required")
    blocker = blockers.get_active_blocker_for_execution(ctx["execution_id"])
    assert blocker["blocker_code"] == blockers.BlockerCode.NEEDS_AUTH.value

    update_job(ctx["job_id"], provider_metadata=json.dumps({"mock_scenario": "simple"}))
    process_execution(ctx["execution_id"])
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value
    assert blockers.get_active_blocker_for_execution(ctx["execution_id"]) is None


def test_email_verification_and_account_creation_blockers(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-5b", "email_verification")
    blocker = blockers.get_active_blocker_for_execution(ctx["execution_id"])
    assert blocker["blocker_code"] == blockers.BlockerCode.NEEDS_EMAIL_VERIFICATION.value

    ctx2 = _prepare(monkeypatch, sample_profile, "lc-5c", "account_creation_required")
    blocker2 = blockers.get_active_blocker_for_execution(ctx2["execution_id"])
    assert blocker2["blocker_code"] == blockers.BlockerCode.NEEDS_ACCOUNT_CREATION.value


# --- 6: approval becomes stale while paused ---------------------------------

def test_stale_approval_blocks_submission_and_returns_to_ready(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-6", "simple")
    result = repo.get_execution(ctx["execution_id"])
    assert result["status"] == ExecutionStatus.SUBMISSION_READY.value

    from app.applications.approval import _record_approval_row

    job = get_job(ctx["job_id"])
    # Record an approval, then mutate the job's JD after the fact -- the
    # exact "approval becomes stale while paused" scenario.
    approval_id = _record_approval_row(job, result, provider_submission_supported=True)
    update_job(ctx["job_id"], description=JD_TEXT + " Also requires 10 years of COBOL experience.",
               resume_jd_fingerprint="changed-fingerprint")

    fresh = check_approval_freshness(ctx["job_id"])
    assert fresh["has_approval"]
    assert not fresh["valid"]
    assert any("job description changed" in r for r in fresh["reasons"])

    from app.applications.approval import verify_durable_approval_for_submission

    fresh_job = get_job(ctx["job_id"])
    ok, reason = verify_durable_approval_for_submission(fresh_job, result)
    assert not ok
    assert "stale" in reason


# --- 7: submit timeout -> SUBMISSION_STATUS_UNKNOWN -> no blind retry ------

def test_submission_status_unknown_never_blindly_retried(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-7", "timeout_after_submit")
    result = approve_and_apply(ctx["job_id"])
    assert result.ok, result.reason
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value

    blocker = blockers.get_active_blocker_for_execution(ctx["execution_id"])
    assert blocker["blocker_code"] == blockers.BlockerCode.SUBMISSION_STATUS_UNKNOWN.value
    assert blocker["required_action"] == "CHECK_APPLICATION_STATUS"

    # A second process_execution() call must never blindly resubmit.
    again = process_execution(ctx["execution_id"])
    assert again["status"] == ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value

    from app.applications.mock_ats import PROVIDER_NAME
    from app.db import db_session

    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM mock_ats_server_records WHERE job_id = ?", (ctx["job_id"],),
        ).fetchone()
    assert row is not None  # the mock ATS DID genuinely receive it server-side

    # Reconciliation resolves the blocker via the real evidence path.
    outcome = reconcile_execution(ctx["execution_id"], "confirmed_applied", confirmation_id=row["confirmation_id"])
    assert outcome.ok
    assert blockers.get_active_blocker_for_execution(ctx["execution_id"]) is None
    history = blockers.list_blockers_for_execution(ctx["execution_id"])
    assert history[-1]["resolution_note"].startswith("reconciled:confirmed_applied")


# --- 8: unsupported provider -> assist action -> never fake APPLIED --------

def test_unsupported_form_discovery_never_fakes_applied(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-8", "form_not_found")
    execution = repo.get_execution(ctx["execution_id"])
    assert execution["status"] == ExecutionStatus.NEEDS_USER_ACTION.value
    assert execution["status"] != ExecutionStatus.APPLIED.value

    blocker = blockers.get_active_blocker_for_execution(ctx["execution_id"])
    assert blocker["blocker_code"] == blockers.BlockerCode.PROVIDER_UNSUPPORTED.value


# --- 10: blocker resolution is idempotent and concurrency-safe -------------

def test_blocker_raise_and_resolve_idempotent(tmp_env, sample_profile, monkeypatch):
    _setup(monkeypatch, sample_profile)
    first = blockers.raise_blocker("exec-x", 999, blockers.BlockerCode.NEEDS_CAPTCHA, provider="mock_ats")
    second = blockers.raise_blocker("exec-x", 999, blockers.BlockerCode.NEEDS_CAPTCHA, provider="mock_ats")
    assert first["id"] == second["id"]
    assert len(blockers.list_blockers_for_execution("exec-x")) == 1

    resolved = blockers.resolve_blocker("exec-x", resolution_note="done")
    assert resolved is not None
    again = blockers.resolve_blocker("exec-x", resolution_note="done again")
    assert again is None  # idempotent no-op -- nothing left to resolve

    # A DIFFERENT code for the same execution supersedes, never violates the
    # partial-unique-index concurrency guard.
    third = blockers.raise_blocker("exec-x", 999, blockers.BlockerCode.NEEDS_AUTH, provider="mock_ats")
    assert third["blocker_code"] == blockers.BlockerCode.NEEDS_AUTH.value
    assert blockers.get_active_blocker_for_execution("exec-x")["id"] == third["id"]


# --- 12: application detail/timeline shows correct user-facing states -----

def test_application_detail_and_board_show_plain_language(tmp_env, sample_profile, monkeypatch):
    ctx = _prepare(monkeypatch, sample_profile, "lc-12", "captcha")
    client = TestClient(app)

    resp = client.get(f"/applications/{ctx['execution_id']}/detail")
    assert resp.status_code == 200
    assert "CAPTCHA required" in resp.text
    assert "NEEDS_USER_ACTION" not in resp.text  # no raw enum leaked to the user
    assert "Timeline" in resp.text
    assert "Receipt" in resp.text

    board_resp = client.get("/applications/board")
    assert board_resp.status_code == 200
    assert "Needs Action" in board_resp.text
    assert "Ready to Apply" in board_resp.text
    assert "CAPTCHA required" in board_resp.text


def test_board_categorizes_ready_to_apply_and_submitted(tmp_env, sample_profile, monkeypatch):
    ready_ctx = _prepare(monkeypatch, sample_profile, "lc-13a", "simple")
    submitted_ctx = _prepare(monkeypatch, sample_profile, "lc-13b", "simple")
    approve_and_apply(submitted_ctx["job_id"])

    buckets = board.build_board()
    ready_ids = {c["execution_id"] for c in buckets[board.BUCKET_READY_TO_APPLY]}
    submitted_ids = {c["execution_id"] for c in buckets[board.BUCKET_SUBMITTED]}
    assert ready_ctx["execution_id"] in ready_ids
    assert submitted_ctx["execution_id"] in submitted_ids


def test_demo_scenarios_never_pollute_real_board(tmp_env, sample_profile, monkeypatch):
    from app.applications import demo as demo_mod

    _setup(monkeypatch, sample_profile)
    demo_mod.run_demo("captcha")
    buckets = board.build_board()
    all_titles = [c["title"] for cards in buckets.values() for c in cards]
    assert not any("Demo Backend Engineer" in t for t in all_titles)

    status = demo_mod.list_demo_status()
    captcha_entry = next(s for s in status if s["key"] == "captcha")
    assert captcha_entry["blocker"]["blocker_code"] == blockers.BlockerCode.NEEDS_CAPTCHA.value


def test_demo_page_loads_and_run_resolve_routes_work(tmp_env, sample_profile, monkeypatch):
    _setup(monkeypatch, sample_profile)
    client = TestClient(app)
    resp = client.get("/demo")
    assert resp.status_code == 200
    assert "Demo Successful Application" in resp.text

    run_resp = client.post("/demo/login_required/run", follow_redirects=False)
    assert run_resp.status_code == 303
    resolve_resp = client.post("/demo/login_required/resolve", follow_redirects=False)
    assert resolve_resp.status_code == 303

    from app.applications import demo as demo_mod

    status = demo_mod.list_demo_status()
    entry = next(s for s in status if s["key"] == "login_required")
    assert entry["execution"]["status"] == ExecutionStatus.SUBMISSION_READY.value
