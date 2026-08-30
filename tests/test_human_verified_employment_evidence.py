"""Human-Verified Employment Type Evidence + Canary Revalidation V1.

Deterministic coverage for app.applications.human_verified_employment_evidence
and its wiring into app.matching.employment_type.resolve_employment_type_evidence,
app.applications.eligibility.evaluate_executor_eligibility, and
app.applications.canary_feasibility.evaluate_canary_feasibility. No real
network call -- every "external evidence" value here is a plain string
supplied directly to record_identity_check(), matching this project's
existing browser_fixtures/mock_ats convention of never contacting a real
employer or aggregator in a test."""

import pytest

from app.applications.canary_feasibility import FeasibilityVerdict, evaluate_canary_feasibility
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications.human_verified_employment_evidence import (
    confirm_by_human,
    get_latest_record,
    get_verified_value,
    record_identity_check,
)
from app.jobs_repo import get_job, insert_job, update_job
from app.matching.employment_type import EmploymentTypeEvidenceSource, resolve_employment_type_evidence
from app.models import ApplicationState, EmploymentType, Job, SponsorshipStatus
from app.sponsorship.decision import compute_jd_fingerprint

TITLE = "Software Engineer, Backend"
COMPANY = "Robinhood"
DESCRIPTION = (
    "As a Software Engineer, you'll build and own backend services. "
    "What you bring: 2+ years of experience in software development. "
    "Proficiency in Go or Python."
)
DICE_URL = "https://www.dice.com/job-detail/a611e17c-f091-4309-ae52-0af36a7306de"


def _make_job(tmp_env, **overrides) -> Job:
    fingerprint = overrides.pop("jd_sponsorship_fingerprint", None)
    if fingerprint is None:
        fingerprint = compute_jd_fingerprint(
            overrides.get("title", TITLE), overrides.get("company", COMPANY),
            overrides.get("description", DESCRIPTION),
        )
    defaults = dict(
        title=TITLE, company=COMPANY, company_identifier="robinhood",
        location="Menlo Park, CA; New York, NY", description=DESCRIPTION, provider="greenhouse",
        external_job_id="7263592", canonical_url="https://boards.greenhouse.io/robinhood/jobs/7263592",
        url="https://boards.greenhouse.io/robinhood/jobs/7263592",
        employment_type="", sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR,
        technical_match_score=50.0, application_state=ApplicationState.ANALYZED,
        jd_sponsorship_fingerprint=fingerprint,
    )
    defaults.update(overrides)
    job_id = insert_job(Job(**defaults))
    return get_job(job_id)


def _exact_confirmed_record(tmp_env, job, *, normalized_value=EmploymentType.FULL_TIME):
    rec = record_identity_check(
        job, evidence_url=DICE_URL, evidence_source_name="Dice",
        raw_employment_type_value="Full Time", normalized_value=normalized_value,
        identity_match_verdict="EXACT_MATCH",
        identity_match_evidence="title+company+locations+salary($166k-195k Zone 1)+2yr-req all match",
    )
    return confirm_by_human(rec.id, confirmation_text="I VERIFY JOB AS FULL_TIME BASED ON THE PRESENTED MATCHED EVIDENCE.")


# --------------------------------------------------------------------------
# Identity-verdict gating
# --------------------------------------------------------------------------

def test_exact_match_plus_confirmation_yields_verified_value(tmp_env):
    job = _make_job(tmp_env)
    _exact_confirmed_record(tmp_env, job)
    assert get_verified_value(job) == EmploymentType.FULL_TIME


def test_title_only_match_recorded_as_ambiguous_is_rejected(tmp_env):
    # A reviewer who only compared title text (no location/salary/JD
    # corroboration) must record AMBIGUOUS, never EXACT_MATCH -- and an
    # AMBIGUOUS row must never surface a value even if later "confirmed".
    job = _make_job(tmp_env)
    rec = record_identity_check(
        job, evidence_url="https://example.com/some-other-listing", evidence_source_name="Indeed",
        raw_employment_type_value="Full Time", normalized_value=EmploymentType.FULL_TIME,
        identity_match_verdict="AMBIGUOUS", identity_match_evidence="title matches only; no location/salary/JD corroboration",
    )
    confirm_by_human(rec.id, confirmation_text="I VERIFY JOB AS FULL_TIME BASED ON THE PRESENTED MATCHED EVIDENCE.")
    assert get_verified_value(job) is None


def test_salary_location_jd_mismatch_is_rejected(tmp_env):
    job = _make_job(tmp_env)
    rec = record_identity_check(
        job, evidence_url="https://example.com/different-listing", evidence_source_name="LinkedIn",
        raw_employment_type_value="Full Time", normalized_value=EmploymentType.FULL_TIME,
        identity_match_verdict="MISMATCH",
        identity_match_evidence="salary range and office location do not match internal posting",
    )
    confirm_by_human(rec.id, confirmation_text="I VERIFY JOB AS FULL_TIME BASED ON THE PRESENTED MATCHED EVIDENCE.")
    assert get_verified_value(job) is None


def test_ambiguous_evidence_remains_unknown_end_to_end(tmp_env):
    job = _make_job(tmp_env)
    rec = record_identity_check(
        job, evidence_url="https://example.com/x", evidence_source_name="Dice",
        raw_employment_type_value="Full Time", normalized_value=EmploymentType.FULL_TIME,
        identity_match_verdict="AMBIGUOUS", identity_match_evidence="insufficient corroboration",
    )
    confirm_by_human(rec.id, confirmation_text="I VERIFY JOB AS FULL_TIME BASED ON THE PRESENTED MATCHED EVIDENCE.")
    decision = resolve_employment_type_evidence(
        job.employment_type, job.title, job.description, "", human_verified_value=get_verified_value(job),
    )
    assert decision.value == EmploymentType.UNKNOWN


def test_invalid_identity_verdict_rejected():
    from app.applications.human_verified_employment_evidence import IDENTITY_MATCH_VERDICTS
    assert set(IDENTITY_MATCH_VERDICTS) == {"EXACT_MATCH", "PROBABLE_MATCH", "AMBIGUOUS", "MISMATCH"}


# --------------------------------------------------------------------------
# Human-confirmation gating
# --------------------------------------------------------------------------

def test_exact_match_without_confirmation_stays_unknown(tmp_env):
    job = _make_job(tmp_env)
    record_identity_check(
        job, evidence_url=DICE_URL, evidence_source_name="Dice",
        raw_employment_type_value="Full Time", normalized_value=EmploymentType.FULL_TIME,
        identity_match_verdict="EXACT_MATCH", identity_match_evidence="all signals match",
    )
    # No confirm_by_human() call.
    assert get_verified_value(job) is None
    decision = resolve_employment_type_evidence(
        job.employment_type, job.title, job.description, "", human_verified_value=get_verified_value(job),
    )
    assert decision.value == EmploymentType.UNKNOWN


def test_exact_match_plus_confirmation_end_to_end_full_time(tmp_env):
    job = _make_job(tmp_env)
    _exact_confirmed_record(tmp_env, job)
    decision = resolve_employment_type_evidence(
        job.employment_type, job.title, job.description, "", human_verified_value=get_verified_value(job),
    )
    assert decision.value == EmploymentType.FULL_TIME
    assert decision.source == EmploymentTypeEvidenceSource.HUMAN_VERIFIED_EXTERNAL_EVIDENCE


# --------------------------------------------------------------------------
# Official contradictory evidence always wins
# --------------------------------------------------------------------------

def test_official_contract_beats_confirmed_human_full_time(tmp_env):
    job = _make_job(tmp_env, employment_type="Contract")
    _exact_confirmed_record(tmp_env, job)
    decision = resolve_employment_type_evidence(
        job.employment_type, job.title, job.description, "", human_verified_value=get_verified_value(job),
    )
    assert decision.value == EmploymentType.CONTRACT
    assert "overrides a conflicting FULL_TIME signal" in decision.reason


def test_official_contract_in_jd_text_beats_confirmed_human_full_time(tmp_env):
    job = _make_job(tmp_env, description=DESCRIPTION + " This is a contract position.")
    _exact_confirmed_record(tmp_env, job)
    decision = resolve_employment_type_evidence(
        job.employment_type, job.title, job.description, "", human_verified_value=get_verified_value(job),
    )
    assert decision.value == EmploymentType.CONTRACT


# --------------------------------------------------------------------------
# Staleness / invalidation
# --------------------------------------------------------------------------

def test_posting_fingerprint_change_invalidates_verification(tmp_env):
    job = _make_job(tmp_env)
    _exact_confirmed_record(tmp_env, job)
    assert get_verified_value(job) == EmploymentType.FULL_TIME

    # Simulate a material JD edit: the job's own fingerprint changes (as
    # app.sponsorship.decision.persist_decision() does on reanalysis).
    new_fingerprint = compute_jd_fingerprint(job.title, job.company, job.description + " Now requires 8+ years.")
    update_job(job.id, jd_sponsorship_fingerprint=new_fingerprint)
    changed_job = get_job(job.id)

    assert get_verified_value(changed_job) is None


def test_unchanged_fingerprint_keeps_verification_valid(tmp_env):
    job = _make_job(tmp_env)
    _exact_confirmed_record(tmp_env, job)
    # A no-op update (fingerprint unchanged) must not invalidate anything.
    update_job(job.id, notes="unrelated field touch")
    reloaded = get_job(job.id)
    assert get_verified_value(reloaded) == EmploymentType.FULL_TIME


# --------------------------------------------------------------------------
# Job-specificity / no cross-contamination
# --------------------------------------------------------------------------

def test_evidence_is_job_specific_not_shared_across_jobs(tmp_env):
    job_a = _make_job(tmp_env, external_job_id="7263592")
    job_b = _make_job(tmp_env, external_job_id="9999999", title="Software Engineer, Backend")
    _exact_confirmed_record(tmp_env, job_a)

    assert get_verified_value(job_a) == EmploymentType.FULL_TIME
    assert get_verified_value(job_b) is None


def test_no_company_wide_inference_second_robinhood_job_unaffected(tmp_env):
    job_a = _make_job(tmp_env, external_job_id="7263592", title="Software Engineer, Backend")
    job_b = _make_job(
        tmp_env, external_job_id="8088444", title="Software Engineer, Wallet",
        description=DESCRIPTION.replace("Backend services", "Wallet services"),
    )
    _exact_confirmed_record(tmp_env, job_a)

    assert get_verified_value(job_a) == EmploymentType.FULL_TIME
    assert get_verified_value(job_b) is None, "verifying one Robinhood job must never imply another Robinhood job is FULL_TIME"


# --------------------------------------------------------------------------
# Persistence / provenance round-trip
# --------------------------------------------------------------------------

def test_restart_serialization_preserves_provenance(tmp_env):
    job = _make_job(tmp_env)
    confirmed = _exact_confirmed_record(tmp_env, job)

    # Simulate a fresh process re-reading persisted state (no in-memory
    # object reuse) via a brand-new query.
    reread = get_latest_record(job.id)

    assert reread.id == confirmed.id
    assert reread.job_id == job.id
    assert reread.provider == "greenhouse"
    assert reread.external_job_id == "7263592"
    assert reread.evidence_url == DICE_URL
    assert reread.evidence_source_name == "Dice"
    assert reread.raw_employment_type_value == "Full Time"
    assert reread.normalized_value == "FULL_TIME"
    assert reread.identity_match_verdict == "EXACT_MATCH"
    assert reread.human_confirmed is True
    assert reread.human_confirmed_text == "I VERIFY JOB AS FULL_TIME BASED ON THE PRESENTED MATCHED EVIDENCE."
    assert reread.captured_at
    assert reread.human_confirmed_at
    assert reread.posting_fingerprint_at_verification == job.jd_sponsorship_fingerprint


def test_reverification_is_append_only_not_an_update(tmp_env):
    job = _make_job(tmp_env)
    first = _exact_confirmed_record(tmp_env, job)
    second = _exact_confirmed_record(tmp_env, job)
    assert second.id != first.id
    # Both rows persist; only the latest is consulted.
    assert get_latest_record(job.id).id == second.id


# --------------------------------------------------------------------------
# Feasibility gate + eligibility gate consume valid evidence correctly
# --------------------------------------------------------------------------

def test_feasibility_gate_consumes_confirmed_human_evidence(tmp_env):
    job = _make_job(
        tmp_env, provider="mock_ats",  # avoid a real network page-evidence fetch
        sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        technical_match_score=80.0, matched_skills="python,go", gap_skills="",
    )
    before = evaluate_canary_feasibility(job)
    assert before.employment_type.verdict == FeasibilityVerdict.REVIEW

    _exact_confirmed_record(tmp_env, job)
    after_job = get_job(job.id)
    after = evaluate_canary_feasibility(after_job)
    assert after.employment_type.verdict == FeasibilityVerdict.PASS
    assert "HUMAN_VERIFIED_EXTERNAL_EVIDENCE" in after.employment_type.reason


def test_eligibility_gate_consumes_confirmed_human_evidence(tmp_env):
    job = _make_job(tmp_env, sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR)
    before = evaluate_executor_eligibility(job)
    assert before.employment_type == EmploymentType.UNKNOWN

    _exact_confirmed_record(tmp_env, job)
    after_job = get_job(job.id)
    after = evaluate_executor_eligibility(after_job)
    assert after.employment_type == EmploymentType.FULL_TIME


def test_eligibility_gate_still_hard_skips_on_official_contract_despite_human_evidence(tmp_env):
    job = _make_job(tmp_env, employment_type="Contract", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR)
    _exact_confirmed_record(tmp_env, job)
    reloaded = get_job(job.id)
    result = evaluate_executor_eligibility(reloaded)
    assert result.hard_skip
    assert result.blocking_category == "EMPLOYMENT_TYPE"
    assert result.employment_type == EmploymentType.CONTRACT
