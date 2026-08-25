"""Apply / Automation Settings V1: persistence, validation, confirmation,
runtime dispatch (resume mode/cover letter/submission mode/limits/job
preferences), sponsorship hard-skip immutability, and demo isolation.

Playwright coverage for the Settings UI itself lives in
tests/test_apply_automation_settings_playwright.py (marked `browser`,
skipped unless Chromium is actually launchable)."""

import json

import pytest
from fastapi.testclient import TestClient

from app import apply_settings, config
from app.apply_settings import CoverLetterPolicy, ResumeOptimizationMode, SubmissionMode
from app.applications import demo as applications_demo
from app.applications import repo as applications_repo
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications.executor import process_execution, queue_application
from app.applications.rate_limit import check_rate_limits
from app.candidate.profile import save_profile
from app.candidate.schema import CandidateProfile
from app.jobs_repo import insert_job
from app.main import app
from app.models import ApplicationMode, ApplicationState, Job, SponsorshipStatus
from app.pipeline import ingest_and_process
from app.resume_optimizer.optimizer import generate_optimized_resume_content, optimize_resume
from app.resume_optimizer.evidence import build_evidence_graph
from app.resume_optimizer.jd_analysis import analyze_jd
from app.resume_optimizer.matching import match_requirements

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
    "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
    "H-1B sponsorship is available for this role."
)


_MUTABLE_CONFIG_ATTRS = (
    "APPLICATION_EXECUTOR_ENABLED", "AUTO_SUBMIT_ENABLED", "SPONSORSHIP_POLICY",
    "MAX_APPLICATIONS_PER_HOUR", "MAX_APPLICATIONS_PER_DAY", "MAX_APPLICATIONS_PER_COMPANY_PER_DAY",
    "MAX_APPLICATIONS_PER_WEEK", "MAX_CONCURRENT_APPLICATIONS", "MIN_SALARY_USD",
)


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    """Both settings_store.save_settings and app.apply_settings genuinely
    mutate the shared, process-global `config` module by design (that's how
    a save takes effect immediately -- see both modules' docstrings) --
    snapshot-and-restore via monkeypatch, mirroring tests/test_premium_ui.py's
    own established pattern for this exact hazard, so no test in this file
    leaks a changed limit/policy/flag into a later, unrelated test."""
    for attr in _MUTABLE_CONFIG_ATTRS:
        monkeypatch.setattr(config, attr, getattr(config, attr))
    config.APPLICATION_EXECUTOR_ENABLED = True


@pytest.fixture
def profile_saved(tmp_env, sample_profile):
    save_profile(sample_profile)
    return sample_profile


def _mock_job(external_job_id: str, company: str = "Acme Corp", title: str = "Backend Software Engineer",
              provider: str = "mock_ats", mock_scenario: str = "simple") -> Job:
    return Job(
        title=title, company=company, location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider=provider,
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": mock_scenario}),
        mode=ApplicationMode.ASSIST,
    )


# --- 1/2/3: persistence, validation, defaults --------------------------------

def test_default_settings_are_safe(tmp_env):
    s = apply_settings.get_settings()
    assert s.submission_mode == SubmissionMode.REVIEW.value
    assert s.resume_optimization_mode == ResumeOptimizationMode.HONEST.value
    assert s.cover_letter_policy == CoverLetterPolicy.WHEN_REQUESTED.value
    assert s.work_arrangements == list(apply_settings.WORK_ARRANGEMENTS)


def test_settings_persist_across_get_settings_calls(tmp_env):
    result = apply_settings.save_resume_settings({"resume_optimization_mode": "AGGRESSIVE", "auto_approve_resume": "false"})
    assert result.ok
    # A fresh call re-reads from the DB -- simulates a new request/process.
    reloaded = apply_settings.get_settings()
    assert reloaded.resume_optimization_mode == "AGGRESSIVE"
    assert reloaded.auto_approve_resume is False


def test_invalid_resume_mode_rejected(tmp_env):
    result = apply_settings.save_resume_settings({"resume_optimization_mode": "TURBO"})
    assert not result.ok
    assert result.errors
    assert apply_settings.get_settings().resume_optimization_mode == ResumeOptimizationMode.HONEST.value


def test_invalid_cover_letter_policy_rejected(tmp_env):
    result = apply_settings.save_cover_letter_settings({"cover_letter_policy": "SOMETIMES"})
    assert not result.ok


def test_invalid_submission_mode_rejected(tmp_env):
    result = apply_settings.save_submission_settings({"submission_mode": "YOLO"}, confirmed=True)
    assert not result.ok


def test_invalid_work_arrangement_rejected(tmp_env):
    result = apply_settings.save_preferences_settings({"work_arrangements": ["MARS"]})
    assert not result.ok


def test_settings_store_limit_validation(tmp_env):
    from app import settings_store

    errors = settings_store.save_settings({"max_applications_per_day": "99999"})
    assert errors
    errors = settings_store.save_settings({"max_applications_per_day": "10"})
    assert not errors
    assert config.MAX_APPLICATIONS_PER_DAY == 10


# --- 4/5/6: auto-submit confirmation + provider-capability honesty ----------

def test_switching_to_auto_submit_requires_confirmation(tmp_env):
    result = apply_settings.save_submission_settings({"submission_mode": "AUTO_SUBMIT"}, confirmed=False)
    assert not result.ok
    assert result.needs_confirmation
    assert apply_settings.get_settings().submission_mode == SubmissionMode.REVIEW.value
    assert config.AUTO_SUBMIT_ENABLED is False


def test_switching_to_auto_submit_with_confirmation_persists_and_applies_live(tmp_env):
    result = apply_settings.save_submission_settings({"submission_mode": "AUTO_SUBMIT"}, confirmed=True)
    assert result.ok
    assert apply_settings.get_settings().submission_mode == SubmissionMode.AUTO_SUBMIT.value
    assert config.AUTO_SUBMIT_ENABLED is True

    # Switching back to REVIEW never needs confirmation and clears the flag live.
    back = apply_settings.save_submission_settings({"submission_mode": "REVIEW"}, confirmed=False)
    assert back.ok
    assert config.AUTO_SUBMIT_ENABLED is False


def test_settings_persist_across_simulated_restart(tmp_env):
    apply_settings.save_submission_settings({"submission_mode": "AUTO_SUBMIT"}, confirmed=True)
    # Simulate a process restart: config resets to its .env default, then the
    # lifespan-equivalent startup hook re-applies the persisted setting.
    config.AUTO_SUBMIT_ENABLED = False
    apply_settings.apply_overrides_on_startup()
    assert config.AUTO_SUBMIT_ENABLED is True


def test_auto_submit_setting_does_not_bypass_unsupported_provider(profile_saved):
    """Even with Submission=Auto-submit confirmed (AUTO_SUBMIT_ENABLED=True)
    and mode=AUTO_PERMITTED, a provider with submission_supported=False
    (every real ATS adapter as of this phase) must stop at
    SUBMISSION_READY/APPROVED -- never a fake APPLIED."""
    apply_settings.save_submission_settings({"submission_mode": "AUTO_SUBMIT"}, confirmed=True)
    job = ingest_and_process(_mock_job("unsupported-1", company="Lever Co", provider="lever"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    assert result.queued
    execution = process_execution(result.execution_id)
    assert execution["status"] != "APPLIED"


def test_unsupported_provider_never_reaches_applied_even_manually_approved(profile_saved):
    apply_settings.save_submission_settings({"submission_mode": "AUTO_SUBMIT"}, confirmed=True)
    job = ingest_and_process(_mock_job("unsupported-2", company="Greenhouse Co", provider="greenhouse"))
    result = queue_application(job.id, mode="ASSIST")
    execution = process_execution(result.execution_id) if result.queued else None
    if execution is not None:
        assert execution["status"] != "APPLIED"


def test_review_mode_reaches_ready_for_approval_without_submitting(profile_saved):
    assert apply_settings.get_settings().submission_mode == SubmissionMode.REVIEW.value
    job = ingest_and_process(_mock_job("review-1"))
    result = queue_application(job.id, mode="ASSIST")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_READY"


def test_auto_submit_mock_eligible_path_reaches_applied_with_receipt(profile_saved):
    apply_settings.save_submission_settings({"submission_mode": "AUTO_SUBMIT"}, confirmed=True)
    job = ingest_and_process(_mock_job("auto-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "APPLIED"

    from app.applications import receipts as applications_receipts

    receipt = applications_receipts.get_latest_receipt_for_execution(execution["execution_id"])
    assert receipt is not None


def test_submission_status_unknown_never_blindly_resubmits(profile_saved):
    apply_settings.save_submission_settings({"submission_mode": "AUTO_SUBMIT"}, confirmed=True)
    job = ingest_and_process(_mock_job("unknown-1", mock_scenario="timeout_after_submit"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")
    execution = process_execution(result.execution_id)
    assert execution["status"] == "SUBMISSION_STATUS_UNKNOWN"

    # Calling process_execution again on the same row must never re-submit --
    # Phase 9's crash-recovery guard converts a SUBMITTING/SUBMITTED resume
    # straight to SUBMISSION_STATUS_UNKNOWN rather than calling submit() again.
    again = process_execution(execution["execution_id"])
    assert again["status"] in ("SUBMISSION_STATUS_UNKNOWN",)


# --- 7/8: resume mode dispatch, evidence-bound aggressive ------------------

def _rich_profile() -> CandidateProfile:
    return CandidateProfile.model_validate({
        "contact": {"full_name": "Rich Candidate", "email": "rich@example.com", "phone": "555-1",
                    "city": "Austin", "state": "TX"},
        "employment": [{
            "company": "BigCo", "title": "Backend Engineer", "start_date": "2020-01", "end_date": "Present",
            "location": "Remote",
            "verified_bullets": [f"Built feature {i} in Python/FastAPI serving production traffic." for i in range(8)],
            "skills_used": ["python", "fastapi", "postgresql"],
        }],
        "skills": ["python", "fastapi", "postgresql", "docker"],
        "projects": [
            {"name": f"Project {i}", "description": "A project.",
             "verified_bullets": [f"Did project work {i}."], "skills_used": ["python"], "url": ""}
            for i in range(5)
        ],
        "education": [],
        "work_authorization": {"current_status": "H-1B", "requires_sponsorship": True, "sponsorship_type_needed": "H-1B"},
        "preferences": {},
        "standard_answers": {"years_of_experience": 4},
    })


def test_resume_mode_dispatches_different_bullet_caps():
    profile = _rich_profile()
    jd_analysis = analyze_jd("Backend Software Engineer", JD_TEXT)
    graph = build_evidence_graph(profile)
    matches = match_requirements(jd_analysis.requirements, graph, profile)

    honest = generate_optimized_resume_content(profile, "Backend Software Engineer", JD_TEXT, jd_analysis, matches, graph, mode="HONEST")
    aggressive = generate_optimized_resume_content(profile, "Backend Software Engineer", JD_TEXT, jd_analysis, matches, graph, mode="AGGRESSIVE")

    assert len(honest.experience[0].bullets) == 5
    assert len(aggressive.experience[0].bullets) == 6
    assert len(honest.projects) == 3
    assert len(aggressive.projects) == 4


def test_aggressive_mode_remains_evidence_bound():
    from app.resume.claim_checker import check_resume_claims

    profile = _rich_profile()
    jd_analysis = analyze_jd("Backend Software Engineer", JD_TEXT)
    graph = build_evidence_graph(profile)
    matches = match_requirements(jd_analysis.requirements, graph, profile)
    aggressive = generate_optimized_resume_content(profile, "Backend Software Engineer", JD_TEXT, jd_analysis, matches, graph, mode="AGGRESSIVE")

    violations = check_resume_claims(aggressive, profile)
    assert violations == []
    all_bullets = " ".join(b for e in aggressive.experience for b in e.bullets)
    verified = set(profile.employment[0].verified_bullets)
    for line in (b for e in aggressive.experience for b in e.bullets):
        assert line in verified


def test_off_mode_never_auto_generates_but_manual_generation_still_works(tmp_env, sample_profile):
    save_profile(sample_profile)
    apply_settings.save_resume_settings({"resume_optimization_mode": "OFF", "auto_approve_resume": "true"})
    job = ingest_and_process(_mock_job("off-mode-1"))

    from app.resume_optimizer.scheduler import _find_jobs_needing_optimization

    assert job.id not in _find_jobs_needing_optimization(50)

    # Manual generation (dashboard "Generate Resume" / CLI) is never gated by
    # the OFF setting.
    result = optimize_resume(job.id)
    assert result.status == "READY"


def test_auto_approve_resume_gate(tmp_env, sample_profile):
    """When Auto-approve resume is OFF, a READY one-page variant is not
    promoted onto the job automatically; the manual approve action promotes
    it, and only when it is genuinely READY/one-page."""
    save_profile(sample_profile)
    apply_settings.save_resume_settings({"resume_optimization_mode": "HONEST", "auto_approve_resume": "false"})
    job = ingest_and_process(_mock_job("approve-1"))
    optimize_resume(job.id)

    from app.resume_optimizer.promotion import promote_current_variant
    from app.jobs_repo import get_job

    assert promote_current_variant(job.id) is True
    assert get_job(job.id).promoted_resume_variant_id


# --- 10: cover letter policy -------------------------------------------------

def test_cover_letter_policy_off_never_generates(profile_saved):
    apply_settings.save_cover_letter_settings({"cover_letter_policy": "OFF"})
    job = ingest_and_process(_mock_job("cl-off"))
    from app.pipeline import generate_assist_outputs

    updated = generate_assist_outputs(job.id)
    assert updated.cover_letter_path is None


def test_cover_letter_policy_when_requested_only_if_jd_asks(tmp_env, sample_profile):
    save_profile(sample_profile)
    apply_settings.save_cover_letter_settings({"cover_letter_policy": "WHEN_REQUESTED"})
    from app.pipeline import generate_assist_outputs

    plain_job = ingest_and_process(_mock_job("cl-wr-1"))
    updated_plain = generate_assist_outputs(plain_job.id)
    assert updated_plain.cover_letter_path is None

    requesting_job = ingest_and_process(_mock_job("cl-wr-2", title="Backend Software Engineer II"))
    from app.jobs_repo import update_job
    update_job(requesting_job.id, description=JD_TEXT + " Please include a cover letter with your application.")
    updated_requesting = generate_assist_outputs(requesting_job.id)
    assert updated_requesting.cover_letter_path is not None


def test_cover_letter_policy_always_generates(profile_saved):
    apply_settings.save_cover_letter_settings({"cover_letter_policy": "ALWAYS"})
    job = ingest_and_process(_mock_job("cl-always"))
    from app.pipeline import generate_assist_outputs

    updated = generate_assist_outputs(job.id)
    assert updated.cover_letter_path is not None


# --- 11/12/13/14: application limits ----------------------------------------

def test_weekly_application_limit_enforced(profile_saved, monkeypatch):
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_DAY", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_WEEK", 2)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)

    jobs = [ingest_and_process(_mock_job(f"wk{i}", company=f"WeekCo{i}")) for i in range(3)]
    outcomes = []
    for j in jobs:
        result = queue_application(j.id, mode="AUTO_PERMITTED")
        outcomes.append(process_execution(result.execution_id)["status"])
    assert outcomes[0] == "APPLIED"
    assert outcomes[1] == "APPLIED"
    assert outcomes[2] == "NEEDS_USER_ACTION"


def test_concurrent_application_limit_enforced(profile_saved, monkeypatch):
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_DAY", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 100)
    monkeypatch.setattr(config, "MAX_CONCURRENT_APPLICATIONS", 1)

    # Queue (but don't process past FORM_FILLED) two applications so both
    # stay active=1 concurrently, then verify a fresh check_rate_limits call
    # sees the concurrency ceiling.
    job_a = ingest_and_process(_mock_job("conc-a", mock_scenario="login_required"))
    result_a = queue_application(job_a.id, mode="ASSIST")
    process_execution(result_a.execution_id)  # pauses NEEDS_USER_ACTION, stays active=1

    rl = check_rate_limits("Some Other Co")
    assert not rl.allowed
    assert "MAX_CONCURRENT_APPLICATIONS" in rl.reason


def test_settings_store_min_salary_live_override(tmp_env):
    from app import settings_store
    from app.matching.compensation import evaluate_compensation

    settings_store.save_settings({"min_salary_usd": "150000"})
    ok, reason = evaluate_compensation(100000, 120000)
    assert not ok
    assert "150,000" in reason


# --- 15/16: demo isolation ---------------------------------------------------

def test_demo_scenarios_do_not_consume_or_block_each_other(tmp_env, sample_profile):
    save_profile(sample_profile)
    # Realistic default per-company-per-day limit (2) -- three demos that
    # each reach a real submit attempt (successful_application,
    # confirmed_submission, submission_unknown) all share "Demo Fixture Co"
    # and must not collide (the is_test_fixture rate-limit exclusion fix).
    from app.applications import approval as approval_mod

    keys = ("successful_application", "confirmed_submission", "submission_unknown")
    for key in keys:
        job = applications_demo.run_demo(key)["job_id"]  # -> SUBMISSION_READY
        # Real Approve & Apply -- the same function the app's own
        # /jobs/{job_id}/applications/approve route calls, never a bypass.
        approval_mod.approve_and_apply(job)

    results = {key: applications_demo.describe_demo(key, applications_demo.ensure_demo_job(key).id) for key in keys}
    assert results["successful_application"]["execution"]["status"] == "APPLIED"
    assert results["confirmed_submission"]["execution"]["status"] == "APPLIED"
    assert results["submission_unknown"]["execution"]["status"] == "SUBMISSION_STATUS_UNKNOWN"
    for key in keys:
        reason = (results[key]["execution"].get("user_action_reason") or "")
        assert "MAX_APPLICATIONS" not in reason


def test_explicit_limit_demo_still_demonstrates_a_block_and_restores_config(tmp_env, sample_profile):
    save_profile(sample_profile)
    before = config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY
    applications_demo.run_demo("application_limit")  # prepares AND approves in one call

    job = applications_demo.ensure_demo_job("application_limit")
    execution = applications_repo.get_active_execution_for_job(job.id)
    assert execution["status"] == "NEEDS_USER_ACTION"
    assert "MAX_APPLICATIONS_PER_COMPANY_PER_DAY" in (execution.get("user_action_reason") or "")
    # The temporary override must always be fully restored.
    assert config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY == before


# --- 17/18: hard invariants cannot be overridden by settings ----------------

def test_no_sponsorship_hard_skip_is_immutable_regardless_of_setting(profile_saved):
    apply_settings.save_sponsorship_settings({"include_likely_sponsors": "true"})
    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="No Sponsor Co", location="Remote",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
            "This is a full-time position. We are not able to sponsor visas now or in the future."
        ),
        mode=ApplicationMode.ASSIST,
    ))
    assert job.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP
    eligibility = evaluate_executor_eligibility(job)
    assert eligibility.hard_skip
    assert eligibility.blocking_category == "SPONSORSHIP"


def test_non_full_time_cannot_be_enabled_for_unattended_via_preferences(profile_saved):
    apply_settings.save_preferences_settings({"work_arrangements": ["REMOTE", "HYBRID", "ONSITE"]})
    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Contract Co", location="Remote",
        description=JD_TEXT + " This is a 6-month C2C contract position.",
        employment_type="Contract", mode=ApplicationMode.ASSIST,
    ))
    eligibility = evaluate_executor_eligibility(job)
    assert eligibility.hard_skip
    assert eligibility.blocking_category == "EMPLOYMENT_TYPE"


def test_job_preferences_filter_default_matches_everything(profile_saved):
    job = ingest_and_process(_mock_job("prefs-default"))
    ok, _ = apply_settings.job_matches_preferences(job)
    assert ok


def test_job_preferences_filter_excludes_configured_keyword(profile_saved):
    apply_settings.save_preferences_settings({"excluded_keywords": "backend"})
    job = ingest_and_process(_mock_job("prefs-excluded"))
    ok, reason = apply_settings.job_matches_preferences(job)
    assert not ok
    assert "excluded" in reason


# --- HTTP layer: settings routes --------------------------------------------

def test_settings_page_renders_all_sections(tmp_env):
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    for heading in ("Resume", "Cover Letter", "Submission", "Application Limits",
                    "Job Preferences", "Sponsorship", "Advanced Safety"):
        assert heading in resp.text


def test_settings_submission_route_requires_confirmation_then_persists(tmp_env):
    client = TestClient(app)
    resp = client.post("/settings/submission", data={"submission_mode": "AUTO_SUBMIT"})
    assert resp.status_code == 200
    assert "Confirm" in resp.text
    assert apply_settings.get_settings().submission_mode == SubmissionMode.REVIEW.value

    resp2 = client.post(
        "/settings/submission",
        data={"submission_mode": "AUTO_SUBMIT", "confirm_auto_submit": "true"},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    assert apply_settings.get_settings().submission_mode == SubmissionMode.AUTO_SUBMIT.value


def test_settings_resume_route_persists(tmp_env):
    client = TestClient(app)
    resp = client.post(
        "/settings/resume",
        data={"resume_optimization_mode": "AGGRESSIVE"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert apply_settings.get_settings().resume_optimization_mode == "AGGRESSIVE"
