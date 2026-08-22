"""Company alias model (CLAUDE.md Phase 7 section 9). Aliases let evidence
rows using a different legal/DBA/brand name ("Google LLC", "Google Cloud")
resolve to the same registry_companies row as "Google" without ever merging
purely on name similarity -- every alias is an explicit, stored row with a
type/source/confidence, and starts unverified unless the caller says
otherwise."""

from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.registry.normalize import normalize_company_name
from app.sponsorship.schema import AliasType


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_alias(
    company_id: int, alias: str, alias_type: AliasType = AliasType.DBA,
    source: str = "", confidence: int = 0, verified: bool = False,
) -> int:
    normalized = normalize_company_name(alias)
    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM company_aliases WHERE company_id = ? AND normalized_alias = ?",
            (company_id, normalized),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE company_aliases SET confidence = MAX(confidence, ?), verified = MAX(verified, ?) WHERE id = ?",
                (confidence, int(verified), existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO company_aliases
               (company_id, alias, normalized_alias, alias_type, source, confidence, verified, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (company_id, alias, normalized, alias_type.value if hasattr(alias_type, "value") else alias_type,
             source, confidence, int(verified), utcnow()),
        )
        return cur.lastrowid


def find_company_id_by_alias(name: str) -> Optional[int]:
    """Returns a company_id ONLY when exactly one company claims this alias --
    an ambiguous alias (claimed by more than one company) is never
    auto-resolved here."""
    normalized = normalize_company_name(name)
    if not normalized:
        return None
    with db_session() as conn:
        rows = conn.execute(
            "SELECT DISTINCT company_id FROM company_aliases WHERE normalized_alias = ?", (normalized,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["company_id"]
        return None


def list_aliases_for_company(company_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM company_aliases WHERE company_id = ? ORDER BY id ASC", (company_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_alias_collisions() -> list[dict]:
    """Same normalized alias claimed by more than one distinct company --
    used by app.sponsorship.doctor (CLAUDE.md Phase 7 section 35)."""
    with db_session() as conn:
        rows = conn.execute(
            """SELECT normalized_alias, COUNT(DISTINCT company_id) AS n
               FROM company_aliases WHERE verified = 1
               GROUP BY normalized_alias HAVING n > 1"""
        ).fetchall()
        return [dict(r) for r in rows]
