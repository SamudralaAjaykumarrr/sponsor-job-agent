import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.registry.models import CompanyRegistryEntry, utcnow
from app.registry.scheduling import compute_health, compute_next_poll_at, next_interval_minutes

_COLUMNS = [
    "company_name", "company_domain", "provider", "tenant_identifier", "careers_url",
    "country", "enabled", "verified_at", "last_success_at", "last_failure_at",
    "last_error", "consecutive_failures", "support_level", "notes",
    "last_polled_at", "next_poll_at", "average_job_yield", "average_latency_ms",
    "poll_interval_minutes", "created_at", "updated_at",
]


def _row_to_entry(row: sqlite3.Row) -> CompanyRegistryEntry:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    return CompanyRegistryEntry.model_validate(data)


def _coerce(v):
    return v.value if hasattr(v, "value") else v


def insert_entry(entry: CompanyRegistryEntry) -> int:
    with db_session() as conn:
        values = [_coerce(getattr(entry, col)) for col in _COLUMNS]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        cols = ", ".join(_COLUMNS)
        cur = conn.execute(f"INSERT INTO company_registry ({cols}) VALUES ({placeholders})", values)
        return cur.lastrowid


def get_entry(entry_id: int) -> Optional[CompanyRegistryEntry]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM company_registry WHERE id = ?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row else None


def get_entry_by_tenant(provider: str, tenant_identifier: str) -> Optional[CompanyRegistryEntry]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM company_registry WHERE provider = ? AND tenant_identifier = ?",
            (provider, tenant_identifier),
        ).fetchone()
        return _row_to_entry(row) if row else None


def list_entries(provider: Optional[str] = None, enabled_only: bool = False) -> list[CompanyRegistryEntry]:
    query = "SELECT * FROM company_registry"
    clauses, params = [], []
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY company_name ASC"
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_entry(r) for r in rows]


def list_due_for_poll(now: Optional[str] = None, limit: int = 200) -> list[CompanyRegistryEntry]:
    now = now or datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        rows = conn.execute(
            """SELECT * FROM company_registry
               WHERE enabled = 1 AND (next_poll_at IS NULL OR next_poll_at <= ?)
               ORDER BY (next_poll_at IS NULL) DESC, next_poll_at ASC LIMIT ?""",
            (now, limit),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]


def update_entry(entry_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    cleaned = {k: _coerce(v) for k, v in fields.items()}
    set_clause = ", ".join(f"{k} = ?" for k in cleaned)
    with db_session() as conn:
        conn.execute(f"UPDATE company_registry SET {set_clause} WHERE id = ?", [*cleaned.values(), entry_id])


def mark_poll_result(
    entry_id: int, *, success: bool, jobs_new: int = 0, latency_ms: float = 0.0, error: str = "",
) -> None:
    """Applies the deterministic adaptive-polling rules and persists the
    resulting schedule + health bookkeeping for one tenant after one poll."""
    entry = get_entry(entry_id)
    if entry is None:
        return
    now_iso = utcnow()
    consecutive_failures = 0 if success else entry.consecutive_failures + 1
    interval = next_interval_minutes(
        entry.poll_interval_minutes, success=success, jobs_new=jobs_new,
        consecutive_failures=consecutive_failures,
    )
    # Exponential moving average -- smooths noisy per-cycle yields/latencies
    # without needing history storage.
    alpha = 0.3
    new_avg_yield = entry.average_job_yield + alpha * (jobs_new - entry.average_job_yield)
    new_avg_latency = entry.average_latency_ms + alpha * (latency_ms - entry.average_latency_ms)

    fields = dict(
        last_polled_at=now_iso,
        next_poll_at=compute_next_poll_at(interval),
        poll_interval_minutes=interval,
        consecutive_failures=consecutive_failures,
        average_job_yield=round(new_avg_yield, 3),
        average_latency_ms=round(new_avg_latency, 2),
    )
    if success:
        fields["last_success_at"] = now_iso
        fields["last_error"] = ""
    else:
        fields["last_failure_at"] = now_iso
        fields["last_error"] = error[:500]
    update_entry(entry_id, **fields)


def seed_demo_entries() -> None:
    """Only called when REGISTRY_SEED_DEMO_DATA=true (see app.main lifespan)
    AND the table is currently empty. Illustrative public boards only --
    never a substitute for the real Phase 4 bulk importer."""
    if list_entries():
        return
    demo = [
        CompanyRegistryEntry(company_name="GitLab", provider="greenhouse", tenant_identifier="gitlab",
                              careers_url="https://about.gitlab.com/jobs/", country="US",
                              notes="Illustrative public Greenhouse board -- verify still valid before relying on it."),
        CompanyRegistryEntry(company_name="Lever Demo", provider="lever", tenant_identifier="leverdemo",
                              careers_url="https://jobs.lever.co/leverdemo", country="US",
                              notes="Lever's own public demo account -- fake data, illustrative only."),
    ]
    for entry in demo:
        insert_entry(entry)


def provider_health_summary() -> list[dict]:
    """Aggregated per-provider tenant health counts for the dashboard."""
    summary: dict[str, dict] = defaultdict(lambda: {
        "tenants": 0, "healthy": 0, "degraded": 0, "failing": 0,
        "last_success_at": None, "last_failure_at": None,
    })
    for entry in list_entries():
        s = summary[entry.provider]
        s["tenants"] += 1
        health = compute_health(entry.consecutive_failures, entry.last_success_at)
        s[health.value.lower()] += 1
        if entry.last_success_at and (s["last_success_at"] is None or entry.last_success_at > s["last_success_at"]):
            s["last_success_at"] = entry.last_success_at
        if entry.last_failure_at and (s["last_failure_at"] is None or entry.last_failure_at > s["last_failure_at"]):
            s["last_failure_at"] = entry.last_failure_at
    return [{"provider": k, **v} for k, v in summary.items()]
