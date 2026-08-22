"""Per-provider circuit breaker + cross-process inflight-concurrency slot
counter, both backed by the `provider_circuit_state` table so the state is
shared by every worker process (not just one worker's memory) -- a provider
suffering widespread failures must be protected from ALL workers at once,
not just the one that happened to notice first. See CLAUDE.md Phase 5
section 10/11.

Circuit states:
  CLOSED     -- normal operation.
  OPEN       -- tripped; polling for this provider is paused fleet-wide
                except for one periodic low-frequency HALF_OPEN probe.
  HALF_OPEN  -- cooldown has elapsed; exactly one probe attempt is allowed
                through at a time. Success -> CLOSED (fresh state). Failure
                -> OPEN again with a fresh cooldown. A provider is never
                permanently disabled by this mechanism -- it always keeps
                retrying at a bounded, low frequency."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import config
from app.db import db_session
from app.workers.models import CircuitState

# Rolling-window sample size for the failure-rate evaluation. Deliberately a
# fixed internal constant (not user-configurable) -- CLAUDE.md's exposed
# settings are CIRCUIT_BREAKER_FAILURE_THRESHOLD (a fraction) and
# CIRCUIT_BREAKER_COOLDOWN_SECONDS; the window size is an implementation
# detail of how that fraction is estimated.
_WINDOW_SIZE = 20
_MIN_ATTEMPTS_BEFORE_TRIP = 5
# A short run of consecutive failures trips the circuit immediately even
# before the rolling window fills -- otherwise a sudden total outage would
# need to burn through `_MIN_ATTEMPTS_BEFORE_TRIP` failures one at a time.
_CONSECUTIVE_TRIP_THRESHOLD = 5


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CircuitStatus:
    provider: str
    state: str
    consecutive_failures: int
    window_attempts: int
    window_failures: int
    opened_at: Optional[str]


def _ensure_row(conn, provider: str) -> None:
    conn.execute(
        """INSERT INTO provider_circuit_state (provider, state, updated_at)
           VALUES (?, 'CLOSED', ?) ON CONFLICT(provider) DO NOTHING""",
        (provider, utcnow()),
    )


def get_status(provider: str) -> CircuitStatus:
    with db_session() as conn:
        _ensure_row(conn, provider)
        row = conn.execute("SELECT * FROM provider_circuit_state WHERE provider = ?", (provider,)).fetchone()
        return CircuitStatus(
            provider=provider, state=row["state"], consecutive_failures=row["consecutive_failures"],
            window_attempts=row["window_attempts"], window_failures=row["window_failures"],
            opened_at=row["opened_at"],
        )


def may_attempt(provider: str) -> bool:
    """Whether a worker is allowed to make a request to this provider right
    now. CLOSED: always. OPEN: only after the cooldown elapses (which flips
    it to HALF_OPEN and claims the single probe slot atomically here).
    HALF_OPEN: only if the single probe slot is currently free."""
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        row = conn.execute("SELECT * FROM provider_circuit_state WHERE provider = ?", (provider,)).fetchone()
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
                seconds=config.CIRCUIT_BREAKER_COOLDOWN_SECONDS
            )
            if not cooldown_elapsed:
                return False
            # Cooldown elapsed -- transition to HALF_OPEN and atomically
            # claim the single probe slot in the same statement so two
            # workers racing here can't both get a probe through.
            cur = conn.execute(
                """UPDATE provider_circuit_state SET state = 'HALF_OPEN', half_open_inflight = 1,
                     half_open_probe_at = ?, updated_at = ?
                   WHERE provider = ? AND state = 'OPEN'""",
                (now, now, provider),
            )
            return cur.rowcount == 1

        if state == "HALF_OPEN":
            cur = conn.execute(
                """UPDATE provider_circuit_state SET half_open_inflight = 1, half_open_probe_at = ?, updated_at = ?
                   WHERE provider = ? AND state = 'HALF_OPEN' AND half_open_inflight = 0""",
                (now, now, provider),
            )
            return cur.rowcount == 1

        return True


def record_result(provider: str, *, success: bool) -> None:
    """Feeds one attempt's outcome back into the breaker. Must be called
    exactly once per attempt that may_attempt() allowed through."""
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        row = conn.execute("SELECT * FROM provider_circuit_state WHERE provider = ?", (provider,)).fetchone()
        state = row["state"]

        if state == "HALF_OPEN":
            if success:
                conn.execute(
                    """UPDATE provider_circuit_state SET state='CLOSED', consecutive_failures=0,
                         window_attempts=0, window_failures=0, opened_at=NULL, half_open_inflight=0,
                         updated_at=? WHERE provider=?""",
                    (now, provider),
                )
            else:
                conn.execute(
                    """UPDATE provider_circuit_state SET state='OPEN', opened_at=?, half_open_inflight=0,
                         consecutive_failures=consecutive_failures+1, updated_at=? WHERE provider=?""",
                    (now, now, provider),
                )
            return

        consecutive = 0 if success else row["consecutive_failures"] + 1
        window_attempts = row["window_attempts"] + 1
        window_failures = row["window_failures"] + (0 if success else 1)

        should_trip = consecutive >= _CONSECUTIVE_TRIP_THRESHOLD or (
            window_attempts >= _MIN_ATTEMPTS_BEFORE_TRIP
            and (window_failures / window_attempts) >= config.CIRCUIT_BREAKER_FAILURE_THRESHOLD
        )

        if window_attempts >= _WINDOW_SIZE:
            # Roll the window so long-lived healthy providers don't carry
            # ancient failures forever.
            window_attempts, window_failures = 0, 0

        if should_trip and state == "CLOSED":
            conn.execute(
                """UPDATE provider_circuit_state SET state='OPEN', opened_at=?, consecutive_failures=?,
                     window_attempts=?, window_failures=?, updated_at=? WHERE provider=?""",
                (now, consecutive, window_attempts, window_failures, now, provider),
            )
        else:
            conn.execute(
                """UPDATE provider_circuit_state SET consecutive_failures=?, window_attempts=?,
                     window_failures=?, updated_at=? WHERE provider=?""",
                (consecutive, window_attempts, window_failures, now, provider),
            )


def release_half_open_probe(provider: str) -> None:
    """Safety net: if a HALF_OPEN probe attempt crashes before record_result
    runs, this frees the probe slot so the provider isn't stuck forever.
    record_result already clears it in the normal path."""
    with db_session() as conn:
        conn.execute(
            "UPDATE provider_circuit_state SET half_open_inflight = 0, updated_at = ? WHERE provider = ?",
            (utcnow(), provider),
        )


# --- Provider-scoped concurrency (inflight request slots) ------------------

def acquire_inflight_slot(provider: str, limit: Optional[int] = None) -> bool:
    """Atomically reserves one of `limit` concurrent-request slots for this
    provider, shared across every worker process. Returns False if the
    provider is already at its concurrency limit -- the caller should skip
    this attempt for now (the portal stays due and will be tried again next
    cycle) rather than block."""
    limit = limit if limit is not None else config.PROVIDER_CONCURRENCY_DEFAULT
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        cur = conn.execute(
            "UPDATE provider_circuit_state SET inflight = inflight + 1, updated_at = ? WHERE provider = ? AND inflight < ?",
            (now, provider, limit),
        )
        return cur.rowcount == 1


def release_inflight_slot(provider: str) -> None:
    with db_session() as conn:
        conn.execute(
            # CASE WHEN, not MAX(0, inflight - 1) -- SQLite's MAX() accepts 2+
            # scalar args, but Postgres's MAX() is aggregate-only (1 arg) and
            # would need GREATEST() instead. CASE WHEN is standard SQL that
            # behaves identically on both backends, avoiding yet another
            # backend-conditional statement (a real bug caught by this
            # phase's own Postgres integration testing).
            "UPDATE provider_circuit_state SET inflight = CASE WHEN inflight - 1 < 0 THEN 0 ELSE inflight - 1 END, "
            "updated_at = ? WHERE provider = ?",
            (utcnow(), provider),
        )


def force_close(provider: str) -> None:
    """Admin action (CLAUDE.md Phase 6 section 34): 'close circuit after
    validated recovery'. Always an explicit human/operator action, never
    automatic -- resets to a fresh CLOSED state with zeroed failure
    counters, exactly like a successful HALF_OPEN probe would."""
    with db_session() as conn:
        _ensure_row(conn, provider)
        conn.execute(
            """UPDATE provider_circuit_state SET state='CLOSED', consecutive_failures=0,
                 window_attempts=0, window_failures=0, opened_at=NULL, half_open_inflight=0,
                 updated_at=? WHERE provider=?""",
            (utcnow(), provider),
        )


def force_probe(provider: str) -> None:
    """Admin action: 'force provider probe' -- transitions an OPEN circuit
    straight to HALF_OPEN (bypassing the cooldown timer) so the very next
    attempt gets a real probe through, without waiting for
    CIRCUIT_BREAKER_COOLDOWN_SECONDS to elapse naturally. A no-op if the
    circuit isn't currently OPEN."""
    now = utcnow()
    with db_session() as conn:
        _ensure_row(conn, provider)
        conn.execute(
            """UPDATE provider_circuit_state SET state='HALF_OPEN', half_open_inflight=0,
                 half_open_probe_at=?, updated_at=? WHERE provider=? AND state='OPEN'""",
            (now, now, provider),
        )


def reset_inflight_slots(provider: Optional[str] = None) -> None:
    """Recovery tool for a worker crash that leaves inflight counters
    stranded above 0 with nothing actually running -- not called during
    normal operation. Exposed for the CLI/doctor."""
    with db_session() as conn:
        if provider:
            conn.execute("UPDATE provider_circuit_state SET inflight = 0, updated_at = ? WHERE provider = ?", (utcnow(), provider))
        else:
            conn.execute("UPDATE provider_circuit_state SET inflight = 0, updated_at = ?", (utcnow(),))
