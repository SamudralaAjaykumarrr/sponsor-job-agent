from app.models import ApplicationState, FreshnessTier, PriorityTier, SponsorshipStatus, WorkArrangement

FRESHNESS_WEIGHT = {
    FreshnessTier.MAXIMUM: 20,
    FreshnessTier.VERY_HIGH: 15,
    FreshnessTier.HIGH: 10,
    FreshnessTier.MODERATE: 5,
    FreshnessTier.LOWER: 0,
}

# Ordered highest-priority first, per CLAUDE.md "Job Priority" section.
TIER_ORDER = [
    PriorityTier.P1_REMOTE_CONFIRMED,
    PriorityTier.P2_REMOTE_LIKELY,
    PriorityTier.P3_HYBRID_CONFIRMED,
    PriorityTier.P4_HYBRID_LIKELY,
    PriorityTier.P5_ONSITE_CONFIRMED,
    PriorityTier.P6_ONSITE_LIKELY,
]

TIER_BASE_SCORE = {
    PriorityTier.P1_REMOTE_CONFIRMED: 100,
    PriorityTier.P2_REMOTE_LIKELY: 85,
    PriorityTier.P3_HYBRID_CONFIRMED: 70,
    PriorityTier.P4_HYBRID_LIKELY: 55,
    PriorityTier.P5_ONSITE_CONFIRMED: 40,
    PriorityTier.P6_ONSITE_LIKELY: 25,
    PriorityTier.NOT_ELIGIBLE: 0,
}


def determine_priority_tier(
    work_arrangement: WorkArrangement, sponsorship_status: SponsorshipStatus
) -> PriorityTier:
    if sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP:
        return PriorityTier.NOT_ELIGIBLE
    if sponsorship_status == SponsorshipStatus.UNKNOWN:
        return PriorityTier.NOT_ELIGIBLE

    if work_arrangement == WorkArrangement.REMOTE:
        return (
            PriorityTier.P1_REMOTE_CONFIRMED
            if sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
            else PriorityTier.P2_REMOTE_LIKELY
        )
    if work_arrangement == WorkArrangement.HYBRID:
        return (
            PriorityTier.P3_HYBRID_CONFIRMED
            if sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
            else PriorityTier.P4_HYBRID_LIKELY
        )
    # ONSITE or UNKNOWN work arrangement with known sponsorship still gets scored
    # (treated conservatively as onsite-tier priority).
    return (
        PriorityTier.P5_ONSITE_CONFIRMED
        if sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
        else PriorityTier.P6_ONSITE_LIKELY
    )


def compute_priority_score(
    priority_tier: PriorityTier, technical_match_score: float, freshness_tier: FreshnessTier
) -> float:
    base = TIER_BASE_SCORE[priority_tier]
    if priority_tier == PriorityTier.NOT_ELIGIBLE:
        return 0.0
    match_component = technical_match_score * 0.3  # 0-30
    freshness_component = FRESHNESS_WEIGHT[freshness_tier]  # 0-20
    return round(base + match_component + freshness_component, 1)


def determine_initial_application_state(
    sponsorship_status: SponsorshipStatus, is_target_role: bool
) -> ApplicationState:
    if not is_target_role:
        return ApplicationState.SKIPPED
    if sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP:
        return ApplicationState.SKIPPED
    return ApplicationState.ANALYZED
