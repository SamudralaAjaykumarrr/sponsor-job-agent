"""Submit-once execution claim for the Greenhouse Verified Submission
Contract V1 (`app.applications.greenhouse_submit_engine`).

`greenhouse_submit_claims` (migration 58) is APPEND-ONCE per execution: one
row, created lazily on first contact, whose `submit_attempted` flag is
flipped from 0 to 1 by exactly one atomic `UPDATE ... WHERE
submit_attempted = 0` -- the actual, physical guarantee that at most one
real submit click is ever attempted for a given execution, mirroring the
same atomic-claim idiom this project already uses everywhere a "did I win
the race" question needs a real answer
(`app.applications.approval._claim_ready_execution`, `app.workers.leasing`,
`app.applications.queue.claim_execution_batch`) rather than a
read-then-write check that a concurrent caller could race.

This module owns no submission logic of its own -- it is purely the claim
ledger. `app.applications.greenhouse_submit_engine` is the only caller that
may ever flip `submit_attempted`; `app.applications.greenhouse_submit_contract`
only ever reads it (to report "already attempted" in its pre-submit
picture)."""

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


def get_claim(execution_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM greenhouse_submit_claims WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return dict(row) if row else None


def _ensure_row(execution_id: str, job_id: int) -> None:
    """Idempotent: creates the row if it doesn't exist yet. Never touches
    `submit_attempted` -- a pre-existing row (from an earlier BLOCKED attempt
    that never reached the physical claim) is left exactly as it was."""
    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM greenhouse_submit_claims WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if existing is not None:
            return
        now = utcnow()
        try:
            conn.execute(
                """INSERT INTO greenhouse_submit_claims
                   (execution_id, job_id, claimed_at, claimed_by, submit_attempted, created_at, updated_at)
                   VALUES (?, ?, '', '', 0, ?, ?)""",
                (execution_id, job_id, now, now),
            )
        except Exception as exc:  # noqa: BLE001 -- a concurrent racer already inserted; that's fine
            if "IntegrityError" not in type(exc).__name__ and "UniqueViolation" not in type(exc).__name__:
                raise


def already_attempted(execution_id: str) -> bool:
    row = get_claim(execution_id)
    return bool(row and row["submit_attempted"])


def acquire_submit_claim(execution_id: str, job_id: int, *, claimed_by: str = "") -> ClaimAttempt:
    """The one atomic flip. Returns acquired=False (never raises) when a
    prior attempt already holds the claim -- the caller must treat this as
    BLOCKED and must never open a browser or perform any submit action."""
    _ensure_row(execution_id, job_id)
    now = utcnow()
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE greenhouse_submit_claims SET submit_attempted = 1, submit_attempted_at = ?, "
            "claimed_at = ?, claimed_by = ?, updated_at = ? "
            "WHERE execution_id = ? AND submit_attempted = 0",
            (now, now, claimed_by, now, execution_id),
        )
        won = cur.rowcount == 1
        row = conn.execute(
            "SELECT * FROM greenhouse_submit_claims WHERE execution_id = ?", (execution_id,)
        ).fetchone()
    if not won:
        return ClaimAttempt(False, dict(row) if row else None,
                             "a submit action was already attempted for this execution -- never retried")
    return ClaimAttempt(True, dict(row) if row else None, "submit-once claim acquired")


def record_outcome(
    execution_id: str, *, outcome: str, detail: str = "", final_url: str = "", heading_text: str = "",
    body_text_snippet: str = "", phrase_matched: Optional[bool] = None,
    heading_phrase_matched: Optional[bool] = None, submit_control_disappeared: Optional[bool] = None,
    form_fields_disappeared: Optional[bool] = None,
) -> None:
    """Greenhouse Confirmation Detection Forensics V1: the evidence kwargs
    are all optional (every pre-existing caller keeps working unchanged) and
    are recorded on EVERY outcome, not just CONFIRMED -- closing the
    observability gap that made jobs 454/291/342's real UNRECOGNIZED_OUTCOME
    failures impossible to diagnose after the fact. `heading_text`/
    `body_text_snippet` are truncated here (never trust the caller to have
    bounded them) -- bounded, page-authored diagnostic snippets, never a raw
    payload. `None` for any structural/phrase field means 'not observed for
    this outcome path' (e.g. a pre-click timeout never got body text) and is
    stored as SQL NULL, never coerced to a guessed False."""
    def _bool_to_int(v: Optional[bool]) -> Optional[int]:
        return None if v is None else int(v)

    with db_session() as conn:
        conn.execute(
            "UPDATE greenhouse_submit_claims SET outcome = ?, outcome_detail = ?, final_url = ?, "
            "heading_text = ?, body_text_snippet = ?, phrase_matched = ?, heading_phrase_matched = ?, "
            "submit_control_disappeared = ?, form_fields_disappeared = ?, updated_at = ? WHERE execution_id = ?",
            (outcome, (detail or "")[:2000], (final_url or "")[:2000], (heading_text or "")[:300],
             (body_text_snippet or "")[:500], _bool_to_int(phrase_matched), _bool_to_int(heading_phrase_matched),
             _bool_to_int(submit_control_disappeared), _bool_to_int(form_fields_disappeared), utcnow(),
             execution_id),
        )


def list_claims(limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM greenhouse_submit_claims ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
