"""Provider-parameterized submit-once execution claim (Canary Candidate Pool
Expansion + Multi-Provider Readiness V1).

`provider_submit_claims` (migration 62) generalizes migration 58's
`greenhouse_submit_claims` idiom -- one row per `(provider, execution_id)`,
`submit_attempted` flipped 0->1 by exactly one atomic
`UPDATE ... WHERE submit_attempted = 0` -- so a future Lever/Ashby/Workable
submit engine can reuse the identical physical "at most one submit click"
guarantee without inventing a new table per provider.

This module owns no submission logic of its own and, as of this phase, has
no engine calling `acquire_submit_claim()` for a real click yet -- no
Lever/Ashby/Workable submit engine exists. It exists so
`app.applications.provider_submit_contract` can honestly report claim state
(steps 7-8) today, matching the same read-only relationship
`app.applications.greenhouse_submit_contract` already has with
`greenhouse_submit_claim`. Greenhouse's own claim ledger
(`greenhouse_submit_claims`/`greenhouse_submit_claim.py`) is completely
unchanged by this module."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ClaimAttempt:
    acquired: bool
    row: Optional[dict]
    reason: str = ""


def get_claim(provider: str, execution_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM provider_submit_claims WHERE provider = ? AND execution_id = ?",
            (provider, execution_id),
        ).fetchone()
        return dict(row) if row else None


def _ensure_row(provider: str, execution_id: str, job_id: int) -> None:
    """Idempotent: creates the row if it doesn't exist yet. Never touches
    `submit_attempted` on an existing row."""
    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM provider_submit_claims WHERE provider = ? AND execution_id = ?",
            (provider, execution_id),
        ).fetchone()
        if existing is not None:
            return
        now = utcnow()
        try:
            conn.execute(
                """INSERT INTO provider_submit_claims
                   (provider, execution_id, job_id, claimed_at, claimed_by, submit_attempted,
                    created_at, updated_at)
                   VALUES (?, ?, ?, '', '', 0, ?, ?)""",
                (provider, execution_id, job_id, now, now),
            )
        except Exception as exc:  # noqa: BLE001 -- a concurrent racer already inserted; that's fine
            if "IntegrityError" not in type(exc).__name__ and "UniqueViolation" not in type(exc).__name__:
                raise


def already_attempted(provider: str, execution_id: str) -> bool:
    row = get_claim(provider, execution_id)
    return bool(row and row["submit_attempted"])


def acquire_submit_claim(provider: str, execution_id: str, job_id: int, *, claimed_by: str = "") -> ClaimAttempt:
    """The one atomic flip. Returns acquired=False (never raises) when a
    prior attempt already holds the claim -- the caller must treat this as
    BLOCKED and must never open a browser or perform any submit action."""
    _ensure_row(provider, execution_id, job_id)
    now = utcnow()
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE provider_submit_claims SET submit_attempted = 1, submit_attempted_at = ?, "
            "claimed_at = ?, claimed_by = ?, updated_at = ? "
            "WHERE provider = ? AND execution_id = ? AND submit_attempted = 0",
            (now, now, claimed_by, now, provider, execution_id),
        )
        won = cur.rowcount == 1
        row = conn.execute(
            "SELECT * FROM provider_submit_claims WHERE provider = ? AND execution_id = ?",
            (provider, execution_id),
        ).fetchone()
    if not won:
        return ClaimAttempt(False, dict(row) if row else None,
                             "a submit action was already attempted for this execution -- never retried")
    return ClaimAttempt(True, dict(row) if row else None, "submit-once claim acquired")


def record_outcome(provider: str, execution_id: str, *, outcome: str, detail: str = "") -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE provider_submit_claims SET outcome = ?, outcome_detail = ?, updated_at = ? "
            "WHERE provider = ? AND execution_id = ?",
            (outcome, (detail or "")[:2000], utcnow(), provider, execution_id),
        )


def list_claims(provider: str = "", limit: int = 200) -> list[dict]:
    with db_session() as conn:
        if provider:
            rows = conn.execute(
                "SELECT * FROM provider_submit_claims WHERE provider = ? ORDER BY id DESC LIMIT ?",
                (provider, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM provider_submit_claims ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
