"""Per-row checkpoint/lease tracking for a large acquisition batch (CLAUDE.md
Phase 6 section 28): "Two workers processing acquisition must not create
duplicate companies/portals." app.registry.acquisition's original
single-process `run_acquisition_batch()` already can't create duplicates
even under naive re-runs (registry_companies/registry_portals have real
unique-identity DB constraints -- see app/db.py's
idx_registry_companies_identity / idx_registry_portals_provider_tenant --
and app.registry.importers.process_row looks up-or-creates rather than
blindly inserting). What THIS module adds is efficient, safe *partitioning*
of one batch's rows across multiple concurrent workers/processes, using the
exact same atomic claim-lease pattern as the poll/verification queues
(app/workers/leasing.py), so two workers never redundantly reprocess (and
therefore never race on) the same row."""

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db import db_session
from app.registry.importers import RegistryCandidate


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def seed_records(batch_id: int, candidates: list[RegistryCandidate]) -> int:
    """Idempotent: re-seeding an already-seeded batch (e.g. a second worker
    calling this for the same batch_id) inserts nothing new -- ON CONFLICT
    (batch_id, row_index) DO NOTHING."""
    now = utcnow()
    rows = [
        (
            batch_id, c.row_number, c.company_name, c.company_domain,
            json.dumps({
                "company_name": c.company_name, "company_domain": c.company_domain,
                "careers_url": c.careers_url, "provider": c.provider,
                "tenant_identifier": c.tenant_identifier, "country": c.country,
                "source": c.source, "source_url": c.source_url, "row_number": c.row_number,
            }),
            now,
        )
        for c in candidates
    ]
    inserted = 0
    with db_session() as conn:
        for row in rows:
            cur = conn.execute(
                """INSERT INTO registry_acquisition_records
                     (batch_id, row_index, company_name_raw, company_domain_raw, raw_row_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(batch_id, row_index) DO NOTHING""",
                row,
            )
            if cur.rowcount:
                inserted += 1
    return inserted


def claim_batch(*, batch_id: int, worker_id: str, limit: int, lease_seconds: int) -> list[dict]:
    """Atomically claims up to `limit` PENDING (or lease-expired) rows for
    this worker. Same WHERE-guarded UPDATE pattern as
    app.workers.leasing.claim_poll_batch -- correct on both SQLite (single-
    writer serialization) and Postgres (MVCC read-committed re-check); a
    dedicated SKIP LOCKED path isn't needed here since acquisition batches
    are not the same kind of tight, high-contention hot loop as the poll
    queue."""
    now = utcnow()
    claimed: list[dict] = []
    with db_session() as conn:
        candidates = conn.execute(
            """SELECT id FROM registry_acquisition_records
               WHERE batch_id = ? AND status IN ('PENDING', 'CLAIMED')
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
               ORDER BY row_index ASC
               LIMIT ?""",
            (batch_id, now, limit * 4),
        ).fetchall()
        for row in candidates:
            if len(claimed) >= limit:
                break
            record_id = row["id"]
            expires = _iso_plus(lease_seconds)
            cur = conn.execute(
                """UPDATE registry_acquisition_records
                   SET status = 'CLAIMED', lease_owner = ?, lease_expires_at = ?, updated_at = ?
                   WHERE id = ? AND status IN ('PENDING', 'CLAIMED')
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (worker_id, expires, now, record_id, now),
            )
            if cur.rowcount == 1:
                full = conn.execute("SELECT * FROM registry_acquisition_records WHERE id = ?", (record_id,)).fetchone()
                claimed.append(dict(full))
    return claimed


def candidate_from_record(record: dict) -> RegistryCandidate:
    raw = json.loads(record["raw_row_json"] or "{}")
    return RegistryCandidate(
        company_name=raw.get("company_name", ""), company_domain=raw.get("company_domain", ""),
        careers_url=raw.get("careers_url", ""), provider=raw.get("provider", ""),
        tenant_identifier=raw.get("tenant_identifier", ""), country=raw.get("country", ""),
        source=raw.get("source", ""), source_url=raw.get("source_url", ""),
        row_number=raw.get("row_number", record["row_index"]),
    )


def mark_done(record_id: int, *, company_id: Optional[int], portal_id: Optional[int], verification_result: str = "") -> None:
    with db_session() as conn:
        conn.execute(
            """UPDATE registry_acquisition_records
               SET status = 'DONE', company_id = ?, portal_id = ?, verification_result = ?,
                   lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
               WHERE id = ?""",
            (company_id, portal_id, verification_result, utcnow(), record_id),
        )


def mark_failed(record_id: int, *, error: str) -> None:
    with db_session() as conn:
        conn.execute(
            """UPDATE registry_acquisition_records
               SET status = 'FAILED', retry_count = retry_count + 1, error = ?,
                   lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
               WHERE id = ?""",
            (error[:500], utcnow(), record_id),
        )


def batch_progress(batch_id: int) -> dict:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM registry_acquisition_records WHERE batch_id = ? GROUP BY status",
            (batch_id,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM registry_acquisition_records WHERE batch_id = ?", (batch_id,)
        ).fetchone()["c"]
    by_status = {r["status"]: r["c"] for r in rows}
    return {
        "total": total,
        "pending": by_status.get("PENDING", 0),
        "claimed": by_status.get("CLAIMED", 0),
        "done": by_status.get("DONE", 0),
        "failed": by_status.get("FAILED", 0),
    }
