"""Durable document-upload binding (Real Provider Execution V1, migration 57).

The brief's DOCUMENT UPLOAD requirement: "Prove the exact application-specific
resume artifact is the one selected for upload. Bind job id, resume variant
id, resume hash, filename, upload target/provider field, timestamp/checkpoint.
Do the same for cover letter when present. Never silently substitute another
resume."

This project already verified a resume artifact's OWNERSHIP in two places
(`app.applications.executor._verify_resume_artifact` and
`app.applications.browser_assist._verify_resume`, both checking the
`/<job_id>/` path segment plus a SHA-256 of the bytes). What was missing was
the durable BINDING record: an append-only, queryable statement that at a
specific moment, this exact artifact hash was placed into this exact provider
form field for this exact job. Without it, "which resume actually went to
this employer?" could only be reconstructed from a path string on the job row
that a later regeneration may have already overwritten.

Design rules:
  - APPEND-ONLY. Every upload attempt is its own row (see migration 57's
    docstring for why there is deliberately no unique (execution, field)
    index). A binding is never mutated after the fact; a follow-up
    observation is a new row.
  - Best-effort, never-raising on the WRITE path (`record_binding_safe`) --
    an audit-log failure must never turn a genuinely successful upload into
    an error, matching `app.applications.checkpoints`/`spa_events`'s own
    contract. The strict `record_binding()` is available for tests/CLI and
    for callers that genuinely want the failure.
  - This module NEVER selects, substitutes, copies, or validates a document.
    It records what a caller already decided and already verified. The
    substitution guard itself is `verify_artifact_matches_job()` below, which
    is a pure, side-effect-free check callers run BEFORE uploading -- it can
    only ever say "no", never repair.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from app.db import db_session


class DocumentKind(str, Enum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_binding_id() -> str:
    return f"docb_{uuid.uuid4().hex}"


def sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class ArtifactCheck:
    ok: bool
    reason: str = ""
    sha256: str = ""
    filename: str = ""


def verify_artifact_matches_job(job_id: int, artifact_path: str) -> ArtifactCheck:
    """The "never silently substitute another resume" guard, as a pure
    function. Uses the SAME `/<job_id>/` path-segment convention
    `app.applications.executor._verify_resume_artifact`,
    `app.applications.doctor._check_wrong_resume_job_mapping` and
    `app.resume_optimizer.doctor._check_resume_linked_to_wrong_job` already
    agree on -- which deliberately accepts BOTH the legacy flat layout
    (`output/<job_id>/resume.pdf`) and the resume optimizer's nested one
    (`output/<job_id>/optimized/<variant_id>/resume.pdf`). Never re-narrow
    this to an exact immediate-parent match; that was a real integration bug
    (CLAUDE.md's One-Click Autonomous Agent rules)."""
    if not artifact_path:
        return ArtifactCheck(False, "no document artifact path supplied")
    path = Path(artifact_path)
    if not path.exists():
        return ArtifactCheck(False, f"document artifact missing on disk: {path}")
    normalized = str(path).replace("\\", "/")
    if f"/{job_id}/" not in normalized:
        return ArtifactCheck(
            False, f"document artifact path '{path}' does not belong to job {job_id}",
        )
    return ArtifactCheck(True, "", sha256_file(artifact_path), path.name)


def record_binding(
    *, job_id: int, document_kind: DocumentKind, artifact_path: str, provider: str = "",
    execution_id: str = "", session_id: str = "", resume_variant_id: str = "",
    provider_field_id: str = "", provider_field_label: str = "", checkpoint: str = "",
    verified: bool = False, detail: str = "", artifact_sha256: str = "",
) -> dict:
    """Inserts one append-only binding row and returns it. `artifact_sha256`
    may be supplied by a caller that already hashed the file (the executor
    and browser-assist both do) to avoid re-reading it; otherwise it is
    computed here. `verified` is INT-coerced explicitly -- psycopg maps a
    Python bool to Postgres `boolean`, which conflicts with this column's
    INTEGER type (CLAUDE.md Phase 9's boolean-coercion rule)."""
    kind = document_kind.value if isinstance(document_kind, DocumentKind) else str(document_kind)
    digest = artifact_sha256
    filename = Path(artifact_path).name if artifact_path else ""
    if not digest and artifact_path and Path(artifact_path).exists():
        digest = sha256_file(artifact_path)
    binding_id = new_binding_id()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO application_document_bindings
               (binding_id, job_id, execution_id, session_id, provider, document_kind, artifact_path,
                artifact_filename, artifact_sha256, resume_variant_id, provider_field_id,
                provider_field_label, checkpoint, verified, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (binding_id, job_id, execution_id or "", session_id or "", provider or "", kind,
             artifact_path or "", filename, digest or "", resume_variant_id or "", provider_field_id or "",
             (provider_field_label or "")[:300], checkpoint or "", int(bool(verified)), (detail or "")[:1000],
             utcnow()),
        )
        row = conn.execute(
            "SELECT * FROM application_document_bindings WHERE binding_id = ?", (binding_id,),
        ).fetchone()
    return dict(row)


def record_binding_safe(**kwargs) -> Optional[dict]:
    """Best-effort variant used on the live browser path -- an audit-log
    failure must never break a real upload."""
    try:
        return record_binding(**kwargs)
    except Exception:  # noqa: BLE001 -- observability must never break the caller
        return None


def list_bindings_for_job(job_id: int, limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_document_bindings WHERE job_id = ? ORDER BY id ASC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_bindings_for_execution(execution_id: str, limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_document_bindings WHERE execution_id = ? ORDER BY id ASC LIMIT ?",
            (execution_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_bindings_for_session(session_id: str, limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_document_bindings WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def latest_binding(job_id: int, document_kind: DocumentKind) -> Optional[dict]:
    kind = document_kind.value if isinstance(document_kind, DocumentKind) else str(document_kind)
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM application_document_bindings WHERE job_id = ? AND document_kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (job_id, kind),
        ).fetchone()
        return dict(row) if row else None
