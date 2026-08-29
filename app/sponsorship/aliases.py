"""Company alias model (CLAUDE.md Phase 7 section 9). Aliases let evidence
rows using a different legal/DBA/brand name ("Google LLC", "Google Cloud")
resolve to the same registry_companies row as "Google" without ever merging
purely on name similarity -- every alias is an explicit, stored row with a
type/source/confidence, and starts unverified unless the caller says
otherwise."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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


def _companies_by_normalized_name(normalized_name: str) -> list[int]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id FROM registry_companies WHERE normalized_name = ?", (normalized_name,)
        ).fetchall()
        return [r["id"] for r in rows]


@dataclass
class AliasSeedResult:
    applied: int = 0
    skipped_no_company: list[str] = field(default_factory=list)
    skipped_ambiguous_company: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied, "skipped_no_company": self.skipped_no_company,
            "skipped_ambiguous_company": self.skipped_ambiguous_company,
        }


def seed_known_aliases(path: Optional[str | Path] = None) -> AliasSeedResult:
    """Loads app.config.EMPLOYER_ALIAS_SEED_PATH (or an explicit `path`) --
    a small, human-curated list of verified legal-name/DBA aliases for
    real, already-discovered employers (CLAUDE.md 'Employer entity matching'
    section 3). Every alias in the seed file is a deliberately researched,
    verified mapping -- never a fuzzy guess -- so this always calls add_alias
    with verified=True. Safe to re-run (add_alias is itself idempotent per
    (company_id, normalized_alias)).

    A seed row whose `registry_normalized_name` doesn't resolve to EXACTLY
    ONE registry_companies row is skipped, never force-applied -- matching
    app.sponsorship.identity.resolve_company's own "ambiguous -> never
    guess" rule."""
    if path is None:
        from app.config import EMPLOYER_ALIAS_SEED_PATH

        path = EMPLOYER_ALIAS_SEED_PATH
    path = Path(path)
    result = AliasSeedResult()
    if not path.exists():
        return result

    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("aliases", []):
        normalized_name = entry["registry_normalized_name"]
        candidates = _companies_by_normalized_name(normalized_name)
        if not candidates:
            result.skipped_no_company.append(entry["alias"])
            continue
        if len(candidates) > 1:
            result.skipped_ambiguous_company.append(entry["alias"])
            continue
        add_alias(
            candidates[0], entry["alias"], AliasType(entry.get("alias_type", "DBA")),
            source=entry.get("source", ""), confidence=int(entry.get("confidence", 0)), verified=True,
        )
        result.applied += 1
    return result
