"""CLAUDE.md Phase 8 section 53: end-to-end acceptance scenarios A-J, run
against the real pipeline + real DB (tmp_env) + the deterministic mock ATS
(never a real network call, never a real ATS)."""

import json

import pytest

from app import config
from app.applications import repo as applications_repo
from app.applications.duplicate import check_duplicate
from app.applications.executor import process_execution, queue_application
from app.applications.reconcile import reconcile_execution
from app.candidate.profile import save_profile
from app.jobs_repo import get_job, update_job
from app.models import ApplicationMode, ApplicationState, Job
from app.pipeline import ingest_and_process


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)


@pytest.fixture
def profile_saved(tmp_env, sample_profile):
    save_profile(sample_profile)
    return sample_profile


JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
    "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
)


def _mock_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer",
        company="Acme Corp",
        location="Remote - US",
        description=JD_TEXT + "H-1B sponsorship is available for this role.",
        employment_type="Full-time",
        provider="mock_ats",
        external_job_id="1001",
        url="https://mock-ats.local/jobs/1001",
        provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _scenario(job: Job, name: str) -> Job:
    job.provider_metadata = json.dumps({"mock_scenario": name})
    return job


# --- Scenario A: full happy path -> APPLIED ----------------------------------

def test_scenario_a_full_happy_path_to_applied(profile_saved):
    job = ingest_and_process(_mock_job())
    assert job.application_state == ApplicationState.READY_TO_APPLY
    assert job.resume_pdf_path and job.resume_docx_path

    result = queue_application(job.id, mode="AUTO_PERMITTED")
    assert result.queued
    execution = process_execution(result.execution_id)

    assert execution["status"] == "APPLIED"
    assert execution["confirmation_id"]
    assert execution["confirmation_url"]

    final_job = get_job(job.id)
    assert final_job.application_state == ApplicationState.APPLIED


# --- Scenario B: CONTRACT hard-blocked before executor -----------------------

def test_scenario_b_contract_job_hard_blocked(profile_saved):
    job = ingest_and_process(_mock_job(
        employment_type="Contract",
        description=JD_TEXT.replace("full-time", "") + "This is a contract position. H-1B sponsorship is available.",
    ))
    result = queue_application(job.id, mode="ASSIST")
    assert not result.queued
    assert applications_repo.get_active_execution_for_job(job.id) is None


# --- Scenario C: FULL_TIME + NO_SPONSORSHIP -> hard skip ---------------------

def test_scenario_c_no_sponsorship_hard_skip(profile_saved):
    job = ingest_and_process(_mock_job(description=JD_TEXT + "We are not able to sponsor visas at this time."))
    assert job.application_state == ApplicationState.SKIPPED_NO_SPONSORSHIP
    result = queue_application(job.id, mode="ASSIST")
    assert not result.queued


# --- Scenario D: FULL_TIME + LIKELY_SPONSOR -> review only, never submits ----

def test_scenario_d_likely_sponsor_never_auto_submits(profile_saved):
    job = ingest_and_process(_mock_job(description=JD_TEXT + "We may sponsor exceptional candidates."))
    assert job.application_state == ApplicationState.REVIEW_REQUIRED

    result = queue_application(job.id, mode="AUTO_PERMITTED")
    assert result.queued
    execution = process_execution(result.execution_id)

    assert execution["status"] not in ("SUBMITTED", "SUBMISSION_CONFIRMED", "APPLIED")
    assert execution["requires_user_action"]


# --- Scenario E: CAPTCHA -> NEEDS_USER_ACTION --------------------------------

def test_scenario_e_captcha_needs_user_action(profile_saved):
    job = ingest_and_process(_scenario(_mock_job(), "captcha"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)

    assert execution["status"] == "NEEDS_USER_ACTION"
    assert "CAPTCHA_PRESENT" in (execution.get("policy_reasons") or "")
    final_job = get_job(job.id)
    assert final_job.application_state == ApplicationState.NEEDS_USER_ACTION


# --- Scenario F: unknown legal question -> NEEDS_USER_ACTION -----------------

def test_scenario_f_unknown_legal_question_needs_user_action(profile_saved):
    job = ingest_and_process(_scenario(_mock_job(), "legal_unknown"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)

    assert execution["status"] == "NEEDS_USER_ACTION"
    assert "UNKNOWN_LEGAL_QUESTION" in (execution.get("policy_reasons") or "")


# --- Scenario G: duplicate application blocked -------------------------------

def test_scenario_g_duplicate_application_blocked(profile_saved):
    job1 = ingest_and_process(_mock_job(external_job_id="2001"))
    result1 = queue_application(job1.id, mode="AUTO_PERMITTED")
    process_execution(result1.execution_id)
    assert get_job(job1.id).application_state == ApplicationState.APPLIED

    # Simulate the same underlying posting (same company/title/location)
    # manually re-pasted as a separate job row under a different external id
    # -- the manual-ingest path (unlike the discovery-cycle path) does not
    # dedupe by fingerprint/canonical_url, so this is a realistic duplicate.
    job2 = ingest_and_process(_mock_job(external_job_id="2002"))
    dup = check_duplicate(get_job(job2.id))
    assert dup.is_duplicate

    result2 = queue_application(job2.id, mode="AUTO_PERMITTED")
    assert not result2.queued
    assert get_job(job2.id).application_state == ApplicationState.DUPLICATE_APPLICATION_BLOCKED


# --- Scenario H: timeout after submit -> SUBMISSION_STATUS_UNKNOWN, no retry -

def test_scenario_h_timeout_after_submit_no_blind_retry(profile_saved):
    job = ingest_and_process(_scenario(_mock_job(), "timeout_after_submit"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)

    assert execution["status"] == "SUBMISSION_STATUS_UNKNOWN"

    # Calling process_execution again must be a strict no-op -- never a
    # blind retry of the submission.
    execution2 = process_execution(result.execution_id)
    assert execution2["status"] == "SUBMISSION_STATUS_UNKNOWN"
    assert execution2["attempt_count"] == execution["attempt_count"]

    reconciled = reconcile_execution(result.execution_id, "confirmed_applied", confirmation_id="MANUAL-1")
    assert reconciled.ok
    assert get_job(job.id).application_state == ApplicationState.APPLIED


# --- Scenario I: resume artifact does not match job -> validation failure ----

def test_scenario_i_resume_job_mismatch_blocks(profile_saved):
    import shutil
    from pathlib import Path

    job = ingest_and_process(_mock_job())
    original = Path(job.resume_pdf_path)
    wrong_dir = original.parent.parent / "999999"
    wrong_dir.mkdir(parents=True, exist_ok=True)
    wrong_path = wrong_dir / "resume.pdf"
    shutil.copy(original, wrong_path)
    update_job(job.id, resume_pdf_path=str(wrong_path))

    result = queue_application(job.id, mode="ASSIST")
    execution = process_execution(result.execution_id)

    assert execution["status"] == "VALIDATION_REQUIRED"
    assert "job_id" in execution["user_action_reason"]


# --- Scenario J: ATS form schema changes -> FORM_SCHEMA_CHANGED, no submit --

def test_scenario_j_form_schema_drift_blocks_submission(profile_saved):
    job = ingest_and_process(_mock_job())  # scenario "simple"
    result = queue_application(job.id, mode="ASSIST")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_READY"  # baseline established, not yet submitted (ASSIST mode)

    # Simulate the SAME posting's application form structure changing on the
    # ATS side (extra required field appears) before this still-active
    # execution is retried.
    update_job(job.id, provider_metadata=json.dumps({"mock_scenario": "required_fields"}))
    execution2 = process_execution(result.execution_id)

    assert execution2["status"] == "NEEDS_USER_ACTION"
    assert execution2["user_action_reason"] == "FORM_SCHEMA_CHANGED"
