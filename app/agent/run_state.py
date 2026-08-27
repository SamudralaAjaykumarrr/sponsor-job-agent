"""Durable state for the one-click autonomous agent orchestrator
(app/agent/orchestrator.py). Distinct from app.agent.state (the older,
in-memory-only Phase 2 discovery-cycle bookkeeping, which this module does
not replace -- the orchestrator's cycle still updates that module too, so
existing discovery-cycle dashboard/API consumers keep working unchanged).

`desired_state` is what the user last asked for (persisted to the DB so a
dashboard refresh, or a full process restart, never loses it -- see
"Restart Recovery" in the build brief). `actual_state` is what the
orchestrator's own background loop is really doing right now. They can
briefly disagree (e.g. desired=RUNNING, actual=STARTING) while a transition
is in flight.

Single-row table (id=1), read/written via app.db.db_session() like every
other piece of persisted state in this project -- never an in-process-only
flag for anything that must survive a restart."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from app.db import db_session


class AgentRunState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_run_state() -> dict:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM agent_run_state WHERE id = 1").fetchone()
        if row is None:
            # Defensive fallback -- the migration always inserts this row,
            # but a very old/partially-migrated DB should still degrade
            # safely to STOPPED rather than raise.
            return {
                "desired_state": AgentRunState.STOPPED.value, "actual_state": AgentRunState.STOPPED.value,
                "test_mode": False, "last_error": "", "started_at": None, "stopped_at": None,
                "start_count": 0, "stop_count": 0, "run_id": "", "cycle_number": 0,
                "last_cycle_started_at": None, "last_cycle_finished_at": None, "next_cycle_at": None,
                "heartbeat_at": None, "current_stage": "", "current_job_label": "",
            }
        d = dict(row)
        d["test_mode"] = bool(d["test_mode"])
        return d


def set_desired_state(state: AgentRunState, *, test_mode: bool = False) -> None:
    with db_session() as conn:
        if state == AgentRunState.RUNNING:
            conn.execute(
                "UPDATE agent_run_state SET desired_state = ?, test_mode = ?, start_count = start_count + 1, "
                "updated_at = ? WHERE id = 1",
                (state.value, int(test_mode), utcnow()),
            )
        elif state == AgentRunState.STOPPED:
            conn.execute(
                "UPDATE agent_run_state SET desired_state = ?, test_mode = ?, stop_count = stop_count + 1, "
                "updated_at = ? WHERE id = 1",
                (state.value, int(test_mode), utcnow()),
            )
        else:
            conn.execute(
                "UPDATE agent_run_state SET desired_state = ?, test_mode = ?, updated_at = ? WHERE id = 1",
                (state.value, int(test_mode), utcnow()),
            )


def set_actual_state(state: AgentRunState, *, last_error: str = "") -> None:
    now = utcnow()
    with db_session() as conn:
        fields = ["actual_state = ?", "updated_at = ?"]
        params: list = [state.value, now]
        if state == AgentRunState.RUNNING:
            fields.append("started_at = ?")
            params.append(now)
        if state == AgentRunState.STOPPED:
            fields.append("stopped_at = ?")
            params.append(now)
        if last_error or state == AgentRunState.ERROR:
            fields.append("last_error = ?")
            params.append(last_error)
        conn.execute(f"UPDATE agent_run_state SET {', '.join(fields)} WHERE id = 1", params)


# --- single-orchestrator-guarantee lease (autonomous-core-v3 hardening) ---
# Defensive safety net, not a distributed control plane -- see
# app/config.py's AGENT_ORCHESTRATOR_LEASE_SECONDS docstring. Same atomic
# `UPDATE ... WHERE (unowned OR lease-expired OR already-mine)` claim idiom
# as app.workers.leasing / app.applications.queue: correctness comes from
# the database's own single-writer serialization (SQLite WAL + busy_timeout,
# or Postgres MVCC), never an application-level lock, and a crashed lease
# holder recovers purely by the lease expiring -- never a heartbeat-based
# "is that process still alive" check.


def new_instance_id() -> str:
    return uuid.uuid4().hex[:12]


def try_acquire_orchestrator_lease(instance_id: str, lease_seconds: int) -> bool:
    """Atomically claims (or renews, if already owned by this instance_id)
    the single agent_run_state row's lease. Returns True iff this instance
    now holds it."""
    now = utcnow()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE agent_run_state SET instance_id = ?, lease_expires_at = ?, updated_at = ? "
            "WHERE id = 1 AND (lease_expires_at IS NULL OR lease_expires_at <= ? OR instance_id = ?)",
            (instance_id, expires, now, now, instance_id),
        )
        return cur.rowcount == 1


def renew_orchestrator_lease(instance_id: str, lease_seconds: int) -> bool:
    """Heartbeat/renewal for an already-held lease -- returns False if the
    lease was somehow already lost (expired and reclaimed by another
    instance), so the caller can stop treating itself as the active
    orchestrator rather than continuing to run cycles it no longer owns."""
    expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE agent_run_state SET lease_expires_at = ?, updated_at = ? WHERE id = 1 AND instance_id = ?",
            (expires, utcnow(), instance_id),
        )
        return cur.rowcount == 1


def release_orchestrator_lease(instance_id: str) -> None:
    """Best-effort early release on clean stop, guarded by instance_id so an
    instance can never release a lease it no longer owns (e.g. its own lease
    already expired and a different instance re-claimed it)."""
    with db_session() as conn:
        conn.execute(
            "UPDATE agent_run_state SET instance_id = '', lease_expires_at = NULL, updated_at = ? "
            "WHERE id = 1 AND instance_id = ?",
            (utcnow(), instance_id),
        )


# --- calm display state (autonomous-ux-reliability-v1 section G) ----------
# A derived, read-time projection over the SAME persisted fields above --
# never a second stored state machine (CLAUDE.md WORK MODE: "do not invent a
# second application lifecycle model", extended here to the orchestrator's
# own lifecycle). Purely for the dashboard's calm status area; every gate/
# scheduler decision in this project keeps reading the real AgentRunState
# values (actual_state/desired_state) unchanged.

DISPLAY_STATE_RUNNING = "RUNNING"
DISPLAY_STATE_PAUSED_BY_USER = "PAUSED_BY_USER"
DISPLAY_STATE_STOPPED = "STOPPED"
DISPLAY_STATE_RECOVERING = "RECOVERING"
DISPLAY_STATE_IDLE = "IDLE"


def display_state(run_state: dict) -> str:
    """Maps the real (actual_state, desired_state, run history) onto the
    five plain-language labels a calm status UI needs:

      RUNNING        -- actively working (including the brief STARTING
                         window for a genuinely first-ever start).
      RECOVERING      -- STARTING again with prior run history already on
                         record (cycle_number/last_cycle_started_at set) --
                         the exact restart-recovery scenario app.main's
                         lifespan handles by calling start() again when
                         desired_state was RUNNING across a process restart.
      PAUSED_BY_USER  -- stopped (or stopping) because desired_state was
                         explicitly set to STOPPED (the STOP AGENT action),
                         and it has run at least once before.
      STOPPED         -- halted despite desired_state still being RUNNING
                         (a lease loss, crash, or ERROR state not yet
                         recovered) -- an unexpected halt, not a user pause.
      IDLE            -- never started this install; nothing to resume.
    """
    actual = run_state.get("actual_state")
    desired = run_state.get("desired_state")
    has_run_before = bool(run_state.get("started_at"))

    if actual == AgentRunState.RUNNING.value:
        return DISPLAY_STATE_RUNNING
    if actual == AgentRunState.STARTING.value:
        if run_state.get("cycle_number") or run_state.get("last_cycle_started_at"):
            return DISPLAY_STATE_RECOVERING
        return DISPLAY_STATE_RUNNING
    if actual == AgentRunState.STOPPING.value:
        return DISPLAY_STATE_PAUSED_BY_USER if desired == AgentRunState.STOPPED.value else DISPLAY_STATE_STOPPED
    if actual == AgentRunState.ERROR.value:
        return DISPLAY_STATE_STOPPED
    # actual == STOPPED (or a legacy/unknown value -- degrade the same way)
    if desired == AgentRunState.RUNNING.value:
        return DISPLAY_STATE_STOPPED
    if has_run_before:
        return DISPLAY_STATE_PAUSED_BY_USER
    return DISPLAY_STATE_IDLE


def is_running() -> bool:
    """True only once the orchestrator's own loop has genuinely reached
    RUNNING -- never true merely because the user clicked START (that's
    STARTING until the first cycle is underway). This is what config-gated
    call sites (app.applications.executor/scheduler) check to decide whether
    the agent's one-click consent covers them."""
    return get_run_state()["actual_state"] == AgentRunState.RUNNING.value


def is_test_mode() -> bool:
    return get_run_state()["test_mode"]


@dataclass
class CycleCounters:
    jobs_processed: int = 0
    resumes_generated: int = 0
    one_page_success: int = 0
    one_page_overflow: int = 0
    one_page_compression_events: int = 0
    applications_prepared: int = 0
    applications_submitted: int = 0
    needs_user_action: int = 0
    skipped: int = 0
    errors: int = 0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "jobs_processed": self.jobs_processed, "resumes_generated": self.resumes_generated,
            "one_page_success": self.one_page_success, "one_page_overflow": self.one_page_overflow,
            "one_page_compression_events": self.one_page_compression_events,
            "applications_prepared": self.applications_prepared, "applications_submitted": self.applications_submitted,
            "needs_user_action": self.needs_user_action, "skipped": self.skipped, "errors": self.errors,
        }


def record_cycle(started_at: str, finished_at: str, *, test_mode: bool, counters: CycleCounters) -> int:
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO agent_cycle_log
               (started_at, finished_at, test_mode, jobs_processed, resumes_generated, one_page_success,
                one_page_overflow, one_page_compression_events, applications_prepared, applications_submitted,
                needs_user_action, skipped, errors, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                started_at, finished_at, int(test_mode), counters.jobs_processed, counters.resumes_generated,
                counters.one_page_success, counters.one_page_overflow, counters.one_page_compression_events,
                counters.applications_prepared, counters.applications_submitted, counters.needs_user_action,
                counters.skipped, counters.errors, json.dumps(counters.detail, default=str),
            ),
        )
        return cur.lastrowid


def list_recent_cycles(limit: int = 10) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_cycle_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def latest_cycle() -> Optional[dict]:
    rows = list_recent_cycles(limit=1)
    return rows[0] if rows else None


def totals_since(hours: Optional[float] = None) -> dict:
    """Live-aggregated totals over agent_cycle_log -- CLAUDE.md's existing
    'never an in-process counter, always a live DB query' metrics convention
    (see app/observability/metrics.py), extended to the agent_* counters."""
    query = (
        "SELECT COUNT(*) AS cycles, "
        "COALESCE(SUM(jobs_processed),0) AS jobs_processed, "
        "COALESCE(SUM(resumes_generated),0) AS resumes_generated, "
        "COALESCE(SUM(one_page_success),0) AS one_page_success, "
        "COALESCE(SUM(one_page_overflow),0) AS one_page_overflow, "
        "COALESCE(SUM(one_page_compression_events),0) AS one_page_compression_events, "
        "COALESCE(SUM(applications_prepared),0) AS applications_prepared, "
        "COALESCE(SUM(applications_submitted),0) AS applications_submitted, "
        "COALESCE(SUM(needs_user_action),0) AS needs_user_action, "
        "COALESCE(SUM(skipped),0) AS skipped, "
        "COALESCE(SUM(errors),0) AS errors "
        "FROM agent_cycle_log"
    )
    params: list = []
    if hours is not None:
        from datetime import timedelta

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        query += " WHERE started_at >= ?"
        params.append(since)
    with db_session() as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row)


# --- in-progress cycle tracking (CLAUDE.md production-v2 dashboard defect 1:
# "Agent Status = RUNNING but Last cycle = never, Next cycle = pending" must
# be impossible except for a brief STARTING window) --------------------------

def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def begin_run(run_id: str) -> None:
    """Called once when the orchestrator's loop actually starts (not merely
    when the user clicks START -- see AgentRunState.STARTING vs RUNNING)."""
    with db_session() as conn:
        conn.execute(
            "UPDATE agent_run_state SET run_id = ?, cycle_number = 0, next_cycle_at = NULL, "
            "heartbeat_at = ?, current_stage = 'starting', current_job_label = '', updated_at = ? WHERE id = 1",
            (run_id, utcnow(), utcnow()),
        )


def mark_cycle_start(started_at: str) -> int:
    """Records that a cycle is now IN PROGRESS -- distinct from 'never run'.
    Clears next_cycle_at (there is no 'next' while one is actively running)
    and returns the new cycle_number so callers/logs can reference it."""
    with db_session() as conn:
        conn.execute(
            "UPDATE agent_run_state SET cycle_number = cycle_number + 1, last_cycle_started_at = ?, "
            "heartbeat_at = ?, current_stage = 'discovering', current_job_label = '', "
            "next_cycle_at = NULL, updated_at = ? WHERE id = 1",
            (started_at, started_at, utcnow()),
        )
        row = conn.execute("SELECT cycle_number FROM agent_run_state WHERE id = 1").fetchone()
        return row["cycle_number"]


def heartbeat(*, stage: str = "", job_label: str = "") -> None:
    """Cheap, frequent liveness signal so the dashboard/watchdog can tell
    'RUNNING and genuinely working' from 'RUNNING but stuck' (see CLAUDE.md
    production-v2 section 5/39). Called once per pipeline stage per job at
    minimum -- never only once per whole cycle."""
    now = utcnow()
    with db_session() as conn:
        if stage:
            conn.execute(
                "UPDATE agent_run_state SET heartbeat_at = ?, current_stage = ?, current_job_label = ?, "
                "updated_at = ? WHERE id = 1",
                (now, stage, job_label, now),
            )
        else:
            conn.execute(
                "UPDATE agent_run_state SET heartbeat_at = ?, current_job_label = ?, updated_at = ? WHERE id = 1",
                (now, job_label, now),
            )


def mark_cycle_finish(finished_at: str, next_cycle_at: Optional[str]) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE agent_run_state SET last_cycle_finished_at = ?, next_cycle_at = ?, heartbeat_at = ?, "
            "current_stage = 'idle', current_job_label = '', updated_at = ? WHERE id = 1",
            (finished_at, next_cycle_at, finished_at, utcnow()),
        )


def heartbeat_age_seconds() -> Optional[float]:
    """None when the agent has never produced a heartbeat (e.g. STOPPED and
    never started this process lifetime) -- never 0/negative-as-unknown."""
    state = get_run_state()
    hb = state.get("heartbeat_at")
    if not hb:
        return None
    try:
        hb_dt = datetime.fromisoformat(hb)
    except ValueError:
        return None
    if hb_dt.tzinfo is None:
        hb_dt = hb_dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - hb_dt).total_seconds()


# --- agent-level activity log (CLAUDE.md production-v2 dashboard defect 7 /
# one-click-agent section 38): lifecycle/cycle events that have no natural
# job to attach to (Agent started, Discovery cycle started, Found N jobs,
# Agent stopped, Error/recovered, ...) -- distinct from
# app.pipeline_dashboard.build_recent_activity's job-level state/audit rows,
# merged with them for display. -------------------------------------------

_ACTIVITY_LOG_MAX_ROWS = 500


def log_activity(event: str, detail: str = "") -> None:
    """Best-effort: a logging failure must never interrupt the orchestrator
    loop it's called from."""
    try:
        with db_session() as conn:
            conn.execute(
                "INSERT INTO agent_activity_log (ts, event, detail) VALUES (?, ?, ?)",
                (utcnow(), event, detail),
            )
            conn.execute(
                "DELETE FROM agent_activity_log WHERE id NOT IN "
                "(SELECT id FROM agent_activity_log ORDER BY id DESC LIMIT ?)",
                (_ACTIVITY_LOG_MAX_ROWS,),
            )
    except Exception:  # noqa: BLE001
        pass


def list_recent_activity(limit: int = 20) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT ts, event, detail FROM agent_activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
