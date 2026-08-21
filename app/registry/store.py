"""CRUD + dedup lookups + bounded/paginated queries for the Phase 4 registry
tables (registry_companies / registry_portals / registry_provenance). Kept
separate from app/registry/repo.py, which is the unchanged Phase 3
operational company_registry table consumed by the discovery cycle.

All list/query functions here are bounded (LIMIT + keyset pagination) so
callers never have to load the full registry into memory, per CLAUDE.md's
Phase 4 high-volume-operations rules."""

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.db import db_session
from app.registry.models import (
    CareerPortal,
    Company,
    DiscoveryStatus,
    IdentityStatus,
    PortalStatus,
    RegistryProvenance,
)
from app.providers.capabilities import SupportLevel


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce(v):
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, bool):
        return int(v)
    return v


# --- Company ------------------------------------------------------------

_COMPANY_COLUMNS = [
    "normalized_name", "display_name", "primary_domain", "careers_home_url",
    "country", "headquarters_location", "enabled", "created_at", "updated_at",
]


def _row_to_company(row: sqlite3.Row) -> Company:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    return Company.model_validate(data)


def insert_company(company: Company) -> int:
    with db_session() as conn:
        values = [_coerce(getattr(company, c)) for c in _COMPANY_COLUMNS]
        placeholders = ", ".join("?" for _ in _COMPANY_COLUMNS)
        cols = ", ".join(_COMPANY_COLUMNS)
        cur = conn.execute(f"INSERT INTO registry_companies ({cols}) VALUES ({placeholders})", values)
        return cur.lastrowid


def get_company(company_id: int) -> Optional[Company]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM registry_companies WHERE id = ?", (company_id,)).fetchone()
        return _row_to_company(row) if row else None


def get_company_by_identity(normalized_name: str, primary_domain: str) -> Optional[Company]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM registry_companies WHERE normalized_name = ? AND primary_domain = ?",
            (normalized_name, primary_domain),
        ).fetchone()
        return _row_to_company(row) if row else None


def get_company_by_name_only(normalized_name: str) -> Optional[Company]:
    """Best-effort fallback lookup when no domain is available. Only used by
    the importer when a candidate row has no domain at all."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM registry_companies WHERE normalized_name = ? AND primary_domain = '' LIMIT 1",
            (normalized_name,),
        ).fetchone()
        return _row_to_company(row) if row else None


def update_company(company_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    cleaned = {k: _coerce(v) for k, v in fields.items()}
    set_clause = ", ".join(f"{k} = ?" for k in cleaned)
    with db_session() as conn:
        conn.execute(f"UPDATE registry_companies SET {set_clause} WHERE id = ?", [*cleaned.values(), company_id])


def list_companies(limit: int = 200, after_id: int = 0, search: str = "") -> list[Company]:
    query = "SELECT * FROM registry_companies WHERE id > ?"
    params: list = [after_id]
    if search:
        query += " AND (normalized_name LIKE ? OR primary_domain LIKE ?)"
        needle = f"%{search.lower()}%"
        params += [needle, needle]
    query += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_company(r) for r in rows]


def count_companies() -> int:
    with db_session() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM registry_companies").fetchone()["c"]


# --- CareerPortal ---------------------------------------------------------

_PORTAL_COLUMNS = [
    "company_id", "provider", "tenant_identifier", "careers_url", "jobs_url", "canonical_url",
    "support_level", "discovery_status", "verification_status", "identity_status", "enabled",
    "confidence", "confidence_reasons", "last_verified_at", "last_polled_at", "next_poll_at",
    "last_success_at", "last_failure_at", "consecutive_failures", "consecutive_permanent_failures",
    "average_job_yield", "average_latency_ms", "current_job_count", "poll_interval_minutes",
    "registry_entry_id", "superseded_by_portal_id", "notes", "created_at", "updated_at",
]


def _row_to_portal(row: sqlite3.Row) -> CareerPortal:
    import json

    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    reasons = data.get("confidence_reasons") or "[]"
    try:
        data["confidence_reasons"] = json.loads(reasons)
    except (json.JSONDecodeError, TypeError):
        data["confidence_reasons"] = []
    return CareerPortal.model_validate(data)


def _portal_values(portal: CareerPortal) -> list:
    import json

    values = []
    for c in _PORTAL_COLUMNS:
        v = getattr(portal, c)
        if c == "confidence_reasons":
            v = json.dumps(v or [])
        values.append(_coerce(v))
    return values


def insert_portal(portal: CareerPortal) -> int:
    with db_session() as conn:
        cols = ", ".join(_PORTAL_COLUMNS)
        placeholders = ", ".join("?" for _ in _PORTAL_COLUMNS)
        cur = conn.execute(f"INSERT INTO registry_portals ({cols}) VALUES ({placeholders})", _portal_values(portal))
        return cur.lastrowid


def get_portal(portal_id: int) -> Optional[CareerPortal]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM registry_portals WHERE id = ?", (portal_id,)).fetchone()
        return _row_to_portal(row) if row else None


def get_portal_by_provider_tenant(provider: str, tenant_identifier: str) -> Optional[CareerPortal]:
    if not tenant_identifier:
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM registry_portals WHERE provider = ? AND tenant_identifier = ?",
            (provider, tenant_identifier),
        ).fetchone()
        return _row_to_portal(row) if row else None


def get_portal_by_canonical_url(canonical_url: str) -> Optional[CareerPortal]:
    if not canonical_url:
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM registry_portals WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        return _row_to_portal(row) if row else None


def update_portal(portal_id: int, **fields) -> None:
    if not fields:
        return
    import json

    fields["updated_at"] = utcnow()
    if "confidence_reasons" in fields and not isinstance(fields["confidence_reasons"], str):
        fields["confidence_reasons"] = json.dumps(fields["confidence_reasons"] or [])
    cleaned = {k: _coerce(v) for k, v in fields.items()}
    set_clause = ", ".join(f"{k} = ?" for k in cleaned)
    with db_session() as conn:
        conn.execute(f"UPDATE registry_portals SET {set_clause} WHERE id = ?", [*cleaned.values(), portal_id])


def list_portals_for_company(company_id: int) -> list[CareerPortal]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM registry_portals WHERE company_id = ? ORDER BY id ASC", (company_id,)
        ).fetchall()
        return [_row_to_portal(r) for r in rows]


def list_portals(
    *,
    provider: str = "",
    verification_status: str = "",
    support_level: str = "",
    enabled: Optional[bool] = None,
    search: str = "",
    limit: int = 200,
    after_id: int = 0,
) -> list[CareerPortal]:
    """Bounded, keyset-paginated portal listing for the dashboard/CLI. Never
    returns more than `limit` rows regardless of registry size."""
    clauses, params = ["id > ?"], [after_id]
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if verification_status:
        clauses.append("verification_status = ?")
        params.append(verification_status)
    if support_level:
        clauses.append("support_level = ?")
        params.append(support_level)
    if enabled is not None:
        clauses.append("enabled = ?")
        params.append(int(enabled))
    if search:
        clauses.append("(tenant_identifier LIKE ? OR careers_url LIKE ? OR provider LIKE ?)")
        needle = f"%{search.lower()}%"
        params += [needle, needle, needle]
    query = "SELECT * FROM registry_portals WHERE " + " AND ".join(clauses) + " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_portal(r) for r in rows]


def count_portals(**filters) -> int:
    clauses, params = [], []
    for col in ("provider", "verification_status", "support_level"):
        if filters.get(col):
            clauses.append(f"{col} = ?")
            params.append(filters[col])
    query = "SELECT COUNT(*) AS c FROM registry_portals"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with db_session() as conn:
        return conn.execute(query, params).fetchone()["c"]


def list_due_for_verification(limit: int = 200, statuses: Iterable[str] = ("DISCOVERED", "CANDIDATE")) -> list[CareerPortal]:
    """Portals that still need the verification pipeline run against them --
    bounded batch, never the whole table."""
    placeholders = ", ".join("?" for _ in statuses)
    with db_session() as conn:
        rows = conn.execute(
            f"""SELECT * FROM registry_portals
                WHERE enabled = 1 AND verification_status IN ({placeholders})
                ORDER BY id ASC LIMIT ?""",
            [*statuses, limit],
        ).fetchall()
        return [_row_to_portal(r) for r in rows]


def all_portal_ids() -> list[int]:
    """Used by sharding/benchmark tooling only -- bounded by caller context
    (synthetic benchmark DB, or a deliberately small real registry), never
    called from the production discovery cycle."""
    with db_session() as conn:
        rows = conn.execute("SELECT id FROM registry_portals ORDER BY id ASC").fetchall()
        return [r["id"] for r in rows]


def bulk_insert_portals_raw(rows: list[dict]) -> int:
    """Fast-path bulk insert for the synthetic scale benchmark ONLY -- bypasses
    the Pydantic model per-row for speed. Callers MUST use a benchmark-only
    temp database; never call this against the real registry."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    with db_session() as conn:
        conn.executemany(
            f"INSERT INTO registry_portals ({col_sql}) VALUES ({placeholders})",
            [[r[c] for c in cols] for r in rows],
        )
        return len(rows)


# --- Provenance -----------------------------------------------------------

def upsert_provenance(prov: RegistryProvenance) -> int:
    """Idempotent on (portal_id, source_type, source_name) -- a repeated
    import from the same source updates observed_at rather than growing an
    unbounded duplicate log, while still tracking every distinct source."""
    now = utcnow()
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO registry_provenance
                 (portal_id, company_id, source_type, source_name, source_url,
                  imported_at, observed_at, evidence, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(portal_id, source_type, source_name) DO UPDATE SET
                 observed_at = excluded.observed_at,
                 source_url = excluded.source_url,
                 evidence = excluded.evidence,
                 confidence = excluded.confidence
               """,
            (
                prov.portal_id, prov.company_id, prov.source_type, prov.source_name, prov.source_url,
                prov.imported_at or now, prov.observed_at or now, prov.evidence, prov.confidence, now,
            ),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM registry_provenance WHERE portal_id = ? AND source_type = ? AND source_name = ?",
            (prov.portal_id, prov.source_type, prov.source_name),
        ).fetchone()
        return row["id"] if row else 0


def list_provenance_for_portal(portal_id: int) -> list[RegistryProvenance]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM registry_provenance WHERE portal_id = ? ORDER BY observed_at DESC", (portal_id,)
        ).fetchall()
        return [RegistryProvenance.model_validate(dict(r)) for r in rows]


def has_provenance(portal_id: int) -> bool:
    with db_session() as conn:
        row = conn.execute(
            "SELECT 1 FROM registry_provenance WHERE portal_id = ? LIMIT 1", (portal_id,)
        ).fetchone()
        return row is not None


def list_orphan_provenance() -> list[RegistryProvenance]:
    with db_session() as conn:
        rows = conn.execute(
            """SELECT p.* FROM registry_provenance p
               LEFT JOIN registry_portals rp ON rp.id = p.portal_id
               WHERE p.portal_id IS NOT NULL AND rp.id IS NULL"""
        ).fetchall()
        return [RegistryProvenance.model_validate(dict(r)) for r in rows]
