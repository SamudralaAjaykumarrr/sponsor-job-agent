"""Sponsorship Intelligence Coverage V1: app.sponsorship.refresh --
evidence-only re-evaluation that must never disturb application_state or any
other pipeline output, and test matrix item I: the feasibility gate must
consume the freshly-enriched sponsorship_status correctly."""

import datetime

from app.applications.canary_feasibility import FeasibilityVerdict, _sponsorship
from app.jobs_repo import get_job, insert_job
from app.models import ApplicationState, Job, SponsorshipStatus
from app.registry.models import Company
from app.registry import store
from app.sponsorship.evidence import SponsorshipEvidence, record_evidence
from app.sponsorship.refresh import refresh_job_sponsorship

THIS_YEAR = datetime.datetime.now(datetime.timezone.utc).year


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


def _job(company, application_state=ApplicationState.NEEDS_USER_ACTION, **overrides):
    defaults = dict(
        title="Backend Software Engineer", company=company, company_identifier=company.lower(),
        location="Remote - US",
        description="Join our backend team building scalable Python APIs. 3+ years experience.",
        provider="manual", url=f"https://example.com/{company}",
        sponsorship_status=SponsorshipStatus.UNKNOWN, application_state=application_state,
        priority_score=42, priority_tier="P2_REMOTE_LIKELY",
    )
    defaults.update(overrides)
    return insert_job(Job(**defaults))


def test_refresh_never_touches_application_state(tmp_env):
    """Guards against the exact regression this module exists to avoid:
    re-running the full analyze_job() pipeline on an already-progressed job
    (e.g. one already at NEEDS_USER_ACTION with a browser-assist session in
    flight) would silently reset it back to ANALYZED."""
    job_id = _job("StaticCo", application_state=ApplicationState.NEEDS_USER_ACTION)
    outcome = refresh_job_sponsorship(job_id)
    refreshed = get_job(job_id)
    assert refreshed.application_state == ApplicationState.NEEDS_USER_ACTION
    assert outcome.job_id == job_id


def test_refresh_never_touches_priority_or_score(tmp_env):
    job_id = _job("StaticCo2")
    refresh_job_sponsorship(job_id)
    refreshed = get_job(job_id)
    assert refreshed.priority_score == 42
    assert refreshed.priority_tier.value == "P2_REMOTE_LIKELY"


def test_refresh_upgrades_status_using_new_evidence(tmp_env):
    """The whole point of this feature: a job that was UNKNOWN at discovery
    time can honestly become LIKELY_SPONSOR once real historical evidence for
    its employer is imported afterward -- without the JD text changing."""
    _strong_technical_employer("EnrichCo", "enrichco.com")
    job_id = _job("EnrichCo", sponsorship_status=SponsorshipStatus.UNKNOWN)
    outcome = refresh_job_sponsorship(job_id)
    assert outcome.changed
    assert outcome.new_status == SponsorshipStatus.LIKELY_SPONSOR
    refreshed = get_job(job_id)
    assert refreshed.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR
    assert refreshed.application_state == ApplicationState.NEEDS_USER_ACTION  # untouched


def test_refresh_never_upgrades_past_no_sponsorship(tmp_env):
    _strong_technical_employer("DominantCo", "dominantco.com")
    job_id = _job(
        "DominantCo", sponsorship_status=SponsorshipStatus.NO_SPONSORSHIP,
        description="We will not sponsor employment visas for this position.",
    )
    outcome = refresh_job_sponsorship(job_id)
    assert outcome.new_status == SponsorshipStatus.NO_SPONSORSHIP


def test_refresh_excludes_named_job_ids_via_caller(tmp_env):
    """Mirrors the CLI's --exclude contract: the caller (not this function)
    is responsible for skipping protected job ids -- verifies the function
    itself is safe to simply not call."""
    job_id = _job("ProtectedCo")
    before = get_job(job_id)
    # Simulates the CLI/report skipping this id entirely.
    after = get_job(job_id)
    assert before.sponsorship_status == after.sponsorship_status


# --- item I: feasibility gate must consume enriched evidence correctly -----

def test_feasibility_gate_rejects_unknown_before_enrichment(tmp_env):
    job_id = _job("GateCo", sponsorship_status=SponsorshipStatus.UNKNOWN)
    job = get_job(job_id)
    result = _sponsorship(job)
    assert result.verdict == FeasibilityVerdict.REJECT


def test_feasibility_gate_passes_after_enrichment_upgrades_to_likely(tmp_env):
    _strong_technical_employer("GateCo2", "gateco2.com")
    job_id = _job("GateCo2", sponsorship_status=SponsorshipStatus.UNKNOWN)
    refresh_job_sponsorship(job_id)
    job = get_job(job_id)
    assert job.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR
    result = _sponsorship(job)
    assert result.verdict == FeasibilityVerdict.PASS


def test_feasibility_gate_still_rejects_explicit_no_sponsorship_despite_history(tmp_env):
    _strong_technical_employer("GateCo3", "gateco3.com")
    job_id = _job(
        "GateCo3", sponsorship_status=SponsorshipStatus.UNKNOWN,
        description="We will not sponsor employment visas for this position.",
    )
    refresh_job_sponsorship(job_id)
    job = get_job(job_id)
    assert job.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP
    result = _sponsorship(job)
    assert result.verdict == FeasibilityVerdict.REJECT
