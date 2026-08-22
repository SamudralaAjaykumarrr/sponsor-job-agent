"""Duplicate-application protection (CLAUDE.md Phase 8 section 32). Two
layers: (1) app.applications.repo's partial unique index on
application_executions(job_id) WHERE active=1 is the atomic, DB-level guard
against two workers starting a second execution for the SAME job row
concurrently (see docs -- this is what section 61's concurrency test
exercises); (2) this module additionally checks whether a DIFFERENT job row
(same underlying posting via provider/external_job_id or canonical_url, or
the same company+title+location) has already reached APPLIED -- catching the
case where the same requisition was ingested twice as separate job rows
(e.g. re-synced before Phase 3 dedup logic ran, or a canonical_url that
changed between ingests)."""

from dataclasses import dataclass

from app.db import db_session
from app.models import ApplicationState, Job


@dataclass
class DuplicateCheckResult:
    is_duplicate: bool
    reason: str = ""
    duplicate_job_id: int | None = None


def check_duplicate(job: Job) -> DuplicateCheckResult:
    with db_session() as conn:
        if job.provider and job.external_job_id:
            row = conn.execute(
                "SELECT id FROM jobs WHERE provider = ? AND external_job_id = ? AND id != ? "
                "AND application_state = ?",
                (job.provider, job.external_job_id, job.id, ApplicationState.APPLIED.value),
            ).fetchone()
            if row:
                return DuplicateCheckResult(True, "same provider/external_job_id already APPLIED", row["id"])

        if job.canonical_url:
            row = conn.execute(
                "SELECT id FROM jobs WHERE canonical_url = ? AND id != ? AND application_state = ?",
                (job.canonical_url, job.id, ApplicationState.APPLIED.value),
            ).fetchone()
            if row:
                return DuplicateCheckResult(True, "same canonical_url already APPLIED", row["id"])

        row = conn.execute(
            "SELECT id FROM jobs WHERE company = ? AND title = ? AND location = ? AND id != ? "
            "AND application_state = ?",
            (job.company, job.title, job.location, job.id, ApplicationState.APPLIED.value),
        ).fetchone()
        if row:
            return DuplicateCheckResult(True, "same company/title/location already APPLIED", row["id"])

    return DuplicateCheckResult(False)
