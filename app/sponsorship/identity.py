"""Deterministic employer identity resolution (CLAUDE.md Phase 7 sections
8, 36). Evidence rows arrive with a raw company name (and sometimes a
domain/city/state) that must be matched to a `registry_companies` row before
they can contribute to that company's sponsorship profile.

Resolution order (first match wins, most specific first):
  1. normalized_name + domain exact match against registry_companies
  2. verified alias exact match (company_aliases)
  3. normalized_name-only match, but ONLY if exactly one registry company has
     that normalized name (never guess among several)
Anything else is AMBIGUOUS or UNMATCHED and is written to
employer_identity_review for manual resolution -- never force-matched."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.registry.normalize import normalize_company_name, normalize_domain
from app.sponsorship.aliases import find_company_id_by_alias
from app.sponsorship.schema import IdentityReviewStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IdentityMatch:
    company_id: Optional[int]
    matched_via: str  # "domain" | "alias" | "name_only" | "none" | "ambiguous"
    reasons: list[str] = field(default_factory=list)


def _companies_by_normalized_name(conn, normalized_name: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, normalized_name, primary_domain FROM registry_companies WHERE normalized_name = ?",
        (normalized_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_company(
    company_name_raw: str, company_domain: str = "", city: str = "", state: str = "",
) -> IdentityMatch:
    normalized_name = normalize_company_name(company_name_raw)
    normalized_domain = normalize_domain(company_domain) if company_domain else ""

    if not normalized_name:
        return IdentityMatch(company_id=None, matched_via="none", reasons=["empty/unusable company name"])

    with db_session() as conn:
        if normalized_domain:
            row = conn.execute(
                "SELECT id FROM registry_companies WHERE normalized_name = ? AND primary_domain = ?",
                (normalized_name, normalized_domain),
            ).fetchone()
            if row:
                return IdentityMatch(row["id"], "domain", [f"exact name+domain match on '{normalized_domain}'"])

            row = conn.execute(
                "SELECT id FROM registry_companies WHERE primary_domain = ?", (normalized_domain,)
            ).fetchone()
            if row:
                return IdentityMatch(row["id"], "domain", [f"domain match on '{normalized_domain}' (name differs)"])

    alias_match = find_company_id_by_alias(company_name_raw)
    if alias_match is not None:
        return IdentityMatch(alias_match, "alias", [f"verified alias match for '{company_name_raw}'"])

    with db_session() as conn:
        candidates = _companies_by_normalized_name(conn, normalized_name)

    if len(candidates) == 1:
        return IdentityMatch(candidates[0]["id"], "name_only",
                              [f"single registry company matches normalized name '{normalized_name}'"])
    if len(candidates) > 1:
        _create_review(company_name_raw, company_domain, [c["id"] for c in candidates],
                        reason=f"{len(candidates)} registry companies share normalized name '{normalized_name}' "
                               "with different domains -- refusing to auto-merge")
        return IdentityMatch(None, "ambiguous",
                              [f"{len(candidates)} companies share this name; sent to identity review"])

    return IdentityMatch(None, "none", [f"no registry company found for '{normalized_name}'"])


def _create_review(company_name: str, domain: str, candidate_ids: list[int], reason: str) -> int:
    import json

    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM employer_identity_review WHERE source_company_name = ? AND status = 'PENDING'",
            (company_name,),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO employer_identity_review
               (source_company_name, source_domain, candidate_company_ids, reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_name, domain, json.dumps(candidate_ids), reason, IdentityReviewStatus.PENDING.value, utcnow()),
        )
        return cur.lastrowid


def list_pending_reviews(limit: int = 200) -> list[dict]:
    import json

    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM employer_identity_review WHERE status = 'PENDING' ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["candidate_company_ids"] = json.loads(d.get("candidate_company_ids") or "[]")
            except (ValueError, TypeError):
                d["candidate_company_ids"] = []
            out.append(d)
        return out


def resolve_review(review_id: int, company_id: Optional[int], note: str = "") -> None:
    status = IdentityReviewStatus.RESOLVED.value if company_id is not None else IdentityReviewStatus.REJECTED.value
    with db_session() as conn:
        conn.execute(
            "UPDATE employer_identity_review SET status = ?, resolved_company_id = ?, resolution_note = ?, "
            "resolved_at = ? WHERE id = ?",
            (status, company_id, note, utcnow(), review_id),
        )


def resolve_and_attach_evidence(evidence_id: int, company_name_raw: str, company_domain: str = "") -> IdentityMatch:
    """Resolves identity for one evidence row and, on an unambiguous match,
    attaches it immediately -- used by the government-data importers."""
    from app.sponsorship.evidence import attach_company

    match = resolve_company(company_name_raw, company_domain)
    if match.company_id is not None:
        attach_company(evidence_id, match.company_id)
    return match
