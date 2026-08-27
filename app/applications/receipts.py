"""Durable submission receipts (Provider Post-Approval Execution V1, migration
55). An append-only, provider-labeled evidence record written ONLY at the
moment genuine confirmation evidence marks an execution APPLIED -- never
speculatively, never for a merely SUBMITTED/SUBMITTING execution.

Two, and only two, call sites ever write a receipt (both already gated by
this project's existing confirmation rules -- this module adds no new
confirmation logic of its own, it only durably records what those two paths
already decided):

  - `app.applications.executor.process_execution`'s headless-provider path,
    immediately after `provider.verify_confirmation()` returns
    `confirmed=True` (today only reachable for the deterministic `mock_ats`
    fixture -- no real provider has `submission_supported=True`).
  - `app.applications.browser_assist.attempt_user_submit_reconciliation`'s
    browser-observed manual-submit path, immediately after a
    STRONG/MODERATE `ConfirmationGrade` (see
    `app.applications.confirmation_evidence`) confirms the execution.

Never stores a raw cookie/token/password -- `sanitized_url` and
`raw_message_fingerprint` are exactly what the source already sanitized/
fingerprinted before this module ever sees them."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_receipt_id() -> str:
    return f"rcpt_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    execution_id: str
    job_id: int
    provider: str
    submitted_via: str
    confirmation_id: str
    sanitized_url: str
    evidence_strength: str
    raw_message_fingerprint: str
    session_id: str
    approval_id: str
    created_at: str


def record_receipt(
    *, execution_id: str, job_id: int, provider: str, submitted_via: str, confirmation_id: str = "",
    sanitized_url: str = "", evidence_strength: str = "NONE", raw_message_fingerprint: str = "",
    session_id: str = "", approval_id: str = "",
) -> dict:
    """Inserts one append-only receipt row. `submitted_via` is a short label
    identifying which of the two confirmation paths produced this evidence
    (e.g. "headless_provider:mock_ats" or "browser_assist:greenhouse") --
    never a claim of automated final-submission for a real provider; the
    label simply records what genuinely happened."""
    receipt_id = new_receipt_id()
    now = utcnow()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO application_receipts
               (receipt_id, execution_id, job_id, provider, submitted_via, confirmation_id, sanitized_url,
                evidence_strength, raw_message_fingerprint, session_id, approval_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, execution_id, job_id, provider or "", submitted_via, confirmation_id or "",
             sanitized_url or "", evidence_strength or "NONE", raw_message_fingerprint or "", session_id or "",
             approval_id or "", now),
        )
        row = conn.execute("SELECT * FROM application_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()

    # One-click-application-experience-v1 (CLAUDE.md section J): "application
    # submitted" is a meaningful, one-time-per-execution notification --
    # a receipt is only ever written once per execution (module docstring),
    # so this fires exactly once per genuinely confirmed application.
    from app import notifications

    notifications.notify(
        notifications.KIND_APPLIED, "Application submitted",
        f"Confirmed via {provider or 'the employer'}'s application system.",
        dedupe_key=f"receipt:{execution_id}", job_id=job_id, execution_id=execution_id,
    )
    return dict(row)


def get_receipt(receipt_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM application_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
        return dict(row) if row else None


def list_receipts_for_execution(execution_id: str) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_receipts WHERE execution_id = ? ORDER BY id ASC", (execution_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_receipt_for_execution(execution_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM application_receipts WHERE execution_id = ? ORDER BY id DESC LIMIT 1", (execution_id,)
        ).fetchone()
        return dict(row) if row else None


def list_receipts(*, provider: str = "", limit: int = 200) -> list[dict]:
    query = "SELECT * FROM application_receipts"
    params: list = []
    if provider:
        query += " WHERE provider = ?"
        params.append(provider)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
