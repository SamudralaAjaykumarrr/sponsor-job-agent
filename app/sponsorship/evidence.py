"""Employer sponsorship evidence storage (CLAUDE.md Phase 6 section 27) --
a storage/repo foundation for Phase 7 sponsorship intelligence. This module
is intentionally NOT wired into app.sponsorship.classifier.classify_sponsorship()
at all: that function is the sponsorship hard-gate and continues to read
ONLY the local known-sponsors reference list + the JD text itself, exactly
as before Phase 6.

Durable rule (CLAUDE.md, restated here since it is the single most
important constraint on this table): a row here is evidence about a
COMPANY's history (e.g. "this company filed N H-1B petitions in fiscal year
Y"), never proof that a SPECIFIC CURRENT job posting is CONFIRMED_SPONSOR.
Historical sponsorship may influence which companies are worth verifying/
polling more (see app.registry.acquisition_priority), but it must never
promote a job's sponsorship_status by itself -- only the JD's own text can
do that (app.sponsorship.classifier)."""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from app.db import db_session


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SponsorshipEvidence(BaseModel):
    id: Optional[int] = None
    company_id: Optional[int] = None
    company_name_raw: str
    source: str  # e.g. "USER_SUPPLIED", "PUBLIC_DISCLOSURE_DATA", "THIRD_PARTY_AGGREGATOR"
    source_url: str = ""
    fiscal_year: Optional[int] = None
    petition_type: str = ""
    job_title: str = ""
    location: str = ""
    observed_at: str = Field(default_factory=utcnow)
    confidence: int = 0
    source_quality: str = ""
    imported_at: str = Field(default_factory=utcnow)
    notes: str = ""


def record_evidence(evidence: SponsorshipEvidence) -> int:
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO employer_sponsorship_evidence
                 (company_id, company_name_raw, source, source_url, fiscal_year, petition_type,
                  job_title, location, observed_at, confidence, source_quality, imported_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence.company_id, evidence.company_name_raw, evidence.source, evidence.source_url,
                evidence.fiscal_year, evidence.petition_type, evidence.job_title, evidence.location,
                evidence.observed_at, evidence.confidence, evidence.source_quality, evidence.imported_at,
                evidence.notes,
            ),
        )
        return cur.lastrowid


def list_evidence_for_company(company_id: int, limit: int = 100) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM employer_sponsorship_evidence WHERE company_id = ? ORDER BY observed_at DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_evidence_by_name(company_name: str, limit: int = 100) -> list[dict]:
    """Lookup by raw name text -- useful before a company row even exists in
    registry_companies (evidence may be imported ahead of registry
    acquisition discovering the company)."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM employer_sponsorship_evidence WHERE lower(company_name_raw) = lower(?) "
            "ORDER BY observed_at DESC LIMIT ?",
            (company_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def has_any_evidence(company_name: str) -> bool:
    return len(list_evidence_by_name(company_name, limit=1)) > 0


def count_evidence(limit_check: int = 1) -> int:
    with db_session() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM employer_sponsorship_evidence").fetchone()["c"]
