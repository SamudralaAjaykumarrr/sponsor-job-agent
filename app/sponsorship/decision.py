"""Sponsorship decision engine (CLAUDE.md Phase 7 sections 15, 19-24, 47).
The ONE place current-role evidence (app.sponsorship.classifier) and
historical employer evidence (app.sponsorship.profile) are combined into a
final job sponsorship_status, with a persisted, versioned audit trail.

Hard invariants (never violate these):
  - Current-role NO_SPONSORSHIP and CONFIRMED_SPONSOR are always final --
    historical evidence is attached only as extra explanatory `reasons`,
    never changes the status.
  - Historical evidence can only ever move UNKNOWN -> LIKELY_SPONSOR. It can
    never produce CONFIRMED_SPONSOR and never overrides NO_SPONSORSHIP.
  - A conflict (positive+negative current-role language) always resolves to
    LIKELY_SPONSOR (review-only, never auto-apply) -- never a hard skip,
    never CONFIRMED.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.models import SponsorshipStatus
from app.sponsorship.classifier import CLASSIFIER_VERSION, ClassificationResult, classify_sponsorship_detailed
from app.sponsorship.identity import IdentityMatch, resolve_company
from app.sponsorship.profile import EmployerProfile, get_or_compute_profile
from app.sponsorship.schema import HistoricalStrength, RoleSimilarityTier
from app.sponsorship.similarity import location_similarity, role_similarity

# CLAUDE.md Phase 7 section 43 example C/D: only a STRONG_RECENT employer
# history combined with at least MODERATE role similarity is enough to move
# a silent JD from UNKNOWN to LIKELY_SPONSOR. Anything weaker deterministically
# stays UNKNOWN -- it is never enough to imply CONFIRMED either way.
_LIKELY_STRENGTH_REQUIRED = {HistoricalStrength.STRONG_RECENT}
_LIKELY_ROLE_TIER_REQUIRED = {RoleSimilarityTier.STRONG, RoleSimilarityTier.MODERATE}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SponsorshipDecision:
    job_id: int
    status: SponsorshipStatus
    evidence_text: str
    decision_version: int
    classifier_version: str
    jd_fingerprint: str
    current_job_evidence: list[str] = field(default_factory=list)
    historical_evidence_summary: dict = field(default_factory=dict)
    company_policy_evidence: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    blocking_reason: str = ""
    conflict: bool = False
    created_at: str = ""


def compute_jd_fingerprint(title: str, company: str, description: str) -> str:
    normalized = f"{(title or '').strip().lower()}|{(company or '').strip().lower()}|{(description or '').strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]


def _historical_context(
    company: str, current_title: str, current_state: str,
) -> tuple[Optional[EmployerProfile], IdentityMatch, RoleSimilarityTier, list[str]]:
    match = resolve_company(company)
    if match.company_id is None:
        return None, match, RoleSimilarityTier.NONE, []

    profile = get_or_compute_profile(match.company_id)
    if not profile.recent_occupation_titles:
        role_tier = RoleSimilarityTier.NONE
        role_reasons = ["no occupation title data available in historical evidence"]
    else:
        # Compare against every recent occupation title on file and keep the
        # strongest match -- deterministic, no single title is privileged.
        best_tier = RoleSimilarityTier.NONE
        best_reasons: list[str] = []
        tier_rank = {RoleSimilarityTier.NONE: 0, RoleSimilarityTier.WEAK: 1,
                     RoleSimilarityTier.MODERATE: 2, RoleSimilarityTier.STRONG: 3}
        for occ_title in profile.recent_occupation_titles:
            tier, reasons_for_title = role_similarity(current_title, occ_title)
            if tier_rank[tier] > tier_rank[best_tier]:
                best_tier, best_reasons = tier, reasons_for_title
        role_tier, role_reasons = best_tier, best_reasons or ["no title similarity found against employer's recent occupation titles"]

    loc_match, loc_reason = location_similarity(current_state, set(profile.recent_states))
    reasons = list(role_reasons) + [loc_reason]
    return profile, match, role_tier, reasons


def _apply_historical_upgrade(
    result: ClassificationResult, company: str, current_title: str, current_state: str,
) -> tuple[SponsorshipStatus, list[str], dict]:
    """Only ever called when current-role evidence is UNKNOWN. May upgrade to
    LIKELY_SPONSOR; never produces anything else."""
    profile, match, role_tier, context_reasons = _historical_context(company, current_title, current_state)
    summary: dict = {
        "company_match": match.matched_via,
        "company_id": match.company_id,
    }
    if profile is None:
        summary["historical_strength"] = HistoricalStrength.NONE.value
        return SponsorshipStatus.UNKNOWN, ["no employer identity match -- cannot evaluate historical evidence"], summary

    summary.update({
        "historical_strength": profile.historical_strength.value,
        "history_score": profile.history_score,
        "recent_filing_count": profile.recent_filing_count,
        "continuity_years": profile.continuity_years,
        "most_recent_fiscal_year": profile.most_recent_fiscal_year,
        "role_similarity": role_tier.value,
    })

    reasons = list(profile.history_reasons) + context_reasons

    if profile.historical_strength in _LIKELY_STRENGTH_REQUIRED and role_tier in _LIKELY_ROLE_TIER_REQUIRED:
        reasons.append(
            f"employer historical strength={profile.historical_strength.value} with role similarity={role_tier.value} "
            "meets the deterministic threshold for LIKELY_SPONSOR"
        )
        return SponsorshipStatus.LIKELY_SPONSOR, reasons, summary

    reasons.append(
        f"employer historical strength={profile.historical_strength.value}, role similarity={role_tier.value} -- "
        "below the deterministic LIKELY_SPONSOR threshold; historical evidence NEVER implies CONFIRMED"
    )
    return SponsorshipStatus.UNKNOWN, reasons, summary


def decide_sponsorship(
    title: str, company: str, description: str, location_state: str = "",
) -> SponsorshipDecision:
    """Stateless decision computation (no job_id / persistence) -- used by
    both persist_decision() below and anywhere that just needs an
    explanation (e.g. a "preview" API) without writing an audit row."""
    result = classify_sponsorship_detailed(description, company)
    fingerprint = compute_jd_fingerprint(title, company, description)

    historical_summary: dict = {}
    final_status = result.status
    reasons = list(result.reasons)

    if result.status == SponsorshipStatus.UNKNOWN:
        final_status, hist_reasons, historical_summary = _apply_historical_upgrade(
            result, company, title, location_state,
        )
        reasons += hist_reasons
    elif result.status == SponsorshipStatus.LIKELY_SPONSOR:
        # Current-role evidence already yielded LIKELY (conditional, conflict,
        # or local known-sponsors match) -- attach historical context as
        # informational reasons only, never change the status.
        match = resolve_company(company)
        if match.company_id is not None:
            profile = get_or_compute_profile(match.company_id)
            historical_summary = {
                "company_match": match.matched_via, "company_id": match.company_id,
                "historical_strength": profile.historical_strength.value,
                "history_score": profile.history_score,
            }
            reasons.append(f"additional context: employer historical strength={profile.historical_strength.value}")

    evidence_text = result.evidence_text

    return SponsorshipDecision(
        job_id=0,
        status=final_status,
        evidence_text=evidence_text,
        decision_version=1,
        classifier_version=CLASSIFIER_VERSION,
        jd_fingerprint=fingerprint,
        current_job_evidence=result.positive_matches + result.negative_matches + result.conditional_matches,
        historical_evidence_summary=historical_summary,
        conflicts=[result.evidence_text] if result.conflict else [],
        reasons=reasons,
        blocking_reason=result.blocking_reason if final_status != SponsorshipStatus.CONFIRMED_SPONSOR else "",
        conflict=result.conflict,
        created_at=utcnow(),
    )


def get_latest_decision(job_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM sponsorship_decisions WHERE job_id = ? ORDER BY decision_version DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None


def persist_decision(job_id: int, title: str, company: str, description: str, location_state: str = "") -> SponsorshipDecision:
    """Computes a decision and persists a NEW versioned audit row only if the
    JD fingerprint (or classifier version) changed since the last recorded
    decision for this job -- re-running on unchanged input is a no-op read
    (CLAUDE.md Phase 7 sections 21-24)."""
    decision = decide_sponsorship(title, company, description, location_state)
    existing = get_latest_decision(job_id)

    if existing and existing["jd_fingerprint"] == decision.jd_fingerprint and existing["classifier_version"] == CLASSIFIER_VERSION:
        decision.decision_version = existing["decision_version"]
        decision.created_at = existing["created_at"]
        return decision

    decision.decision_version = (existing["decision_version"] + 1) if existing else 1
    decision.job_id = job_id

    with db_session() as conn:
        conn.execute(
            """INSERT INTO sponsorship_decisions
                 (job_id, status, decision_version, classifier_version, jd_fingerprint,
                  current_job_evidence, historical_evidence_summary, company_policy_evidence,
                  conflicts, reasons, blocking_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, decision.status.value, decision.decision_version, decision.classifier_version,
                decision.jd_fingerprint, json.dumps(decision.current_job_evidence),
                json.dumps(decision.historical_evidence_summary), json.dumps(decision.company_policy_evidence),
                json.dumps(decision.conflicts), json.dumps(decision.reasons), decision.blocking_reason,
                decision.created_at,
            ),
        )
    return decision


def list_decision_history(job_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM sponsorship_decisions WHERE job_id = ? ORDER BY decision_version ASC", (job_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for field_name in ("current_job_evidence", "historical_evidence_summary", "company_policy_evidence",
                                "conflicts", "reasons"):
                try:
                    d[field_name] = json.loads(d.get(field_name) or ("{}" if field_name == "historical_evidence_summary" else "[]"))
                except (ValueError, TypeError):
                    d[field_name] = {} if field_name == "historical_evidence_summary" else []
            out.append(d)
        return out
