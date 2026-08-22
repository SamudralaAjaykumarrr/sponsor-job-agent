"""Derived employer historical sponsorship profile (CLAUDE.md Phase 7
sections 11-15, 52-53). Aggregates a company's `employer_sponsorship_evidence`
rows into a cached `employer_sponsorship_profile` row so job classification
never has to scan raw evidence at request time.

Every metric here describes HISTORY. None of it is labeled or usable as
"probability of sponsorship" -- see history_score's docstring below and
CLAUDE.md Phase 7 section 11/15."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.sponsorship.schema import (
    RECENCY_WEIGHT,
    SOURCE_QUALITY_WEIGHT,
    HistoricalStrength,
    SourceQuality,
    recency_bucket,
)
from app.sponsorship.similarity import is_software_family_soc

# Bounded per-company fetch -- a single employer's filing history is never
# expected to approach the millions-of-rows scale of the full dataset (CLAUDE.md
# Phase 7 section 52: never scan the whole table on a request path). If a
# single employer ever exceeds this, the aggregate is still directionally
# correct (recency-sorted, most-relevant rows first).
_MAX_ROWS_PER_COMPANY = 20_000

_TREND_CHANGE_THRESHOLD = 0.2  # 20% relative change between recent/prior windows


@dataclass
class EmployerProfile:
    company_id: int
    years_with_h1b_activity: int = 0
    most_recent_fiscal_year: Optional[int] = None
    recent_filing_count: int = 0
    historical_filing_count: int = 0
    recent_lca_count: int = 0
    historical_lca_count: int = 0
    recent_occupation_families: list[str] = field(default_factory=list)
    recent_occupation_titles: list[str] = field(default_factory=list)
    recent_states: list[str] = field(default_factory=list)
    continuity_years: int = 0
    trend: str = "STABLE"
    source_coverage: list[str] = field(default_factory=list)
    historical_strength: HistoricalStrength = HistoricalStrength.NONE
    history_score: float = 0.0
    history_reasons: list[str] = field(default_factory=list)
    computed_at: str = ""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_employer_profile(company_id: int, as_of_year: Optional[int] = None) -> EmployerProfile:
    """Pure aggregation function -- deterministic given the same evidence
    rows and as_of_year. Does not persist; see refresh_employer_profile()."""
    year = as_of_year or datetime.now(timezone.utc).year

    with db_session() as conn:
        rows = conn.execute(
            """SELECT fiscal_year, source_type, occupation_code, occupation_title,
                      worksite_state, employer_state, source_quality
               FROM employer_sponsorship_evidence
               WHERE company_id = ?
               ORDER BY fiscal_year DESC
               LIMIT ?""",
            (company_id, _MAX_ROWS_PER_COMPANY),
        ).fetchall()

    if not rows:
        return EmployerProfile(company_id=company_id, historical_strength=HistoricalStrength.NONE,
                                history_reasons=["no sponsorship evidence on file for this employer"],
                                computed_at=utcnow())

    fiscal_years: set[int] = set()
    source_types: set[str] = set()
    recent_count = historical_count = 0
    recent_lca = historical_lca = 0
    recent_states: set[str] = set()
    recent_occupation_titles: list[str] = []
    recent_technical = False
    score = 0.0
    recent_window = (year - 1, year)
    continuity_window = range(year - 3, year + 1)
    continuity_years_seen: set[int] = set()

    for r in rows:
        fy = r["fiscal_year"]
        st = r["source_type"] or ""
        quality = SourceQuality(r["source_quality"]) if r["source_quality"] in SourceQuality._value2member_map_ else SourceQuality.UNVERIFIED
        is_lca = st == "DOL_LCA_DATA"

        historical_count += 1
        if is_lca:
            historical_lca += 1
        if fy is not None:
            fiscal_years.add(fy)
            if fy in continuity_window:
                continuity_years_seen.add(fy)
            if recent_window[0] <= fy <= recent_window[1]:
                recent_count += 1
                if is_lca:
                    recent_lca += 1
                state = r["worksite_state"] or r["employer_state"] or ""
                if state:
                    recent_states.add(state.strip().upper())
                occ_title = (r["occupation_title"] or "").strip()
                if occ_title and occ_title not in recent_occupation_titles and len(recent_occupation_titles) < 10:
                    recent_occupation_titles.append(occ_title)
                if is_software_family_soc(r["occupation_code"] or "") or "software" in occ_title.lower() or "developer" in occ_title.lower():
                    recent_technical = True
        source_types.add(st)

        bucket = recency_bucket(fy, year)
        score += RECENCY_WEIGHT[bucket] * SOURCE_QUALITY_WEIGHT.get(quality, 0.1)

    continuity_years = len(continuity_years_seen)
    score += continuity_years * 3.0
    score = round(score, 2)

    most_recent_fy = max(fiscal_years) if fiscal_years else None

    recent_2yr = sum(1 for r in rows if r["fiscal_year"] is not None and year - 1 <= r["fiscal_year"] <= year)
    prior_2yr = sum(1 for r in rows if r["fiscal_year"] is not None and year - 3 <= r["fiscal_year"] <= year - 2)
    trend = "STABLE"
    if prior_2yr == 0 and recent_2yr > 0:
        trend = "UP"
    elif prior_2yr > 0:
        change = (recent_2yr - prior_2yr) / prior_2yr
        if change >= _TREND_CHANGE_THRESHOLD:
            trend = "UP"
        elif change <= -_TREND_CHANGE_THRESHOLD:
            trend = "DOWN"

    reasons = [
        f"{historical_count} total evidence record(s) across {len(fiscal_years)} fiscal year(s)",
    ]
    if most_recent_fy is not None:
        reasons.append(f"most recent fiscal year on file: {most_recent_fy}")
    if recent_count:
        reasons.append(f"{recent_count} record(s) in the last 2 fiscal years")
    if continuity_years:
        reasons.append(f"filed in {continuity_years} of the last 4 fiscal years")
    if recent_technical:
        reasons.append("recent filings include software/computer occupations")

    if recent_count >= 3 and continuity_years >= 2 and recent_technical:
        strength = HistoricalStrength.STRONG_RECENT
    elif recent_count >= 1 or (most_recent_fy is not None and most_recent_fy >= year - 2):
        strength = HistoricalStrength.SOME
    elif historical_count >= 1:
        strength = HistoricalStrength.OLD
    else:
        strength = HistoricalStrength.NONE

    return EmployerProfile(
        company_id=company_id,
        years_with_h1b_activity=len(fiscal_years),
        most_recent_fiscal_year=most_recent_fy,
        recent_filing_count=recent_count,
        historical_filing_count=historical_count,
        recent_lca_count=recent_lca,
        historical_lca_count=historical_lca,
        recent_occupation_families=["software_engineering"] if recent_technical else [],
        recent_occupation_titles=recent_occupation_titles,
        recent_states=sorted(recent_states),
        continuity_years=continuity_years,
        trend=trend,
        source_coverage=sorted(source_types),
        historical_strength=strength,
        history_score=score,
        history_reasons=reasons,
        computed_at=utcnow(),
    )


def refresh_employer_profile(company_id: int, as_of_year: Optional[int] = None) -> EmployerProfile:
    """Recomputes and upserts the cached profile row. Call after any new
    evidence import touching this company (CLAUDE.md Phase 7 section 53)."""
    import json

    profile = compute_employer_profile(company_id, as_of_year=as_of_year)
    with db_session() as conn:
        conn.execute(
            """INSERT INTO employer_sponsorship_profile
                 (company_id, years_with_h1b_activity, most_recent_fiscal_year, recent_filing_count,
                  historical_filing_count, recent_lca_count, historical_lca_count,
                  recent_occupation_families, recent_occupation_titles, recent_states, continuity_years,
                  trend, source_coverage, historical_strength, history_score, history_reasons, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET
                 years_with_h1b_activity = excluded.years_with_h1b_activity,
                 most_recent_fiscal_year = excluded.most_recent_fiscal_year,
                 recent_filing_count = excluded.recent_filing_count,
                 historical_filing_count = excluded.historical_filing_count,
                 recent_lca_count = excluded.recent_lca_count,
                 historical_lca_count = excluded.historical_lca_count,
                 recent_occupation_families = excluded.recent_occupation_families,
                 recent_occupation_titles = excluded.recent_occupation_titles,
                 recent_states = excluded.recent_states,
                 continuity_years = excluded.continuity_years,
                 trend = excluded.trend,
                 source_coverage = excluded.source_coverage,
                 historical_strength = excluded.historical_strength,
                 history_score = excluded.history_score,
                 history_reasons = excluded.history_reasons,
                 computed_at = excluded.computed_at""",
            (
                profile.company_id, profile.years_with_h1b_activity, profile.most_recent_fiscal_year,
                profile.recent_filing_count, profile.historical_filing_count, profile.recent_lca_count,
                profile.historical_lca_count, json.dumps(profile.recent_occupation_families),
                json.dumps(profile.recent_occupation_titles),
                json.dumps(profile.recent_states), profile.continuity_years, profile.trend,
                json.dumps(profile.source_coverage), profile.historical_strength.value,
                profile.history_score, json.dumps(profile.history_reasons), profile.computed_at,
            ),
        )

    from app.sponsorship.acquisition_integration import sync_acquisition_signal

    sync_acquisition_signal(company_id, profile)
    return profile


def get_cached_profile(company_id: int) -> Optional[EmployerProfile]:
    import json

    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM employer_sponsorship_profile WHERE company_id = ?", (company_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    return EmployerProfile(
        company_id=d["company_id"],
        years_with_h1b_activity=d["years_with_h1b_activity"] or 0,
        most_recent_fiscal_year=d["most_recent_fiscal_year"],
        recent_filing_count=d["recent_filing_count"] or 0,
        historical_filing_count=d["historical_filing_count"] or 0,
        recent_lca_count=d["recent_lca_count"] or 0,
        historical_lca_count=d["historical_lca_count"] or 0,
        recent_occupation_families=json.loads(d.get("recent_occupation_families") or "[]"),
        recent_occupation_titles=json.loads(d.get("recent_occupation_titles") or "[]"),
        recent_states=json.loads(d.get("recent_states") or "[]"),
        continuity_years=d["continuity_years"] or 0,
        trend=d.get("trend") or "STABLE",
        source_coverage=json.loads(d.get("source_coverage") or "[]"),
        historical_strength=HistoricalStrength(d.get("historical_strength") or "NONE"),
        history_score=d.get("history_score") or 0.0,
        history_reasons=json.loads(d.get("history_reasons") or "[]"),
        computed_at=d.get("computed_at") or "",
    )


def get_or_compute_profile(company_id: int, as_of_year: Optional[int] = None) -> EmployerProfile:
    cached = get_cached_profile(company_id)
    if cached is not None:
        return cached
    return refresh_employer_profile(company_id, as_of_year=as_of_year)
