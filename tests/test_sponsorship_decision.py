"""CLAUDE.md Phase 7 section 43 (required examples A-G) + sections 17-24
(negation safety, conditional wording, conflict resolution, decision audit
versioning, JD-change detection)."""

import datetime

from app.models import SponsorshipStatus
from app.registry.models import Company
from app.registry import store
from app.sponsorship.decision import decide_sponsorship, get_latest_decision, list_decision_history, persist_decision
from app.sponsorship.evidence import SponsorshipEvidence, record_evidence

THIS_YEAR = datetime.datetime.now(datetime.timezone.utc).year


def _strong_technical_employer(name="StrongCo", domain="strongco.com"):
    cid = store.insert_company(Company(normalized_name=name.lower(), display_name=name, primary_domain=domain))
    for fy in (THIS_YEAR, THIS_YEAR - 1):
        for _ in range(3):
            record_evidence(SponsorshipEvidence(
                company_id=cid, company_name_raw=name, source_type="DOL_LCA_DATA", fiscal_year=fy,
                occupation_code="15-1252", occupation_title="Software Developers, Applications",
                worksite_state="CA", job_title="Software Engineer",
            ))
    return cid


def _old_nontechnical_employer(name="OldCo", domain="oldco.com"):
    cid = store.insert_company(Company(normalized_name=name.lower(), display_name=name, primary_domain=domain))
    record_evidence(SponsorshipEvidence(
        company_id=cid, company_name_raw=name, source_type="USCIS_EMPLOYER_DATA",
        fiscal_year=THIS_YEAR - 8, occupation_code="41-4012", occupation_title="Sales Representatives",
        worksite_state="NY",
    ))
    return cid


# --- Section 43 example A -----------------------------------------------

def test_example_a_strong_history_but_explicit_no_sponsorship(tmp_env):
    _strong_technical_employer("HistoryCoA", "historycoa.com")
    decision = decide_sponsorship(
        "Backend Software Engineer", "HistoryCoA",
        "We are unable to sponsor visas for this role, now or in the future.", "CA",
    )
    assert decision.status == SponsorshipStatus.NO_SPONSORSHIP


# --- Section 43 example B -----------------------------------------------

def test_example_b_no_history_but_explicit_confirmed(tmp_env):
    decision = decide_sponsorship(
        "Backend Software Engineer", "BrandNewCoB",
        "H-1B sponsorship available for qualified candidates.", "CA",
    )
    assert decision.status == SponsorshipStatus.CONFIRMED_SPONSOR


# --- Section 43 example C -----------------------------------------------

def test_example_c_recent_technical_history_silent_jd_never_confirmed(tmp_env):
    _strong_technical_employer("HistoryCoC", "historycoc.com")
    decision = decide_sponsorship(
        "Backend Software Engineer", "HistoryCoC",
        "Join our backend team building scalable Python APIs. 3+ years experience.", "CA",
    )
    assert decision.status in (SponsorshipStatus.LIKELY_SPONSOR, SponsorshipStatus.UNKNOWN)
    assert decision.status != SponsorshipStatus.CONFIRMED_SPONSOR


def test_example_c_strong_match_yields_likely(tmp_env):
    _strong_technical_employer("HistoryCoC2", "historycoc2.com")
    decision = decide_sponsorship(
        "Backend Software Engineer", "HistoryCoC2",
        "Join our backend team building scalable Python APIs. 3+ years experience.", "CA",
    )
    assert decision.status == SponsorshipStatus.LIKELY_SPONSOR


# --- Section 43 example D -----------------------------------------------

def test_example_d_old_nontechnical_history_silent_jd(tmp_env):
    _old_nontechnical_employer("HistoryCoD", "historycod.com")
    decision = decide_sponsorship(
        "Backend Software Engineer", "HistoryCoD",
        "Join our backend team building scalable Python APIs.", "CA",
    )
    assert decision.status in (SponsorshipStatus.UNKNOWN, SponsorshipStatus.LIKELY_SPONSOR)
    assert decision.status != SponsorshipStatus.CONFIRMED_SPONSOR
    # Deterministic choice made by this implementation: OLD/non-technical never upgrades.
    assert decision.status == SponsorshipStatus.UNKNOWN


# --- Section 43 example E -----------------------------------------------

def test_example_e_employer_policy_generally_available_silent_jd(tmp_env, tmp_path):
    """Uses the local known-sponsors reference list as the 'employer policy
    generally available' signal (tmp_env seeds Acme Corp/Globex/Initech)."""
    decision = decide_sponsorship(
        "Backend Software Engineer", "Acme Corp",
        "Join our backend team building scalable Python APIs.", "CA",
    )
    assert decision.status == SponsorshipStatus.LIKELY_SPONSOR


# --- Section 43 example F -----------------------------------------------

def test_example_f_conditional_language_is_review_required(tmp_env):
    decision = decide_sponsorship(
        "Backend Software Engineer", "CaseByCaseCo",
        "We may sponsor exceptional candidates for this role on a case-by-case basis.", "CA",
    )
    assert decision.status == SponsorshipStatus.LIKELY_SPONSOR
    assert decision.blocking_reason


# --- Section 43 example G -----------------------------------------------

def test_example_g_conflicting_positive_and_negative_is_review_required(tmp_env):
    decision = decide_sponsorship(
        "Backend Software Engineer", "ConflictCoG",
        "We sponsor H-1B visas for most roles, but we are unable to sponsor for this particular position.", "CA",
    )
    assert decision.status == SponsorshipStatus.LIKELY_SPONSOR
    assert decision.conflict is True
    assert decision.blocking_reason


# --- Negation safety (section 17) ----------------------------------------

def test_negation_we_do_not_offer_sponsorship(tmp_env):
    decision = decide_sponsorship("Engineer", "NegCo", "We do not offer visa sponsorship.", "CA")
    assert decision.status == SponsorshipStatus.NO_SPONSORSHIP


def test_negation_applicants_requiring_sponsorship_not_considered(tmp_env):
    decision = decide_sponsorship(
        "Engineer", "NegCo2", "Applicants requiring sponsorship will not be considered for this role.", "CA",
    )
    assert decision.status == SponsorshipStatus.NO_SPONSORSHIP


def test_negation_historical_sponsor_but_this_role_does_not(tmp_env):
    decision = decide_sponsorship(
        "Engineer", "NegCo3",
        "We have historically sponsored employees, but this role does not support sponsorship.", "CA",
    )
    assert decision.status == SponsorshipStatus.NO_SPONSORSHIP


def test_negation_us_citizens_only(tmp_env):
    decision = decide_sponsorship("Engineer", "NegCo4", "US citizens only need apply.", "CA")
    assert decision.status == SponsorshipStatus.NO_SPONSORSHIP


def test_negation_permanent_work_authorization_required(tmp_env):
    decision = decide_sponsorship("Engineer", "NegCo5", "Permanent work authorization required for this role.", "CA")
    assert decision.status == SponsorshipStatus.NO_SPONSORSHIP


def test_conditional_may_be_considered_not_auto_confirmed(tmp_env):
    decision = decide_sponsorship("Engineer", "CondCo", "Visa sponsorship may be considered for the right candidate.", "CA")
    assert decision.status != SponsorshipStatus.CONFIRMED_SPONSOR
    assert decision.status == SponsorshipStatus.LIKELY_SPONSOR


# --- Decision audit / versioning (sections 21-24) -------------------------

def test_persist_decision_creates_versioned_audit_row(tmp_env):
    job_id = 999
    d1 = persist_decision(job_id, "Backend Engineer", "AuditCo", "Join our backend team.", "CA")
    assert d1.decision_version == 1
    # Re-persisting identical input is a no-op (no new version).
    d1_again = persist_decision(job_id, "Backend Engineer", "AuditCo", "Join our backend team.", "CA")
    assert d1_again.decision_version == 1
    assert len(list_decision_history(job_id)) == 1

    # JD changes to explicit confirmation -> new version, old retained.
    d2 = persist_decision(job_id, "Backend Engineer", "AuditCo", "We sponsor H-1B visas for this role.", "CA")
    assert d2.decision_version == 2
    assert d2.status == SponsorshipStatus.CONFIRMED_SPONSOR
    history = list_decision_history(job_id)
    assert len(history) == 2
    assert history[0]["status"] == "UNKNOWN"
    assert history[1]["status"] == "CONFIRMED_SPONSOR"


def test_jd_change_from_positive_to_no_sponsorship_hard_skips(tmp_env):
    job_id = 1000
    d1 = persist_decision(job_id, "Engineer", "FlipCo", "We sponsor H-1B visas.", "CA")
    assert d1.status == SponsorshipStatus.CONFIRMED_SPONSOR
    d2 = persist_decision(job_id, "Engineer", "FlipCo", "We are unable to sponsor visas now or in the future.", "CA")
    assert d2.status == SponsorshipStatus.NO_SPONSORSHIP
    assert d2.decision_version == 2


def test_historical_evidence_never_overrides_no_sponsorship(tmp_env):
    _strong_technical_employer("DominantHistoryCo", "dominanthistoryco.com")
    decision = decide_sponsorship(
        "Backend Software Engineer", "DominantHistoryCo",
        "We will not sponsor employment visas for this position.", "CA",
    )
    assert decision.status == SponsorshipStatus.NO_SPONSORSHIP


def test_historical_evidence_never_overrides_confirmed(tmp_env):
    decision = decide_sponsorship(
        "Backend Software Engineer", "NoHistoryConfirmedCo",
        "Visa sponsorship available for this position.", "CA",
    )
    assert decision.status == SponsorshipStatus.CONFIRMED_SPONSOR
