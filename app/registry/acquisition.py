"""Resumable registry-acquisition batch executor -- CLAUDE.md Phase 5
sections 17-19. Runs the full seed -> normalize -> company -> portal
candidate -> verification -> VERIFIED/ACTIVE-or-QUARANTINED pipeline for one
input dataset, tracked as a `registry_acquisition_batches` row so progress
survives an interruption and can be resumed exactly where it left off.

Reuses app.registry.importers' row-processing logic (`process_row`) for the
candidate-creation half, and app.registry.verification/lifecycle/sync for
the verification half -- this module only adds batch bookkeeping,
checkpointed resume, and per-record failure isolation on top of both.

Every source must be attributable (source_name/source path recorded); no
scraping of search engines, LinkedIn, or Indeed is performed anywhere in
this pipeline -- see CLAUDE.md Phase 5 section 18."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.db import db_session
from app.registry import acquisition_records
from app.registry import lifecycle as registry_lifecycle
from app.registry import store
from app.registry import sync as registry_sync
from app.registry.importers import ImportSummary, process_row, read_candidates
from app.registry.models import PortalStatus, VerificationResult
from app.registry.verification import verify_portal

logger = logging.getLogger("registry.acquisition")

_DEFAULT_CHECKPOINT_EVERY = 25


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BatchResult:
    batch_id: int
    status: str
    records_total: int
    records_processed: int
    companies_created: int
    portal_candidates: int
    verified: int
    active: int
    quarantined: int
    failed: int
    resume_cursor: int
    errors: list[str]

    @classmethod
    def from_row(cls, row: dict) -> "BatchResult":
        return cls(
            batch_id=row["id"], status=row["status"], records_total=row["records_total"],
            records_processed=row["records_processed"], companies_created=row["companies_created"],
            portal_candidates=row["portal_candidates"], verified=row["verified"], active=row["active"],
            quarantined=row["quarantined"], failed=row["failed"], resume_cursor=row["resume_cursor"],
            errors=json.loads(row["errors"] or "[]"),
        )


def _create_batch(*, source_name: str, source_type: str, path: str) -> int:
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO registry_acquisition_batches
                 (source_name, source_type, path, status, created_at, errors)
               VALUES (?, ?, ?, 'PENDING', ?, '[]')""",
            (source_name, source_type, path, utcnow()),
        )
        return cur.lastrowid


def get_batch(batch_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM registry_acquisition_batches WHERE id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None


def list_batches(limit: int = 100) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM registry_acquisition_batches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def _update_batch(batch_id: int, **fields) -> None:
    if not fields:
        return
    if "errors" in fields and not isinstance(fields["errors"], str):
        fields["errors"] = json.dumps(fields["errors"])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db_session() as conn:
        conn.execute(f"UPDATE registry_acquisition_batches SET {set_clause} WHERE id = ?", [*fields.values(), batch_id])


def _maybe_verify(portal_id: int, *, counts: dict) -> None:
    """Best-effort immediate verification of a freshly created candidate
    portal, so a batch's verified/active/quarantined counts reflect real
    outcomes by the time the batch finishes rather than staying at zero
    until some later, separate verification pass. Never raises -- a
    verification failure here is just recorded, same as any other per-row
    outcome; it must never abort the batch."""
    portal = store.get_portal(portal_id)
    if portal is None or not portal.tenant_identifier:
        return
    company = store.get_company(portal.company_id)
    try:
        outcome = verify_portal(portal, company_display_name=company.display_name if company else "")
    except Exception:  # noqa: BLE001 - one bad candidate must never abort the batch
        logger.warning("acquisition verification for portal %s raised unexpectedly", portal_id, exc_info=True)
        counts["failed"] += 1
        return

    if outcome.result == VerificationResult.UNSUPPORTED:
        return  # no working discovery implementation -- not a failure, just not yet verifiable

    registry_lifecycle.apply_verification_outcome(portal_id, outcome)
    if outcome.result == VerificationResult.VERIFIED:
        registry_lifecycle.maybe_detect_migration(portal.company_id, store.get_portal(portal_id))
    registry_sync.sync_portal_to_operational_registry(portal_id)

    updated = store.get_portal(portal_id)
    if updated is None:
        return
    if updated.verification_status in (PortalStatus.VERIFIED, PortalStatus.ACTIVE):
        counts["verified"] += 1
        if updated.verification_status == PortalStatus.ACTIVE:
            counts["active"] += 1
    elif updated.verification_status == PortalStatus.QUARANTINED:
        counts["quarantined"] += 1
    elif outcome.result in (VerificationResult.FAILED, VerificationResult.TEMPORARY_FAILURE):
        counts["failed"] += 1


def run_acquisition_batch(
    path: str | Path, *, source_name: Optional[str] = None, source_type: str = "CSV",
    resume_batch_id: Optional[int] = None, verify_new_candidates: bool = True,
    checkpoint_every: int = _DEFAULT_CHECKPOINT_EVERY,
) -> BatchResult:
    """Runs (or resumes) one acquisition batch to completion. Idempotent:
    re-running an already-COMPLETED batch id is a no-op that just returns
    its final result; resuming a PAUSED/FAILED batch continues from
    resume_cursor rather than reprocessing earlier rows."""
    path = Path(path)
    source_name = source_name or path.name

    if resume_batch_id is not None:
        existing = get_batch(resume_batch_id)
        if existing is None:
            raise ValueError(f"no such acquisition batch id={resume_batch_id}")
        if existing["status"] == "COMPLETED":
            return BatchResult.from_row(existing)
        batch_id = resume_batch_id
        resume_cursor = existing["resume_cursor"]
        counts = {
            "records_processed": existing["records_processed"], "companies_created": existing["companies_created"],
            "portal_candidates": existing["portal_candidates"], "verified": existing["verified"],
            "active": existing["active"], "quarantined": existing["quarantined"], "failed": existing["failed"],
        }
        _update_batch(batch_id, status="RUNNING", started_at=existing["started_at"] or utcnow())
    else:
        batch_id = _create_batch(source_name=source_name, source_type=source_type, path=str(path))
        resume_cursor = 0
        counts = {"records_processed": 0, "companies_created": 0, "portal_candidates": 0,
                  "verified": 0, "active": 0, "quarantined": 0, "failed": 0}
        _update_batch(batch_id, status="RUNNING", started_at=utcnow())

    summary = ImportSummary(source_name=source_name, dry_run=False)
    errors: list[str] = []
    row_number = 0

    try:
        for candidate in read_candidates(path):
            row_number += 1
            if row_number <= resume_cursor:
                continue

            before_created = summary.rows_created
            before_companies = summary.companies_created
            try:
                portal_id = process_row(candidate, source_name, summary, dry_run=False)
            except Exception as exc:  # noqa: BLE001 - one bad row must never abort the batch
                summary.rows_invalid += 1
                errors.append(f"row {candidate.row_number}: {exc}")
                logger.warning("acquisition row %s failed", candidate.row_number, exc_info=True)
                portal_id = None

            just_created = summary.rows_created > before_created
            counts["companies_created"] += summary.companies_created - before_companies
            if just_created:
                counts["portal_candidates"] += 1
                if verify_new_candidates and portal_id is not None:
                    _maybe_verify(portal_id, counts=counts)

            counts["records_processed"] += 1
            resume_cursor = row_number

            if counts["records_processed"] % checkpoint_every == 0:
                _update_batch(batch_id, resume_cursor=resume_cursor, records_total=row_number,
                               errors=errors[-200:], **counts)

        _update_batch(
            batch_id, status="COMPLETED", finished_at=utcnow(), resume_cursor=resume_cursor,
            records_total=row_number, errors=errors[-200:], **counts,
        )
    except Exception:
        _update_batch(batch_id, status="FAILED", resume_cursor=resume_cursor, errors=errors[-200:], **counts)
        raise

    return BatchResult.from_row(get_batch(batch_id))


# --- Phase 6 (CLAUDE.md section 28): distributed, multi-worker-safe batch --
# processing. run_acquisition_batch() above is single-process and remains
# the default (CLI/dashboard) path -- unchanged. The functions below are an
# ADDITIVE alternative for when several worker processes (possibly on
# separate machines sharing Postgres) need to process ONE large batch
# together without redundant work or duplicate companies/portals: each row
# is claimed via the same atomic lease pattern as the poll/verification
# queues (app.registry.acquisition_records / app.workers.leasing), and the
# underlying registry_companies/registry_portals unique-identity DB
# constraints are what actually guarantee no duplicates even under a race.


def create_distributed_batch(path: str | Path, *, source_name: Optional[str] = None, source_type: str = "CSV") -> int:
    """Reads the whole source once, creates the batch row, and seeds one
    registry_acquisition_records row per input row (PENDING). Call this
    ONCE per dataset (e.g. from an operator CLI command) -- every worker
    then calls process_distributed_batch_once() repeatedly against the
    returned batch_id."""
    path = Path(path)
    source_name = source_name or path.name
    candidates = list(read_candidates(path))
    batch_id = _create_batch(source_name=source_name, source_type=source_type, path=str(path))
    acquisition_records.seed_records(batch_id, candidates)
    _update_batch(batch_id, status="RUNNING", started_at=utcnow(), records_total=len(candidates))
    return batch_id


def process_distributed_batch_once(
    batch_id: int, *, worker_id: str, limit: int = 50, lease_seconds: int = 120, verify_new_candidates: bool = True,
) -> dict:
    """Claims and processes up to `limit` not-yet-done rows of an existing
    distributed batch. Safe to call concurrently from many workers/
    processes/machines against the same batch_id -- each row is claimed by
    exactly one caller at a time (app.registry.acquisition_records.claim_batch),
    and process_row()'s own identity-based upsert (plus the registry's
    unique indexes) means even a claimed-then-crashed-then-reclaimed row
    never produces a duplicate company/portal. Returns progress so far;
    marks the batch COMPLETED once every row is DONE or FAILED."""
    batch = get_batch(batch_id)
    if batch is None:
        raise ValueError(f"no such acquisition batch id={batch_id}")

    claimed = acquisition_records.claim_batch(
        batch_id=batch_id, worker_id=worker_id, limit=limit, lease_seconds=lease_seconds,
    )
    summary = ImportSummary(source_name=batch["source_name"], dry_run=False)

    for record in claimed:
        candidate = acquisition_records.candidate_from_record(record)
        try:
            portal_id = process_row(candidate, batch["source_name"], summary, dry_run=False)
        except Exception as exc:  # noqa: BLE001 - one bad row must never abort other workers' progress
            logger.warning("distributed acquisition row %s failed", record["row_index"], exc_info=True)
            acquisition_records.mark_failed(record["id"], error=str(exc))
            continue

        verification_result = ""
        company_id = None
        if portal_id is not None:
            if verify_new_candidates:
                _maybe_verify(portal_id, counts={"verified": 0, "active": 0, "quarantined": 0, "failed": 0})
            portal = store.get_portal(portal_id)
            if portal is not None:
                company_id = portal.company_id
                verification_result = portal.verification_status.value
        acquisition_records.mark_done(record["id"], company_id=company_id, portal_id=portal_id, verification_result=verification_result)

    progress = acquisition_records.batch_progress(batch_id)
    if progress["total"] > 0 and progress["pending"] == 0 and progress["claimed"] == 0:
        _update_batch(
            batch_id, status="COMPLETED", finished_at=utcnow(),
            records_processed=progress["done"] + progress["failed"],
        )
    return {"batch_id": batch_id, "claimed_this_call": len(claimed), "progress": progress}
