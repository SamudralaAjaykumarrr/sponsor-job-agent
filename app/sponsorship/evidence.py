"""Employer sponsorship evidence storage (CLAUDE.md Phase 6 section 27,
extended by Phase 7 sections 2-4). A storage/repo layer for HISTORY about a
COMPANY -- never proof that a SPECIFIC CURRENT job posting is
CONFIRMED_SPONSOR.

Durable rule (restated, most important constraint on this table): a row here
is evidence about a COMPANY's history (e.g. "this company filed N H-1B
petitions in fiscal year Y"). `app.sponsorship.classifier` (the pure
current-role pattern matcher) never imports this module. The one place
historical evidence is allowed to influence a job's sponsorship_status is
`app.sponsorship.decision.decide_sponsorship()`, and even there it can only
ever upgrade UNKNOWN -> LIKELY_SPONSOR -- never produce CONFIRMED_SPONSOR,
never override NO_SPONSORSHIP, never downgrade CONFIRMED_SPONSOR.

Never stores beneficiary/worker names or other immigration-filing PII
(CLAUDE.md Phase 7 section 37) -- only employer/role/location/aggregate
fields."""

import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from app.db import db_session
from app.registry.normalize import normalize_company_name, normalize_domain
from app.sponsorship.schema import SourceQuality, SourceType, source_quality_for

# Bounded -- never store an unbounded raw snippet.
_SNIPPET_MAX_CHARS = 500


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SponsorshipEvidence(BaseModel):
    id: Optional[int] = None
    company_id: Optional[int] = None
    company_name_raw: str
    company_normalized_name: str = ""
    company_domain: str = ""

    source: str = ""  # legacy free-text label, kept for Phase 6 backward compatibility
    source_type: str = ""  # app.sponsorship.schema.SourceType value
    source_url: str = ""
    source_record_id: str = ""
    dataset_id: Optional[int] = None

    fiscal_year: Optional[int] = None
    filing_date: Optional[str] = None
    petition_type: str = ""
    visa_class: str = ""
    job_title: str = ""
    occupation_code: str = ""
    occupation_title: str = ""
    worksite_city: str = ""
    worksite_state: str = ""
    employer_city: str = ""
    employer_state: str = ""
    location: str = ""  # legacy free-text field, kept for Phase 6 backward compatibility
    status_outcome: str = ""
    count_value: Optional[int] = None

    observed_at: str = Field(default_factory=utcnow)
    confidence: int = 0
    source_quality: str = ""
    imported_at: str = Field(default_factory=utcnow)
    raw_source_fingerprint: str = ""
    snippet: str = ""
    notes: str = ""

    def model_post_init(self, __context) -> None:
        if not self.company_normalized_name and self.company_name_raw:
            self.company_normalized_name = normalize_company_name(self.company_name_raw)
        if self.company_domain:
            self.company_domain = normalize_domain(self.company_domain)
        if not self.source_quality and self.source_type:
            self.source_quality = source_quality_for(self.source_type).value
        if self.snippet:
            self.snippet = self.snippet[:_SNIPPET_MAX_CHARS]


def compute_fingerprint(*parts: str) -> str:
    """Deterministic idempotency key for a raw evidence row, used when the
    source dataset itself provides no stable per-record id."""
    joined = "|".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


_INSERT_COLUMNS = [
    "company_id", "company_name_raw", "company_normalized_name", "company_domain",
    "source", "source_type", "source_url", "source_record_id", "dataset_id",
    "fiscal_year", "filing_date", "petition_type", "visa_class", "job_title",
    "occupation_code", "occupation_title", "worksite_city", "worksite_state",
    "employer_city", "employer_state", "location", "status_outcome", "count_value",
    "observed_at", "confidence", "source_quality", "imported_at",
    "raw_source_fingerprint", "snippet", "notes",
]


def record_evidence(evidence: SponsorshipEvidence) -> int:
    with db_session() as conn:
        cur = conn.execute(
            f"""INSERT INTO employer_sponsorship_evidence ({", ".join(_INSERT_COLUMNS)})
                VALUES ({", ".join("?" for _ in _INSERT_COLUMNS)})""",
            [getattr(evidence, c) for c in _INSERT_COLUMNS],
        )
        return cur.lastrowid


def record_evidence_idempotent(evidence: SponsorshipEvidence) -> tuple[int, bool]:
    """Idempotent insert keyed on (dataset_id, source_record_id) when both are
    present -- re-importing the identical dataset never creates duplicate
    rows (CLAUDE.md Phase 7 sections 5/45). Returns (id, created)."""
    if evidence.dataset_id is not None and evidence.source_record_id:
        with db_session() as conn:
            existing = conn.execute(
                "SELECT id FROM employer_sponsorship_evidence "
                "WHERE dataset_id = ? AND source_record_id = ? AND source_record_id != ''",
                (evidence.dataset_id, evidence.source_record_id),
            ).fetchone()
            if existing:
                return existing["id"], False
    return record_evidence(evidence), True


def bulk_record_evidence_idempotent(rows: list[SponsorshipEvidence]) -> tuple[int, int]:
    """Batched idempotent insert -- ONE transaction for the whole batch
    (CLAUDE.md Phase 7 section 7: "batched", "transactions") rather than one
    connection per row. Idempotency key is (dataset_id, source_record_id)
    when both are present, checked via SELECT inside the same transaction so
    a re-run of the identical batch is a safe no-op (section 5/45). Returns
    (created, skipped)."""
    created = skipped = 0
    with db_session() as conn:
        for evidence in rows:
            if evidence.dataset_id is not None and evidence.source_record_id:
                # The redundant `AND source_record_id != ''` lets SQLite's
                # planner prove this matches the partial unique index
                # `idx_sponsorship_evidence_source_record` even with bound
                # parameters (it can't infer that from `= ?` alone) -- without
                # it this degrades to a full table scan per row, which is
                # O(n^2) over a large import. Verified via EXPLAIN QUERY PLAN
                # and scripts/sponsorship_benchmark.py.
                existing = conn.execute(
                    "SELECT id FROM employer_sponsorship_evidence "
                    "WHERE dataset_id = ? AND source_record_id = ? AND source_record_id != ''",
                    (evidence.dataset_id, evidence.source_record_id),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
            conn.execute(
                f"""INSERT INTO employer_sponsorship_evidence ({", ".join(_INSERT_COLUMNS)})
                    VALUES ({", ".join("?" for _ in _INSERT_COLUMNS)})""",
                [getattr(evidence, c) for c in _INSERT_COLUMNS],
            )
            created += 1
    return created, skipped


def _row_to_evidence(row) -> SponsorshipEvidence:
    return SponsorshipEvidence.model_validate(dict(row))


def list_evidence_for_company(company_id: int, limit: int = 200) -> list[SponsorshipEvidence]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM employer_sponsorship_evidence WHERE company_id = ? ORDER BY fiscal_year DESC, observed_at DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]


def list_evidence_by_name(company_name: str, limit: int = 100) -> list[SponsorshipEvidence]:
    """Lookup by raw name text -- useful before a company row even exists in
    registry_companies (evidence may be imported ahead of registry
    acquisition discovering the company)."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM employer_sponsorship_evidence WHERE lower(company_name_raw) = lower(?) "
            "ORDER BY observed_at DESC LIMIT ?",
            (company_name, limit),
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]


def list_evidence_by_normalized_name(normalized_name: str, limit: int = 500) -> list[SponsorshipEvidence]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM employer_sponsorship_evidence WHERE company_normalized_name = ? "
            "ORDER BY fiscal_year DESC LIMIT ?",
            (normalized_name, limit),
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]


def list_unresolved_evidence(limit: int = 500) -> list[SponsorshipEvidence]:
    """Evidence rows not yet attached to a registry company -- input queue for
    app.sponsorship.identity resolution."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM employer_sponsorship_evidence WHERE company_id IS NULL "
            "ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]


def attach_company(evidence_id: int, company_id: int) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE employer_sponsorship_evidence SET company_id = ? WHERE id = ?",
            (company_id, evidence_id),
        )


def has_any_evidence(company_name: str) -> bool:
    return len(list_evidence_by_name(company_name, limit=1)) > 0


def count_evidence() -> int:
    with db_session() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM employer_sponsorship_evidence").fetchone()["c"]


def count_evidence_for_dataset(dataset_id: int) -> int:
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM employer_sponsorship_evidence WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()["c"]


def count_companies_with_recent_h1b_history(as_of_year: Optional[int] = None) -> int:
    """Used by app.observability metrics -- distinct companies with any
    USCIS/DOL evidence in the last 2 fiscal years."""
    year = as_of_year or datetime.now(timezone.utc).year
    with db_session() as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT company_id) AS c FROM employer_sponsorship_evidence
               WHERE company_id IS NOT NULL AND fiscal_year >= ?
                 AND source_type IN (?, ?)""",
            (year - 1, SourceType.USCIS_EMPLOYER_DATA.value, SourceType.DOL_LCA_DATA.value),
        ).fetchone()
        return row["c"] or 0
