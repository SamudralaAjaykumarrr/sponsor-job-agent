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
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app import config
from app.db import db_session


class EvidenceVerificationType(str, Enum):
    # CLAUDE.md Phase 11's original vocabulary -- kept unchanged so every
    # existing recorded row and call site (scripts/phase11_live_validation.py)
    # keeps meaning exactly what it always meant.
    LIVE_PUBLIC = "LIVE_PUBLIC"
    FIXTURE = "FIXTURE"
    NOT_TESTED = "NOT_TESTED"
    # CLAUDE.md Phase 12 section 41: a finer-grained vocabulary for HOW a
    # capability was verified, distinct from the FIXTURE/NOT_TESTED cases
    # above. STATIC_HTML is weaker evidence than a real driven browser (no
    # JS execution, no dynamic form/SPA behavior exercised); REAL_BROWSER is
    # one genuine Playwright-driven observation; REAL_BROWSER_REPEATED means
    # the SAME (provider, capability) has now been genuinely re-observed via
    # a real browser more than once (see `repeat_count` below) -- repeated
    # evidence strengthens confidence but never auto-promotes a capability
    # that hasn't actually been re-checked.
    STATIC_HTML = "STATIC_HTML"
    REAL_BROWSER = "REAL_BROWSER"
    REAL_BROWSER_REPEATED = "REAL_BROWSER_REPEATED"


# Verification types whose staleness clock actually matters -- a real,
# time-sensitive observation of live provider behavior. FIXTURE/NOT_TESTED/
# STATIC_HTML describe something that isn't "does the real live page still
# behave this way", so staleness doesn't apply to them (CLAUDE.md Phase 11
# section 43, extended for Phase 12's new real-browser-shaped types).
_TIME_SENSITIVE_TYPES = frozenset({
    EvidenceVerificationType.LIVE_PUBLIC.value,
    EvidenceVerificationType.REAL_BROWSER.value,
    EvidenceVerificationType.REAL_BROWSER_REPEATED.value,
})


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
            "SELECT id, verification_type, repeat_count FROM capability_evidence_records "
            "WHERE provider = ? AND capability = ?",
            (provider, capability),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO capability_evidence_records
                   (provider, capability, verification_type, observed_at, notes, source_domain, parser_version,
                    repeat_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (provider, capability, vtype, observed, notes, source_domain, parser_version, now, now),
            )
        else:
            # CLAUDE.md Phase 12 section 41: repeated REAL_BROWSER-family
            # evidence strengthens confidence -- a genuine re-observation
            # via a real browser bumps repeat_count and, from the 2nd
            # consecutive real-browser observation onward, is stored as
            # REAL_BROWSER_REPEATED rather than a plain REAL_BROWSER (never
            # inflated by a FIXTURE/NOT_TESTED/STATIC_HTML re-check, which
            # resets the streak instead -- weaker evidence never counts
            # toward "repeated real-browser confidence").
            prior_type = existing["verification_type"]
            prior_count = existing["repeat_count"] or 1
            if vtype in _TIME_SENSITIVE_TYPES and prior_type in _TIME_SENSITIVE_TYPES:
                repeat_count = prior_count + 1
                stored_vtype = EvidenceVerificationType.REAL_BROWSER_REPEATED.value if repeat_count >= 2 else vtype
            else:
                repeat_count = 1
                stored_vtype = vtype
            conn.execute(
                """UPDATE capability_evidence_records
                   SET verification_type = ?, observed_at = ?, notes = ?, source_domain = ?, parser_version = ?,
                       repeat_count = ?, updated_at = ?
                   WHERE provider = ? AND capability = ?""",
                (stored_vtype, observed, notes, source_domain, parser_version, repeat_count, now, provider, capability),
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
    """CLAUDE.md Phase 11 section 43 (extended Phase 12 section 42): only a
    real, time-sensitive observation of live provider behavior can go stale
    -- FIXTURE/NOT_TESTED/STATIC_HTML rows describe something that isn't
    time-sensitive real-world observation, so staleness doesn't apply to
    them. Never automatically disables the capability -- staleness is only
    ever surfaced for review (doctor/dashboard)."""
    if row.get("verification_type") not in _TIME_SENSITIVE_TYPES:
        return False
    max_age = max_age_days if max_age_days is not None else config.CAPABILITY_EVIDENCE_MAX_AGE_DAYS
    return evidence_age_days(row["observed_at"]) > max_age


def list_stale(max_age_days: Optional[int] = None) -> list[StalenessResult]:
    results = []
    for row in list_evidence():
        if is_stale(row, max_age_days):
            results.append(StalenessResult(row=row, stale=True, age_days=evidence_age_days(row["observed_at"])))
    return results
