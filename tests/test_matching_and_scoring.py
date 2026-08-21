from app.matching.roles import is_target_role
from app.matching.skills import extract_jd_keywords, match_candidate_skills
from app.models import FreshnessTier, PriorityTier, SponsorshipStatus, WorkArrangement
from app.scoring.scorer import compute_priority_score, determine_priority_tier


def test_target_role_primary():
    relevant, primary = is_target_role("Backend Software Engineer")
    assert relevant and primary


def test_target_role_secondary():
    relevant, primary = is_target_role("DevOps Engineer")
    assert relevant and not primary


def test_non_stem_role_rejected():
    relevant, primary = is_target_role("Retail Store Associate")
    assert not relevant


def test_extract_and_match_skills(sample_profile):
    jd = "We need a Python engineer with FastAPI and PostgreSQL experience, plus Docker and CI/CD."
    keywords = extract_jd_keywords(jd)
    assert "python" in keywords and "fastapi" in keywords

    score, matched, gaps = match_candidate_skills(keywords, sample_profile.skills)
    assert score > 50
    assert "python" in matched
    assert "kafka" not in matched


def test_priority_tier_ordering():
    assert determine_priority_tier(WorkArrangement.REMOTE, SponsorshipStatus.CONFIRMED_SPONSOR) == PriorityTier.P1_REMOTE_CONFIRMED
    assert determine_priority_tier(WorkArrangement.REMOTE, SponsorshipStatus.LIKELY_SPONSOR) == PriorityTier.P2_REMOTE_LIKELY
    assert determine_priority_tier(WorkArrangement.HYBRID, SponsorshipStatus.CONFIRMED_SPONSOR) == PriorityTier.P3_HYBRID_CONFIRMED
    assert determine_priority_tier(WorkArrangement.ONSITE, SponsorshipStatus.CONFIRMED_SPONSOR) == PriorityTier.P5_ONSITE_CONFIRMED


def test_no_sponsorship_and_unknown_are_not_eligible():
    assert determine_priority_tier(WorkArrangement.REMOTE, SponsorshipStatus.NO_SPONSORSHIP) == PriorityTier.NOT_ELIGIBLE
    assert determine_priority_tier(WorkArrangement.REMOTE, SponsorshipStatus.UNKNOWN) == PriorityTier.NOT_ELIGIBLE


def test_remote_confirmed_scores_higher_than_onsite_likely():
    remote_score = compute_priority_score(PriorityTier.P1_REMOTE_CONFIRMED, 80, FreshnessTier.HIGH)
    onsite_score = compute_priority_score(PriorityTier.P6_ONSITE_LIKELY, 80, FreshnessTier.HIGH)
    assert remote_score > onsite_score


def test_remote_no_sponsorship_scores_zero_not_eligible():
    tier = determine_priority_tier(WorkArrangement.REMOTE, SponsorshipStatus.NO_SPONSORSHIP)
    score = compute_priority_score(tier, 100, FreshnessTier.MAXIMUM)
    assert tier == PriorityTier.NOT_ELIGIBLE
    assert score == 0.0
