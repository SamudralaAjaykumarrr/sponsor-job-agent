"""Persistent, bounded schema-drift tracking (CLAUDE.md Phase 6 section 16),
distinct from app.workers.schema_check's per-attempt boolean shape check.
Never stores raw payloads -- only a short structural signature derived from
the shape-check's own descriptive detail string (e.g. "expected field
'jobs' missing from response"), plus small text fields."""

import hashlib
from datetime import datetime, timedelta, timezone

from app.db import db_session


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def structural_signature(detail: str) -> str:
    return hashlib.sha256(detail.encode("utf-8")).hexdigest()[:16]


def record_drift(*, provider: str, tenant_identifier: str, detail: str, expected_parser_version: str = "") -> None:
    signature = structural_signature(detail)
    now = utcnow()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO provider_schema_drift
                 (provider, tenant_identifier, signature, expected_parser_version,
                  first_seen_at, last_seen_at, occurrence_count, detail)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(provider, tenant_identifier, signature) DO UPDATE SET
                 last_seen_at = excluded.last_seen_at,
                 occurrence_count = provider_schema_drift.occurrence_count + 1""",
            (provider, tenant_identifier, signature, expected_parser_version, now, now, detail[:500]),
        )


def list_recent_drift(limit: int = 100) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM provider_schema_drift ORDER BY last_seen_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def distinct_tenants_with_recent_drift(provider: str, *, since_hours: float = 1.0) -> int:
    """How many DIFFERENT tenants of this provider have shown schema drift
    recently -- used to distinguish "one oddball tenant" from "the provider
    changed its API shape for everyone", which should feed the circuit
    breaker (CLAUDE.md: "a provider-wide drift affecting many tenants should
    feed circuit-breaker logic")."""
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    with db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT tenant_identifier) AS c FROM provider_schema_drift "
            "WHERE provider = ? AND last_seen_at >= ?",
            (provider, since),
        ).fetchone()
        return row["c"]
