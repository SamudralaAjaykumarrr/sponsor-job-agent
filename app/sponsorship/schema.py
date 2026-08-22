"""Phase 7 shared enums/constants for the sponsorship intelligence layer.

Durable design rule restated from CLAUDE.md: everything in this module
describes EMPLOYER HISTORY or CURRENT-ROLE EVIDENCE QUALITY. None of it is a
probability of anything, and none of it may -- by itself -- promote a job's
`sponsorship_status` to CONFIRMED_SPONSOR. Only current-role/current-JD
evidence (app.sponsorship.classifier) can do that. See
app/sponsorship/decision.py for the one place these signals are combined."""

from enum import Enum


class SourceType(str, Enum):
    CURRENT_JOB_DESCRIPTION = "CURRENT_JOB_DESCRIPTION"
    CURRENT_COMPANY_POLICY = "CURRENT_COMPANY_POLICY"
    OFFICIAL_EMPLOYER_CAREERS_PAGE = "OFFICIAL_EMPLOYER_CAREERS_PAGE"
    USCIS_EMPLOYER_DATA = "USCIS_EMPLOYER_DATA"
    DOL_LCA_DATA = "DOL_LCA_DATA"
    PUBLIC_GOVERNMENT_DATA = "PUBLIC_GOVERNMENT_DATA"
    MANUAL_VERIFIED_EVIDENCE = "MANUAL_VERIFIED_EVIDENCE"
    OTHER_REPUTABLE_PUBLIC_SOURCE = "OTHER_REPUTABLE_PUBLIC_SOURCE"


class SourceQuality(str, Enum):
    PRIMARY_CURRENT_ROLE = "PRIMARY_CURRENT_ROLE"
    PRIMARY_EMPLOYER_POLICY = "PRIMARY_EMPLOYER_POLICY"
    PRIMARY_GOVERNMENT = "PRIMARY_GOVERNMENT"
    SECONDARY_REPUTABLE = "SECONDARY_REPUTABLE"
    MANUAL_VERIFIED = "MANUAL_VERIFIED"
    UNVERIFIED = "UNVERIFIED"


# Deterministic, one-directional mapping -- never inferred any other way.
SOURCE_TYPE_TO_QUALITY: dict[SourceType, SourceQuality] = {
    SourceType.CURRENT_JOB_DESCRIPTION: SourceQuality.PRIMARY_CURRENT_ROLE,
    SourceType.CURRENT_COMPANY_POLICY: SourceQuality.PRIMARY_EMPLOYER_POLICY,
    SourceType.OFFICIAL_EMPLOYER_CAREERS_PAGE: SourceQuality.PRIMARY_EMPLOYER_POLICY,
    SourceType.USCIS_EMPLOYER_DATA: SourceQuality.PRIMARY_GOVERNMENT,
    SourceType.DOL_LCA_DATA: SourceQuality.PRIMARY_GOVERNMENT,
    SourceType.PUBLIC_GOVERNMENT_DATA: SourceQuality.PRIMARY_GOVERNMENT,
    SourceType.MANUAL_VERIFIED_EVIDENCE: SourceQuality.MANUAL_VERIFIED,
    SourceType.OTHER_REPUTABLE_PUBLIC_SOURCE: SourceQuality.SECONDARY_REPUTABLE,
}

# Relative, auditable weights -- not a probability, just a fixed multiplier
# used consistently by app.sponsorship.profile's history_score.
SOURCE_QUALITY_WEIGHT: dict[SourceQuality, float] = {
    SourceQuality.PRIMARY_GOVERNMENT: 1.0,
    SourceQuality.PRIMARY_EMPLOYER_POLICY: 0.9,
    SourceQuality.MANUAL_VERIFIED: 0.8,
    SourceQuality.PRIMARY_CURRENT_ROLE: 0.8,
    SourceQuality.SECONDARY_REPUTABLE: 0.3,
    SourceQuality.UNVERIFIED: 0.1,
}


def source_quality_for(source_type: str) -> SourceQuality:
    try:
        st = SourceType(source_type)
    except ValueError:
        return SourceQuality.UNVERIFIED
    return SOURCE_TYPE_TO_QUALITY.get(st, SourceQuality.UNVERIFIED)


class AliasType(str, Enum):
    LEGAL_NAME = "LEGAL_NAME"
    DBA = "DBA"
    BRAND_NAME = "BRAND_NAME"
    FORMER_NAME = "FORMER_NAME"
    SUBSIDIARY_NAME = "SUBSIDIARY_NAME"


class RelationshipType(str, Enum):
    PARENT = "PARENT"
    SUBSIDIARY = "SUBSIDIARY"
    AFFILIATE = "AFFILIATE"
    ACQUIRED = "ACQUIRED"


class HistoricalStrength(str, Enum):
    """Dashboard filter buckets (CLAUDE.md Phase 7 section 30) -- deterministic,
    derived purely from an employer's aggregated evidence. Never overrides a
    job's current-role sponsorship_status; it is a separate, additional filter
    axis only."""

    STRONG_RECENT = "STRONG_RECENT"
    SOME = "SOME"
    OLD = "OLD"
    NONE = "NONE"


class RoleSimilarityTier(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    NONE = "NONE"


class RecencyBucket(str, Enum):
    CURRENT = "CURRENT"
    ONE_YEAR = "ONE_YEAR"
    TWO_YEARS = "TWO_YEARS"
    THREE_TO_FIVE_YEARS = "THREE_TO_FIVE_YEARS"
    OLDER = "OLDER"


RECENCY_WEIGHT: dict[RecencyBucket, float] = {
    RecencyBucket.CURRENT: 1.0,
    RecencyBucket.ONE_YEAR: 0.8,
    RecencyBucket.TWO_YEARS: 0.6,
    RecencyBucket.THREE_TO_FIVE_YEARS: 0.4,
    RecencyBucket.OLDER: 0.15,
}


def recency_bucket(fiscal_year: int | None, as_of_year: int) -> RecencyBucket:
    if fiscal_year is None:
        return RecencyBucket.OLDER
    age = as_of_year - fiscal_year
    if age <= 0:
        return RecencyBucket.CURRENT
    if age == 1:
        return RecencyBucket.ONE_YEAR
    if age == 2:
        return RecencyBucket.TWO_YEARS
    if age <= 5:
        return RecencyBucket.THREE_TO_FIVE_YEARS
    return RecencyBucket.OLDER


class DatasetStatus(str, Enum):
    PENDING = "PENDING"
    IMPORTING = "IMPORTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IdentityReviewStatus(str, Enum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
