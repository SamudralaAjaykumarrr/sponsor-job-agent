"""Sponsorship review queue (CLAUDE.md Phase 7 section 31). Surfaces
LIKELY_SPONSOR jobs (review-only, never auto-applied) ordered by fit +
freshness + employer history strength, with an explicit "what's missing"
explanation per job."""

from dataclasses import dataclass, field

from app.db import db_session
from app.sponsorship.decision import get_latest_decision


@dataclass
class ReviewQueueItem:
    job_id: int
    title: str
    company: str
    location: str
    work_arrangement: str
    technical_match_score: float
    freshness_tier: str
    priority_score: float
    historical_strength: str = "NONE"
    missing_confirmation: str = ""
    reasons: list[str] = field(default_factory=list)


_STRENGTH_RANK = {"STRONG_RECENT": 3, "SOME": 2, "OLD": 1, "NONE": 0}


def build_review_queue(limit: int = 100) -> list[ReviewQueueItem]:
    import json

    with db_session() as conn:
        rows = conn.execute(
            """SELECT id, title, company, location, work_arrangement, technical_match_score,
                      freshness_tier, priority_score
               FROM jobs
               WHERE sponsorship_status = 'LIKELY_SPONSOR'
               ORDER BY priority_score DESC, first_seen_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        decision = get_latest_decision(d["id"])
        historical_strength = "NONE"
        reasons: list[str] = []
        missing = "Employer sponsorship not explicitly confirmed for this specific role."
        if decision:
            try:
                summary = json.loads(decision.get("historical_evidence_summary") or "{}")
            except (ValueError, TypeError):
                summary = {}
            historical_strength = summary.get("historical_strength", "NONE")
            try:
                reasons = json.loads(decision.get("reasons") or "[]")
            except (ValueError, TypeError):
                reasons = []
            missing = decision.get("blocking_reason") or missing

        items.append(ReviewQueueItem(
            job_id=d["id"], title=d["title"], company=d["company"], location=d["location"] or "",
            work_arrangement=d["work_arrangement"], technical_match_score=d["technical_match_score"] or 0.0,
            freshness_tier=d["freshness_tier"], priority_score=d["priority_score"] or 0.0,
            historical_strength=historical_strength, missing_confirmation=missing, reasons=reasons,
        ))

    items.sort(
        key=lambda i: (_STRENGTH_RANK.get(i.historical_strength, 0), i.technical_match_score, i.priority_score),
        reverse=True,
    )
    return items
