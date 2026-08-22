"""Parent/subsidiary/affiliate/acquired company relationships (CLAUDE.md
Phase 7 section 10). Stored with confidence + verification, but NEVER used to
automatically transfer sponsorship evidence between the two companies --
app.sponsorship.profile always aggregates strictly per company_id. This
module exists for display (dashboard/company page) and doctor contradiction
checks only."""

from datetime import datetime, timezone

from app.db import db_session
from app.sponsorship.schema import RelationshipType


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_relationship(
    parent_company_id: int, child_company_id: int,
    relationship_type: RelationshipType = RelationshipType.SUBSIDIARY,
    confidence: int = 0, source: str = "", verified: bool = False, notes: str = "",
) -> int:
    if parent_company_id == child_company_id:
        raise ValueError("a company cannot be its own parent/subsidiary")
    rel_value = relationship_type.value if hasattr(relationship_type, "value") else relationship_type
    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM company_relationships WHERE parent_company_id = ? AND child_company_id = ? "
            "AND relationship_type = ?",
            (parent_company_id, child_company_id, rel_value),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE company_relationships SET confidence = MAX(confidence, ?), verified = MAX(verified, ?) "
                "WHERE id = ?",
                (confidence, int(verified), existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO company_relationships
               (parent_company_id, child_company_id, relationship_type, confidence, source, verified, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (parent_company_id, child_company_id, rel_value, confidence, source, int(verified), notes, utcnow()),
        )
        return cur.lastrowid


def list_relationships_for_company(company_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM company_relationships WHERE parent_company_id = ? OR child_company_id = ? ORDER BY id ASC",
            (company_id, company_id),
        ).fetchall()
        return [dict(r) for r in rows]


def find_contradictions() -> list[dict]:
    """A pair of companies related as both PARENT-of and SUBSIDIARY-of each
    other simultaneously (or a relationship contradicted by its own reverse
    row) -- used by app.sponsorship.doctor."""
    with db_session() as conn:
        rows = conn.execute(
            """SELECT a.id AS id_a, b.id AS id_b, a.parent_company_id, a.child_company_id
               FROM company_relationships a
               JOIN company_relationships b
                 ON a.parent_company_id = b.child_company_id AND a.child_company_id = b.parent_company_id
               WHERE a.relationship_type = 'PARENT' AND b.relationship_type = 'PARENT'
                 AND a.parent_company_id < a.child_company_id"""
        ).fetchall()
        return [dict(r) for r in rows]
