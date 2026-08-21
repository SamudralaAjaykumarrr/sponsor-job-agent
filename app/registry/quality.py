"""Deterministic, rule-based portal confidence scoring. Every point is tied to
a concrete, checkable signal with a human-readable reason -- never an opaque
"AI probability". See CLAUDE.md Phase 4 section 16."""

from dataclasses import dataclass, field

from app.providers.capabilities import SupportLevel
from app.registry.models import CareerPortal, IdentityStatus, PortalStatus

# Each weight is a standalone, auditable signal. They needn't sum to exactly
# 100 -- a portal can lack one signal (e.g. never yet polled) and still be a
# very strong candidate.
_WEIGHTS = {
    "official_link": 20,
    "provider_recognized": 15,
    "tenant_extracted": 15,
    "endpoint_verified": 20,
    "identity_matched": 15,
    "recent_success": 8,
    "recent_jobs_observed": 7,
}


@dataclass(frozen=True)
class QualityScore:
    score: int
    reasons: list[str] = field(default_factory=list)


def score_portal(portal: CareerPortal, *, has_official_link_provenance: bool) -> QualityScore:
    score = 0
    reasons: list[str] = []

    if has_official_link_provenance:
        score += _WEIGHTS["official_link"]
        reasons.append("linked directly from an official company-controlled source")

    if portal.support_level in (SupportLevel.FULL, SupportLevel.PARTIAL):
        score += _WEIGHTS["provider_recognized"]
        reasons.append(f"provider recognized as {portal.provider} ({portal.support_level.value})")

    if portal.tenant_identifier:
        score += _WEIGHTS["tenant_extracted"]
        reasons.append("tenant identifier extracted deterministically")

    if portal.verification_status in (PortalStatus.VERIFIED, PortalStatus.ACTIVE):
        score += _WEIGHTS["endpoint_verified"]
        reasons.append("provider endpoint verified with a live response")

    if portal.identity_status == IdentityStatus.MATCHED:
        score += _WEIGHTS["identity_matched"]
        reasons.append("company identity matched from provider-returned data")

    if portal.last_success_at:
        score += _WEIGHTS["recent_success"]
        reasons.append("recent successful poll")

    if portal.average_job_yield and portal.average_job_yield > 0:
        score += _WEIGHTS["recent_jobs_observed"]
        reasons.append("recent jobs observed")

    return QualityScore(score=min(score, 100), reasons=reasons)
