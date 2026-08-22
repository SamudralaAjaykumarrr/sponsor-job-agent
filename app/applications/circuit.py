"""Submission-specific circuit breaker + cross-process inflight-concurrency
slot counter (CLAUDE.md Phase 9 section 34). A SEPARATE mechanism/table
(`application_provider_circuit_state`) from app.workers.circuit's discovery
breaker (`provider_circuit_state`) -- a provider's discovery polling being
paused must never automatically block application submission for that same
provider, and vice versa, since the failure semantics genuinely differ:
submission failures are rarer, more consequential (an in-flight submission
that we can't cleanly cancel), and should trip far more conservatively than
a discovery GET failing.

Mechanics mirror app.workers.circuit exactly (CLOSED/OPEN/HALF_OPEN, single
HALF_OPEN probe slot, bounded rolling window) -- only the table and the
(more conservative) thresholds differ. A provider is NEVER permanently
disabled by this breaker; it always eventually gets another HALF_OPEN
probe after APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import config
from app.db import db_session

_WINDOW_SIZE = 20
_MIN_ATTEMPTS_BEFORE_TRIP = 3


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ApplicationCircuitStatus:
    provider: str
    state: str
    consecutive_failures: int
    window_attempts: int
    window_failures: int
    opened_at: Optional[str]


def _ensure_row(conn, provider: str) -> None:
    conn.execute(
        """INSERT INTO application_provider_circuit_state (provider, state, updated_at)
           VALUES (?, 'CLOSED', ?) ON CONFLICT(provider) DO NOTHING""",
        (provider, utcnow()),
    )


def get_status(provider: str) -> ApplicationCircuitStatus:
    with db_session() as conn:
        _ensure_row(conn, provider)
        row = conn.execute(
            "SELECT * FROM application_provider_circuit_state WHERE provider = ?", (provider,)
        ).fetchone()
        return ApplicationCircuitStatus(
            provider=provider, state=row["state"], consecutive_failures=row["consecutive_failures"],
            window_attempts=row["window_attempts"], window_failures=row["window_failures"],
            opened_at=row["opened_at"],
        )


def may_attempt(provider: str) -> bool:
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        row = conn.execute(
            "SELECT * FROM application_provider_circuit_state WHERE provider = ?", (provider,)
        ).fetchone()
        state = row["state"]

        if state == "CLOSED":
            return True

        if state == "OPEN":
            opened_at = row["opened_at"]
            if not opened_at:
                return False
            try:
                opened_dt = datetime.fromisoformat(opened_at)
            except ValueError:
                return False
            cooldown_elapsed = datetime.now(timezone.utc) >= opened_dt + timedelta(
                seconds=config.APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS
            )
            if not cooldown_elapsed:
                return False
            cur = conn.execute(
                """UPDATE application_provider_circuit_state
                   SET state = 'HALF_OPEN', half_open_inflight = 1, half_open_probe_at = ?, updated_at = ?
                   WHERE provider = ? AND state = 'OPEN'""",
                (now, now, provider),
            )
            return cur.rowcount == 1

        if state == "HALF_OPEN":
            cur = conn.execute(
                """UPDATE application_provider_circuit_state
                   SET half_open_inflight = 1, half_open_probe_at = ?, updated_at = ?
                   WHERE provider = ? AND state = 'HALF_OPEN' AND half_open_inflight = 0""",
                (now, now, provider),
            )
            return cur.rowcount == 1

        return True


def record_result(provider: str, *, success: bool) -> None:
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        row = conn.execute(
            "SELECT * FROM application_provider_circuit_state WHERE provider = ?", (provider,)
        ).fetchone()
        state = row["state"]

        if state == "HALF_OPEN":
            if success:
                conn.execute(
                    """UPDATE application_provider_circuit_state
                       SET state='CLOSED', consecutive_failures=0, window_attempts=0, window_failures=0,
                           opened_at=NULL, half_open_inflight=0, updated_at=? WHERE provider=?""",
                    (now, provider),
                )
            else:
                conn.execute(
                    """UPDATE application_provider_circuit_state
                       SET state='OPEN', opened_at=?, half_open_inflight=0,
                           consecutive_failures=consecutive_failures+1, updated_at=? WHERE provider=?""",
                    (now, now, provider),
                )
            return

        consecutive = 0 if success else row["consecutive_failures"] + 1
        window_attempts = row["window_attempts"] + 1
        window_failures = row["window_failures"] + (0 if success else 1)

        should_trip = consecutive >= config.APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD or (
            window_attempts >= _MIN_ATTEMPTS_BEFORE_TRIP
            and (window_failures / window_attempts) >= config.APPLICATION_CIRCUIT_BREAKER_FAILURE_THRESHOLD
        )

        if window_attempts >= _WINDOW_SIZE:
            window_attempts, window_failures = 0, 0

        if should_trip and state == "CLOSED":
            conn.execute(
                """UPDATE application_provider_circuit_state
                   SET state='OPEN', opened_at=?, consecutive_failures=?, window_attempts=?, window_failures=?,
                       updated_at=? WHERE provider=?""",
                (now, consecutive, window_attempts, window_failures, now, provider),
            )
        else:
            conn.execute(
                """UPDATE application_provider_circuit_state
                   SET consecutive_failures=?, window_attempts=?, window_failures=?, updated_at=? WHERE provider=?""",
                (consecutive, window_attempts, window_failures, now, provider),
            )


def release_half_open_probe(provider: str) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE application_provider_circuit_state SET half_open_inflight = 0, updated_at = ? WHERE provider = ?",
            (utcnow(), provider),
        )


def acquire_inflight_slot(provider: str, limit: Optional[int] = None) -> bool:
    limit = limit if limit is not None else config.APPLICATION_PROVIDER_CONCURRENCY_DEFAULT
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        cur = conn.execute(
            "UPDATE application_provider_circuit_state SET inflight = inflight + 1, updated_at = ? "
            "WHERE provider = ? AND inflight < ?",
            (now, provider, limit),
        )
        return cur.rowcount == 1


def release_inflight_slot(provider: str) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE application_provider_circuit_state SET inflight = CASE WHEN inflight - 1 < 0 THEN 0 "
            "ELSE inflight - 1 END, updated_at = ? WHERE provider = ?",
            (utcnow(), provider),
        )


def force_close(provider: str) -> None:
    """Admin action -- always explicit, never automatic."""
    with db_session() as conn:
        _ensure_row(conn, provider)
        conn.execute(
            """UPDATE application_provider_circuit_state
               SET state='CLOSED', consecutive_failures=0, window_attempts=0, window_failures=0,
                   opened_at=NULL, half_open_inflight=0, updated_at=? WHERE provider=?""",
            (utcnow(), provider),
        )


def force_probe(provider: str) -> None:
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        conn.execute(
            """UPDATE application_provider_circuit_state
               SET state='HALF_OPEN', half_open_inflight=0, half_open_probe_at=?, updated_at=?
               WHERE provider=? AND state='OPEN'""",
            (now, now, provider),
        )


def all_states() -> dict[str, str]:
    with db_session() as conn:
        rows = conn.execute("SELECT provider, state FROM application_provider_circuit_state").fetchall()
        return {r["provider"]: r["state"] for r in rows}
