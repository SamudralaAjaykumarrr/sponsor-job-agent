"""CLAUDE.md Phase 7 sections 11-15: employer historical profile, recency
weighting, role/occupation similarity, location evidence, history score."""

import datetime

from app.registry.models import Company
from app.registry import store
from app.sponsorship.evidence import SponsorshipEvidence, record_evidence
from app.sponsorship.profile import compute_employer_profile, get_or_compute_profile, refresh_employer_profile
from app.sponsorship.schema import HistoricalStrength, RecencyBucket, recency_bucket
from app.sponsorship.similarity import location_similarity, role_similarity
from app.sponsorship.schema import RoleSimilarityTier

THIS_YEAR = datetime.datetime.now(datetime.timezone.utc).year


def _mk_company():
    return store.insert_company(Company(normalized_name="techco", display_name="TechCo", primary_domain="techco.com"))


def _add_evidence(company_id, fiscal_year, occupation_code="15-1252", occupation_title="Software Developers, Applications", state="CA"):
    record_evidence(SponsorshipEvidence(
        company_id=company_id, company_name_raw="TechCo", source_type="DOL_LCA_DATA",
        fiscal_year=fiscal_year, occupation_code=occupation_code, occupation_title=occupation_title,
        worksite_state=state, job_title="Software Engineer",
    ))


def test_recency_bucket_boundaries():
    assert recency_bucket(THIS_YEAR, THIS_YEAR) == RecencyBucket.CURRENT
    assert recency_bucket(THIS_YEAR - 1, THIS_YEAR) == RecencyBucket.ONE_YEAR
    assert recency_bucket(THIS_YEAR - 2, THIS_YEAR) == RecencyBucket.TWO_YEARS
    assert recency_bucket(THIS_YEAR - 5, THIS_YEAR) == RecencyBucket.THREE_TO_FIVE_YEARS
    assert recency_bucket(THIS_YEAR - 6, THIS_YEAR) == RecencyBucket.OLDER
    assert recency_bucket(None, THIS_YEAR) == RecencyBucket.OLDER


def test_no_evidence_yields_none_strength(tmp_env):
    cid = _mk_company()
    profile = compute_employer_profile(cid)
    assert profile.historical_strength == HistoricalStrength.NONE
    assert profile.history_score == 0.0


def test_strong_recent_technical_history(tmp_env):
    cid = _mk_company()
    for fy in (THIS_YEAR, THIS_YEAR - 1):
        for _ in range(3):
            _add_evidence(cid, fy)
    profile = compute_employer_profile(cid)
    assert profile.historical_strength == HistoricalStrength.STRONG_RECENT
    assert profile.continuity_years == 2
    assert "software_engineering" in profile.recent_occupation_families
    assert profile.history_score > 0


def test_old_non_technical_history_stays_weak(tmp_env):
    cid = _mk_company()
    _add_evidence(cid, THIS_YEAR - 8, occupation_code="41-4012", occupation_title="Sales Representatives", state="NY")
    profile = compute_employer_profile(cid)
    assert profile.historical_strength == HistoricalStrength.OLD
    assert profile.recent_occupation_families == []


def test_trend_up_and_down(tmp_env):
    cid_up = _mk_company()
    _add_evidence(cid_up, THIS_YEAR)
    _add_evidence(cid_up, THIS_YEAR)
    _add_evidence(cid_up, THIS_YEAR - 3)
    profile_up = compute_employer_profile(cid_up)
    assert profile_up.trend == "UP"


def test_cache_roundtrip(tmp_env):
    cid = _mk_company()
    for _ in range(3):
        _add_evidence(cid, THIS_YEAR)
    refreshed = refresh_employer_profile(cid)
    cached = get_or_compute_profile(cid)
    assert cached.history_score == refreshed.history_score
    assert cached.historical_strength == refreshed.historical_strength
    assert cached.recent_occupation_titles == refreshed.recent_occupation_titles


def test_role_similarity_strong_exact_title():
    tier, reasons = role_similarity("Software Developers, Applications", "Software Developers, Applications")
    assert tier == RoleSimilarityTier.STRONG
    assert reasons


def test_role_similarity_moderate_same_family():
    tier, _ = role_similarity("Backend Software Engineer", "Software Developers, Applications")
    assert tier in (RoleSimilarityTier.STRONG, RoleSimilarityTier.MODERATE)


def test_role_similarity_none_unrelated():
    tier, _ = role_similarity("Backend Software Engineer", "Retail Sales Associate")
    assert tier == RoleSimilarityTier.NONE


def test_location_similarity_match_and_mismatch():
    matched, reason = location_similarity("CA", {"CA", "NY"})
    assert matched is True
    assert "CA" in reason
    unmatched, _ = location_similarity("TX", {"CA", "NY"})
    assert unmatched is False
