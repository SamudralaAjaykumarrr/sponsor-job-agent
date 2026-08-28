"""Canary Feasibility Gate V1 (feat/canary-feasibility-gate-v1) deterministic
test matrix. No real employer network, no real submission -- every job here
is inserted directly via app.jobs_repo, and any provider network touchpoint
(check_job_still_active/discover_form) is either exercised through a
provider that never makes a real call (an unrecognized/"manual" provider
falls back to GenericAssistOnlyProvider, whose hooks are pure no-network
stubs; mock_ats is a deterministic in-process fixture) or monkeypatched
directly on the provider singleton."""

from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.applications.canary_feasibility import FeasibilityVerdict, evaluate_canary_feasibility
from app.jobs_repo import insert_job, get_job
from app.models import ApplicationState, Job, SponsorshipStatus


BACKEND_DESCRIPTION = """
We are hiring a backend engineer to build and operate our core services.

Requirements:
- 3+ years of experience with Python
- Experience with FastAPI or Flask
- Experience building and consuming REST APIs
- Experience with PostgreSQL or another relational database
- Experience with Docker and Kubernetes
- Experience with AWS
- Experience with distributed systems and asynchronous/event-driven architectures
- Full-time, permanent position. H-1B sponsorship is available for this role.
"""


def _make_job(tmp_env, **overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer", company="Acme Corp", company_identifier="acme-corp",
        location="Remote - US", description=BACKEND_DESCRIPTION, provider="mock_ats",
        canonical_url="https://example.com/jobs/1", url="https://example.com/jobs/1",
        employment_type="full_time", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        technical_match_score=80.0, matched_skills="python,fastapi,postgresql,docker",
        gap_skills="", application_state=ApplicationState.ANALYZED,
    )
    defaults.update(overrides)
    job_id = insert_job(Job(**defaults))
    return get_job(job_id)


# --- A: strong match, valid sponsorship, FULL_TIME -> PASS ------------------

def test_strong_backend_job_confirmed_sponsor_full_time_passes(tmp_env):
    job = _make_job(tmp_env)
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.PASS, result.as_dict()


# --- B: explicit no sponsorship -> REJECT -----------------------------------

def test_no_sponsorship_rejects(tmp_env):
    job = _make_job(tmp_env, sponsorship_status=SponsorshipStatus.NO_SPONSORSHIP)
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.sponsorship.verdict == FeasibilityVerdict.REJECT


# --- C: sponsorship UNKNOWN -> REJECT (existing do-not-apply policy) -------

def test_sponsorship_unknown_rejects(tmp_env):
    job = _make_job(tmp_env, sponsorship_status=SponsorshipStatus.UNKNOWN)
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.sponsorship.verdict == FeasibilityVerdict.REJECT


# --- D: contract/C2C -> REJECT -----------------------------------------------

def test_c2c_employment_type_rejects(tmp_env):
    job = _make_job(tmp_env, employment_type="c2c")
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.employment_type.verdict == FeasibilityVerdict.REJECT


def test_contract_employment_type_rejects(tmp_env):
    job = _make_job(tmp_env, employment_type="contract")
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.employment_type.verdict == FeasibilityVerdict.REJECT


# --- E: large experience mismatch -> REJECT ---------------------------------

def test_large_experience_mismatch_rejects(tmp_env):
    job = _make_job(
        tmp_env, title="Staff Software Engineer",
        description=(
            "We are hiring a Staff Software Engineer to lead our backend platform team. "
            "Requires 10+ years of experience building large-scale distributed systems in Python. "
            "Full-time, permanent position. H-1B sponsorship is available for this role."
        ),
    )
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.experience.verdict == FeasibilityVerdict.REJECT


# --- F: unsupported core stack / role mismatch -> REJECT --------------------

def test_ios_engineer_role_mismatch_rejects(tmp_env):
    job = _make_job(
        tmp_env, title="iOS Engineer",
        description="Build our iOS app in Swift and Objective-C. Full-time, sponsorship available.",
    )
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.role_fit.verdict == FeasibilityVerdict.REJECT


def test_research_scientist_role_mismatch_rejects(tmp_env):
    job = _make_job(tmp_env, title="Research Scientist, Machine Learning")
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.role_fit.verdict == FeasibilityVerdict.REJECT


# --- G: truthful one-page resume infeasible -> REJECT -----------------------

def test_too_many_gap_skills_rejects_one_page_feasibility(tmp_env):
    job = _make_job(
        tmp_env, matched_skills="python",
        gap_skills="scala,rust,go,haskell,elixir,clojure,erlang,f#",
    )
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.one_page_resume.verdict == FeasibilityVerdict.REJECT


# --- H: long/unsupported essay requirements -> REJECT/REVIEW ---------------

def test_multiple_mandatory_essays_flagged(tmp_env, monkeypatch):
    from app.applications import canary_feasibility
    from app.applications.models import FormField, FormSnapshot

    class _StubProvider:
        def discover_form(self, job):
            return FormSnapshot(
                provider="manual", tenant_identifier="acme", external_job_id="1",
                fields=[
                    FormField(name="q1", label="Describe a time you led a project", field_type="textarea",
                              required=True),
                    FormField(name="q2", label="Why do you want to work here", field_type="textarea",
                              required=True),
                    FormField(name="q3", label="Describe your biggest technical achievement", field_type="textarea",
                              required=True),
                ],
            )

        def check_job_still_active(self, job):
            return None

        def classify_job_inactive_reason(self, job):
            return None

    monkeypatch.setattr(canary_feasibility, "get_application_provider", lambda job: _StubProvider())
    job = _make_job(tmp_env)
    result = evaluate_canary_feasibility(job)
    assert result.question_risk.verdict in (FeasibilityVerdict.REVIEW, FeasibilityVerdict.REJECT)
    assert result.verdict in (FeasibilityVerdict.REVIEW, FeasibilityVerdict.REJECT)


# --- I: known stable Greenhouse form -> positive provider/browser feasibility

def test_known_stable_greenhouse_form_is_positive(tmp_env, monkeypatch):
    from app.applications.provider_registry import _PROVIDERS
    from app.applications.models import FormField, FormSnapshot

    greenhouse = _PROVIDERS["greenhouse"]
    monkeypatch.setattr(greenhouse, "check_job_still_active", lambda job: True)
    monkeypatch.setattr(greenhouse, "classify_job_inactive_reason", lambda job: None)
    monkeypatch.setattr(
        greenhouse, "discover_form",
        lambda job: FormSnapshot(
            provider="greenhouse", tenant_identifier="acme", external_job_id="1",
            fields=[
                FormField(name="full_name", label="Full Name", field_type="input_text", required=True),
                FormField(name="email", label="Email", field_type="input_text", required=True),
                FormField(name="resume", label="Resume/CV", field_type="input_file", required=True),
            ],
        ),
    )
    job = _make_job(tmp_env, provider="greenhouse", external_job_id="1")
    result = evaluate_canary_feasibility(job)
    assert result.posting_health.verdict == FeasibilityVerdict.PASS
    assert result.question_risk.verdict == FeasibilityVerdict.PASS
    assert result.provider_browser_feasibility.verdict == FeasibilityVerdict.PASS


# --- provider_browser_feasibility: never a hard REJECT from health alone,
#     and a STALE health observation (outside the cooldown) is informational
#     only -- matches CLAUDE.md's "provider_health never auto-disables" rule.

def test_fresh_captcha_blocked_health_is_review_not_reject(tmp_env):
    from app.applications import provider_health

    job = _make_job(tmp_env, provider="greenhouse", external_job_id="1")
    provider_health.record_failure("greenhouse", provider_health.FailureKind.CAPTCHA)
    result = evaluate_canary_feasibility(job)
    assert result.provider_browser_feasibility.verdict == FeasibilityVerdict.REVIEW
    assert result.provider_browser_feasibility.verdict != FeasibilityVerdict.REJECT


def test_stale_captcha_blocked_health_does_not_block_unrelated_job(tmp_env):
    from app.db import db_session

    job = _make_job(tmp_env, provider="greenhouse", external_job_id="1")
    stale = (datetime.now(timezone.utc) - timedelta(hours=config.CANARY_RECENT_FAILURE_COOLDOWN_HOURS + 1)).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO application_provider_health (provider, tenant, site, captcha_observed, last_failure, "
            "created_at, updated_at) VALUES (?, '', '', 1, ?, ?, ?)",
            ("greenhouse", stale, stale, stale),
        )
    result = evaluate_canary_feasibility(job)
    assert result.provider_browser_feasibility.verdict == FeasibilityVerdict.PASS


# --- J: recent repeated live failure/cooldown -> penalized/excluded --------

def test_recent_own_session_failure_excludes_job(tmp_env):
    from app.db import db_session

    job = _make_job(tmp_env)
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO browser_assist_sessions (session_id, execution_id, job_id, provider, application_url, "
            "status, active, current_step, created_at, updated_at, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
            ("bsess_test1", "exec_test1", job.id, job.provider, job.canonical_url,
             "PAUSED_UNSUPPORTED_SUBMISSION", now, now, now),
        )
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.recent_failure_penalty.verdict == FeasibilityVerdict.REJECT


def test_old_session_failure_outside_cooldown_does_not_exclude(tmp_env):
    from app.db import db_session

    job = _make_job(tmp_env)
    old = (datetime.now(timezone.utc) - timedelta(hours=config.CANARY_RECENT_FAILURE_COOLDOWN_HOURS + 1)).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO browser_assist_sessions (session_id, execution_id, job_id, provider, application_url, "
            "status, active, current_step, created_at, updated_at, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
            ("bsess_test2", "exec_test2", job.id, job.provider, job.canonical_url,
             "PAUSED_UNSUPPORTED_SUBMISSION", old, old, old),
        )
    result = evaluate_canary_feasibility(job)
    assert result.recent_failure_penalty.verdict == FeasibilityVerdict.PASS


def test_sibling_job_same_employer_provider_recent_failure_excludes(tmp_env):
    from app.db import db_session

    failed_job = _make_job(tmp_env, company_identifier="airbnb", provider="greenhouse", external_job_id="8146265")
    candidate_job = _make_job(tmp_env, company_identifier="airbnb", provider="greenhouse", external_job_id="9999999")
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO browser_assist_sessions (session_id, execution_id, job_id, provider, application_url, "
            "status, active, current_step, created_at, updated_at, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
            ("bsess_test3", "exec_test3", failed_job.id, "greenhouse", failed_job.canonical_url,
             "PAUSED_UNSUPPORTED_SUBMISSION", now, now, now),
        )
    result = evaluate_canary_feasibility(candidate_job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.recent_failure_penalty.verdict == FeasibilityVerdict.REJECT


# --- K: a job matching Airbnb job 327's own recorded failure state is never
#        selected -- the general mechanism the live selection step (Part 8)
#        relies on to exclude job 327 specifically, proven here without
#        touching the real job 327 row. ---------------------------------

def test_job_with_airbnb_327s_exact_failure_signature_is_excluded(tmp_env):
    from app.db import db_session

    job = _make_job(
        tmp_env, title="Software Engineer, Payments", company="Airbnb", company_identifier="airbnb",
        provider="greenhouse", external_job_id="8146265",
        canonical_url="https://careers.airbnb.com/positions/8146265?gh_jid=8146265",
        sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR,
    )
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO browser_assist_sessions (session_id, execution_id, job_id, provider, application_url, "
            "status, active, current_step, created_at, updated_at, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
            ("bsess_327like", "exec_327like", job.id, "greenhouse", job.canonical_url,
             "PAUSED_UNSUPPORTED_SUBMISSION", now, now, now),
        )
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.REJECT
    assert result.recent_failure_penalty.verdict == FeasibilityVerdict.REJECT
    assert "recently ended" in result.recent_failure_penalty.reason
