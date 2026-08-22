"""CLAUDE.md Phase 7 section 57: deterministic end-to-end acceptance
scenarios for the sponsorship intelligence layer."""

import datetime

from app.candidate.profile import save_profile
from app.models import ApplicationMode, ApplicationState, Job, SponsorshipStatus
from app.pipeline import ingest_and_process, reanalyze_job
from app.registry.models import Company
from app.registry import store
from app.sponsorship.decision import list_decision_history
from app.sponsorship.evidence import SponsorshipEvidence, record_evidence
from app.sponsorship.importers import import_uscis_employer_data, recompute_profiles_for_dataset

THIS_YEAR = datetime.datetime.now(datetime.timezone.utc).year

# Skill-rich prefix so the technical-match gate passes regardless of the
# sponsorship-language suffix under test -- matches sample_profile's skills.
_JD_BASE = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python "
    "using FastAPI and PostgreSQL, with Docker-based CI/CD. "
)


def _mk_job(company, description, location="Remote", title="Backend Software Engineer"):
    job = Job(title=title, company=company, location=location, description=_JD_BASE + description, mode=ApplicationMode.ASSIST)
    return ingest_and_process(job)


def _strong_technical_employer(name, domain):
    cid = store.insert_company(Company(normalized_name=name.lower(), display_name=name, primary_domain=domain))
    for fy in (THIS_YEAR, THIS_YEAR - 1):
        for _ in range(3):
            record_evidence(SponsorshipEvidence(
                company_id=cid, company_name_raw=name, source_type="DOL_LCA_DATA", fiscal_year=fy,
                occupation_code="15-1252", occupation_title="Software Developers, Applications",
                worksite_state="CA", job_title="Software Engineer",
            ))
    return cid


# --- Scenario 1: strong history + explicit NO -> hard skip -----------------

def test_scenario_1_strong_history_explicit_no_sponsorship_hard_skips(tmp_env, sample_profile):
    save_profile(sample_profile)
    _strong_technical_employer("ScenarioOneCo", "scenario1.com")
    result = _mk_job("ScenarioOneCo", "We will not sponsor visas now or in the future.", location="CA")
    assert result.application_state == ApplicationState.SKIPPED_NO_SPONSORSHIP
    assert result.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP


# --- Scenario 2: no history + explicit confirmed -> eligible downstream ----

def test_scenario_2_no_history_explicit_confirmed_eligible(tmp_env, sample_profile):
    save_profile(sample_profile)
    result = _mk_job("ScenarioTwoCo", "H-1B sponsorship available for this position.", location="Remote")
    assert result.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
    assert result.application_state == ApplicationState.READY_TO_APPLY
    assert result.resume_docx_path


# --- Scenario 3: strong recent software history + silent JD -> LIKELY, review

def test_scenario_3_strong_history_silent_jd_review_required(tmp_env, sample_profile):
    save_profile(sample_profile)
    _strong_technical_employer("ScenarioThreeCo", "scenario3.com")
    result = _mk_job("ScenarioThreeCo", "Join our backend team. 3+ years experience required.", location="CA")
    assert result.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR
    assert result.application_state == ApplicationState.REVIEW_REQUIRED


# --- Scenario 4: weak/old unrelated history + silent JD -> UNKNOWN ---------

def test_scenario_4_weak_old_history_silent_jd_unknown(tmp_env, sample_profile):
    save_profile(sample_profile)
    cid = store.insert_company(Company(normalized_name="scenariofourco", display_name="ScenarioFourCo", primary_domain="scenario4.com"))
    record_evidence(SponsorshipEvidence(
        company_id=cid, company_name_raw="ScenarioFourCo", source_type="USCIS_EMPLOYER_DATA",
        fiscal_year=THIS_YEAR - 8, occupation_code="41-4012", occupation_title="Sales Representatives",
        worksite_state="NY",
    ))
    result = _mk_job("ScenarioFourCo", "Join our backend team.", location="NY")
    assert result.sponsorship_status == SponsorshipStatus.UNKNOWN
    assert result.application_state == ApplicationState.ANALYZED


# --- Scenario 5: ambiguous identity -> no history auto-attached ------------

def test_scenario_5_ambiguous_identity_no_history_auto_attached(tmp_env, sample_profile):
    save_profile(sample_profile)
    store.insert_company(Company(normalized_name="ambigscenario", display_name="AmbigScenario", primary_domain="a1.com"))
    store.insert_company(Company(normalized_name="ambigscenario", display_name="AmbigScenario", primary_domain="a2.com"))
    result = _mk_job("AmbigScenario", "Join our backend team.", location="CA")
    assert result.sponsorship_status == SponsorshipStatus.UNKNOWN
    from app.sponsorship.identity import list_pending_reviews

    assert len(list_pending_reviews()) == 1


# --- Scenario 6: new dataset import recomputes profile, current jobs unaffected

def test_scenario_6_new_dataset_import_recomputes_profile_not_current_classifications(tmp_env, sample_profile, tmp_path):
    save_profile(sample_profile)
    store.insert_company(Company(normalized_name="scenariosixco", display_name="ScenarioSixCo", primary_domain="scenario6.com"))
    result = _mk_job("ScenarioSixCo", "We are unable to sponsor visas now or in the future.", location="CA")
    assert result.application_state == ApplicationState.SKIPPED_NO_SPONSORSHIP

    path = tmp_path / "uscis.csv"
    path.write_text(
        "Fiscal Year,Employer,Initial Approval,Initial Denial,Continuing Approval,Continuing Denial,NAICS Code,State,City\n"
        "2024,ScenarioSixCo,50,2,60,1,5415,CA,San Francisco\n"
    )
    import_result = import_uscis_employer_data(path)
    recompute_profiles_for_dataset(import_result.dataset_id)

    from app.jobs_repo import get_job

    unchanged = get_job(result.id)
    assert unchanged.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP
    assert unchanged.application_state == ApplicationState.SKIPPED_NO_SPONSORSHIP


# --- Scenario 7: JD silent -> explicit sponsorship: reclassify, version up -

def test_scenario_7_jd_silent_to_explicit_sponsorship_reclassifies(tmp_env, sample_profile):
    save_profile(sample_profile)
    result = _mk_job("ScenarioSevenCo", "Join our backend team.", location="Remote")
    assert result.sponsorship_status == SponsorshipStatus.UNKNOWN
    assert result.application_state == ApplicationState.ANALYZED

    updated = reanalyze_job(result.id, new_description=_JD_BASE + "We sponsor H-1B visas for this role. Join our backend team.")
    assert updated.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
    assert updated.application_state == ApplicationState.READY_TO_APPLY

    history = list_decision_history(result.id)
    assert len(history) == 2
    assert history[0]["status"] == "UNKNOWN"
    assert history[1]["status"] == "CONFIRMED_SPONSOR"


# --- Scenario 8: JD positive -> explicit no-sponsorship: hard skip ---------

def test_scenario_8_jd_positive_to_explicit_no_sponsorship_hard_skips(tmp_env, sample_profile):
    save_profile(sample_profile)
    result = _mk_job("ScenarioEightCo", "We sponsor H-1B visas for this role.", location="Remote")
    assert result.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
    assert result.application_state == ApplicationState.READY_TO_APPLY

    updated = reanalyze_job(result.id, new_description=_JD_BASE + "We will not sponsor visas now or in the future.")
    assert updated.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP
    assert updated.application_state == ApplicationState.SKIPPED_NO_SPONSORSHIP

    history = list_decision_history(result.id)
    assert len(history) == 2


def test_terminal_application_state_not_silently_moved_by_jd_change(tmp_env, sample_profile):
    """A human already applied -- a later JD edit must record a new audited
    decision but never silently flip application_state out from under them."""
    from app.jobs_repo import update_job

    save_profile(sample_profile)
    result = _mk_job("TerminalStateCo", "We sponsor H-1B visas for this role.", location="Remote")
    update_job(result.id, application_state=ApplicationState.APPLIED)

    updated = reanalyze_job(result.id, new_description=_JD_BASE + "We will not sponsor visas now or in the future.")
    assert updated.application_state == ApplicationState.APPLIED
    assert updated.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR  # job row's cached status untouched
    history = list_decision_history(result.id)
    assert history[-1]["status"] == "NO_SPONSORSHIP"  # audit trail still recorded the new evidence
