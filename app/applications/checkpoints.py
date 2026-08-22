"""Session checkpoint log (CLAUDE.md Phase 13 sections 37-39). Append-only
observability layer on top of the existing, already-working resume
mechanism (`app.applications.browser_session` + `browser_assist.
resume_session`'s reconstruct-and-resume): `browser_assist_sessions.status`/
`stage` IS the session's actual current-state checkpoint, and reopening the
saved `application_url` + rediscovering from scratch IS how this project
recovers from a crash (CLAUDE.md Phase 11 section 45's "never claim exact
browser reattachment", unchanged and not reopened here). This module adds
the ORDERED HISTORY of meaningful reversible stages a session has passed
through, for audit/doctor/dashboard visibility -- it never itself performs
recovery, and recording a checkpoint never blocks or alters the session."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from app.db import db_session


class CheckpointStage(str, Enum):
    """CLAUDE.md Phase 13 section 37's exact list."""
    ENTRY_REACHED = "ENTRY_REACHED"
    FORM_DISCOVERED = "FORM_DISCOVERED"
    FIELDS_PREPARED = "FIELDS_PREPARED"
    FILE_READY = "FILE_READY"
    STEP_COMPLETED = "STEP_COMPLETED"
    READY_FOR_FINAL_SUBMIT = "READY_FOR_FINAL_SUBMIT"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"


# CLAUDE.md Phase 13 section 38: a coarse funnel ordering used only to flag
# an anomalous OUT-OF-ORDER checkpoint for doctor/review -- advisory only,
# never blocking, mirroring app.applications.apply_entry.
# is_valid_stage_transition's own "logged, not enforced" design. Multiple
# stages share a rank because they are not strictly ordered relative to each
# other (e.g. USER_ACTION_REQUIRED can legitimately occur at almost any
# point).
_STAGE_RANK: dict[str, int] = {
    CheckpointStage.ENTRY_REACHED.value: 0,
    CheckpointStage.FORM_DISCOVERED.value: 1,
    CheckpointStage.FIELDS_PREPARED.value: 2,
    CheckpointStage.FILE_READY.value: 2,
    CheckpointStage.STEP_COMPLETED.value: 2,
    CheckpointStage.USER_ACTION_REQUIRED.value: 2,
    CheckpointStage.READY_FOR_FINAL_SUBMIT.value: 3,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_checkpoint(session_id: str, checkpoint: CheckpointStage, *, job_id: int = None,
                       execution_id: str = "", detail: str = "") -> None:
    """Best-effort append -- never raises into a real browser discovery/fill
    pass, matching app.applications.spa_events.record's own contract."""
    value = checkpoint.value if isinstance(checkpoint, CheckpointStage) else checkpoint
    try:
        with db_session() as conn:
            conn.execute(
                """INSERT INTO application_checkpoints (session_id, job_id, execution_id, checkpoint, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, job_id, execution_id, value, detail[:500], utcnow()),
            )
    except Exception:  # noqa: BLE001 -- observability must never break the caller
        pass


def list_checkpoints(session_id: str, limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_checkpoints WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def latest_checkpoint(session_id: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM application_checkpoints WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,),
        ).fetchone()
        return dict(row) if row else None


@dataclass(frozen=True)
class CheckpointAnomaly:
    session_id: str
    from_checkpoint: str
    to_checkpoint: str
    reason: str


def find_ordering_anomalies(session_id: str) -> list[CheckpointAnomaly]:
    """CLAUDE.md Phase 13 section 62 'checkpoint inconsistency': flags a
    checkpoint whose rank is LOWER than one already recorded earlier in the
    same session, which would mean the session regressed to an earlier
    reversible stage without an intervening reconstruction. Advisory only --
    used by the doctor, never by the running session itself."""
    rows = list_checkpoints(session_id)
    anomalies: list[CheckpointAnomaly] = []
    best_rank = -1
    best_checkpoint = ""
    for row in rows:
        rank = _STAGE_RANK.get(row["checkpoint"], -1)
        if rank == -1:
            continue
        if best_rank != -1 and rank < best_rank and row["checkpoint"] != CheckpointStage.USER_ACTION_REQUIRED.value:
            anomalies.append(CheckpointAnomaly(
                session_id=session_id, from_checkpoint=best_checkpoint, to_checkpoint=row["checkpoint"],
                reason=f"checkpoint regressed from '{best_checkpoint}' (rank {best_rank}) to "
                       f"'{row['checkpoint']}' (rank {rank}) with no reconstruction recorded in between",
            ))
        if rank > best_rank:
            best_rank = rank
            best_checkpoint = row["checkpoint"]
    return anomalies
