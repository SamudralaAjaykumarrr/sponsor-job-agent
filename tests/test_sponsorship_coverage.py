"""Sponsorship Intelligence Coverage V1: app.sponsorship.coverage -- real,
DB-derived before/after coverage metrics, scoped to real (non-fixture)
discovered employers."""

from app.jobs_repo import insert_job
from app.models import ApplicationState, Job, SponsorshipStatus
from app.registry.models import Company
from app.registry import store
from app.sponsorship.coverage import coverage_snapshot
from app.sponsorship.evidence import SponsorshipEvidence, record_evidence


def _job(company, sponsorship_status=SponsorshipStatus.UNKNOWN, **overrides):
    defaults = dict(
        title="Backend Software Engineer", company=company, company_identifier=company.lower(),
        location="Remote - US", description="Join our backend team.", provider="manual",
        url=f"https://example.com/{company}", sponsorship_status=sponsorship_status,
        application_state=ApplicationState.ANALYZED,
    )
    defaults.update(overrides)
    return insert_job(Job(**defaults))


def test_coverage_excludes_fixtures(tmp_env):
    _job("RealCo")
    _job("Acme Corp")
    _job("FixtureCo", is_test_fixture=True)
    snap = coverage_snapshot()
    assert snap["employers_total"] == 1
    assert snap["jobs_total"] == 1


def test_coverage_counts_matched_vs_unmatched(tmp_env):
    cid = store.insert_company(Company(normalized_name="evidenceco", display_name="EvidenceCo", primary_domain="evidenceco.com"))
    record_evidence(SponsorshipEvidence(company_id=cid, company_name_raw="EvidenceCo Inc", source_type="USCIS_EMPLOYER_DATA", fiscal_year=2024))
    _job("EvidenceCo")
    _job("NoEvidenceCo")
    snap = coverage_snapshot()
    assert snap["employers_total"] == 2
    assert snap["employers_matched_to_evidence"] == 1
    assert snap["employers_unmatched"] == 1
    assert "NoEvidenceCo" in snap["unmatched_employer_names"]


def test_coverage_job_status_breakdown(tmp_env):
    _job("ConfirmedCo", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR)
    _job("LikelyCo", sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR)
    _job("NoSponsorCo", sponsorship_status=SponsorshipStatus.NO_SPONSORSHIP)
    _job("UnknownCo", sponsorship_status=SponsorshipStatus.UNKNOWN)
    snap = coverage_snapshot()
    assert snap["jobs_confirmed_sponsor"] == 1
    assert snap["jobs_likely_sponsor"] == 1
    assert snap["jobs_no_sponsorship"] == 1
    assert snap["jobs_unknown"] == 1


def test_coverage_counts_ambiguous_employers(tmp_env):
    store.insert_company(Company(normalized_name="dupeco", display_name="DupeCo East", primary_domain="dupe-e.com"))
    store.insert_company(Company(normalized_name="dupeco", display_name="DupeCo West", primary_domain="dupe-w.com"))
    _job("DupeCo")
    snap = coverage_snapshot()
    assert snap["employers_ambiguous"] == 1
    assert "DupeCo" in snap["ambiguous_employer_names"]
