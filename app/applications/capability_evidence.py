"""Dated capability-evidence tracking (CLAUDE.md Phase 11 sections 42-43).

`app.applications.browser_capability_matrix` remains the hand-curated,
human-reviewed matrix shown in docs/the dashboard -- this module is the
underlying, queryable EVIDENCE store that matrix should be derived from
going forward: one row per (provider, capability) pair, each carrying its
own verification type and observed-at date, so staleness can be computed
mechanically instead of by memory. Recording evidence here never
auto-disables a known-safe capability (CLAUDE.md Phase 11 section 43) --
staleness is surfaced for review, not acted on automatically."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from app import config
from app.db import db_session


class EvidenceVerificationType(str, Enum):
    LIVE_PUBLIC = "LIVE_PUBLIC"
    FIXTURE = "FIXTURE"
    NOT_TESTED = "NOT_TESTED"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_evidence(
    provider: str, capability: str, verification_type: EvidenceVerificationType, *,
    notes: str = "", source_domain: str = "", parser_version: str = "1",
    observed_at: Optional[str] = None,
) -> dict:
    """Upserts the single current evidence row for (provider, capability).
    Only ever called from a genuine observation site (a live browser
    validation run, or a fixture-only test explicitly recording FIXTURE) --
    never called speculatively."""
    vtype = verification_type.value if isinstance(verification_type, EvidenceVerificationType) else verification_type
    now = utcnow()
    observed = observed_at or now
    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM capability_evidence_records WHERE provider = ? AND capability = ?",
            (provider, capability),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO capability_evidence_records
                   (provider, capability, verification_type, observed_at, notes, source_domain, parser_version,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (provider, capability, vtype, observed, notes, source_domain, parser_version, now, now),
            )
        else:
            conn.execute(
                """UPDATE capability_evidence_records
                   SET verification_type = ?, observed_at = ?, notes = ?, source_domain = ?, parser_version = ?,
                       updated_at = ?
                   WHERE provider = ? AND capability = ?""",
                (vtype, observed, notes, source_domain, parser_version, now, provider, capability),
            )
    return get_evidence(provider, capability)


def get_evidence(provider: str, capability: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM capability_evidence_records WHERE provider = ? AND capability = ?",
            (provider, capability),
        ).fetchone()
        return dict(row) if row else None


def list_evidence(provider: Optional[str] = None, limit: int = 500) -> list[dict]:
    query = "SELECT * FROM capability_evidence_records"
    params: list = []
    if provider:
        query += " WHERE provider = ?"
        params.append(provider)
    query += " ORDER BY provider, capability LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@dataclass
class StalenessResult:
    row: dict
    stale: bool
    age_days: float


def evidence_age_days(observed_at: str) -> float:
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return float("inf")
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - observed).total_seconds() / 86400.0


def is_stale(row: dict, max_age_days: Optional[int] = None) -> bool:
    """CLAUDE.md Phase 11 section 43: only LIVE_PUBLIC evidence can go
    stale -- FIXTURE and NOT_TESTED rows describe something that isn't
    time-sensitive real-world observation, so staleness doesn't apply to
    them."""
    if row.get("verification_type") != EvidenceVerificationType.LIVE_PUBLIC.value:
        return False
    max_age = max_age_days if max_age_days is not None else config.CAPABILITY_EVIDENCE_MAX_AGE_DAYS
    return evidence_age_days(row["observed_at"]) > max_age


def list_stale(max_age_days: Optional[int] = None) -> list[StalenessResult]:
    results = []
    for row in list_evidence():
        if is_stale(row, max_age_days):
            results.append(StalenessResult(row=row, stale=True, age_days=evidence_age_days(row["observed_at"])))
    return results
