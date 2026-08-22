"""PostgreSQL-safe work leasing (CLAUDE.md Phase 6 section 7).

The SQLite path in app/workers/leasing.py claims work with a per-row atomic
`UPDATE ... WHERE (unleased OR expired)` -- this is ALSO correct on
Postgres (MVCC's read-committed "EvalPlanQual" re-check means a second
transaction's UPDATE against a row just committed by another transaction
re-evaluates its WHERE clause against the fresh row, so it naturally claims
zero rows rather than double-claiming). But it does one UPDATE per
candidate row, including ones already leased by someone else, which wastes
round-trips under contention.

This module implements the idiomatic, more efficient Postgres pattern
instead: `SELECT ... FOR UPDATE SKIP LOCKED` grabs a batch of unlocked
candidate rows and holds their row locks for the rest of the transaction,
so a concurrent worker's own SKIP LOCKED select simply never sees them (no
wasted UPDATE attempts, no waiting). The subsequent per-row UPDATE within
the same transaction is then guaranteed to succeed (we already hold the
lock exclusively) and the whole claim -- select, lock, update every claimed
row, assign per-row attempt_ids -- commits as one atomic unit before
db_session() returns, satisfying the same "network call never happens
inside a DB transaction" rule as the SQLite path (the actual provider HTTP
request happens later, in app/workers/runner.py, entirely outside this
function)."""

from app.db import db_session
from app.registry.sharding import in_shard
from app.workers.leasing import _iso_plus, utcnow
from app.workers.repo import new_attempt_id


def _select_limit(limit: int, shard_count: int) -> int:
    """How many candidates to SELECT ... FOR UPDATE SKIP LOCKED before
    filtering/claiming. Unlike the SQLite path's flat _OVERFETCH multiplier
    (needed there to compensate for rows a per-row UPDATE...WHERE might lose
    a race on), SKIP LOCKED already skips anything another transaction holds
    -- so overfetching here doesn't help with contention, it only HURTS it:
    a real bug caught by this phase's own Postgres concurrency testing was
    a flat 4x overfetch causing one worker's single SELECT ... FOR UPDATE to
    lock far more rows than it would ever actually claim, starving every
    other concurrent worker of rows that were genuinely available (SKIP
    LOCKED correctly skips a LOCKED row, but a row this worker locked and
    then never updated is still locked until this transaction commits).
    The only reason to overfetch at all is sharding -- roughly 1/shard_count
    of any batch will match this worker's shard, so fetch proportionally
    more only when sharding is actually active."""
    if shard_count <= 1:
        return limit
    return limit * shard_count



def claim_poll_batch(
    *, worker_id: str, limit: int, lease_seconds: int, shard_count: int = 1, shard_index: int = 0,
) -> list[dict]:
    now = utcnow()
    claimed: list[dict] = []
    with db_session() as conn:
        candidates = conn.execute(
            """SELECT id FROM company_registry
               WHERE enabled = 1
                 AND (next_poll_at IS NULL OR next_poll_at <= ?)
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
               ORDER BY (next_poll_at IS NULL) DESC, next_poll_at ASC
               LIMIT ?
               FOR UPDATE SKIP LOCKED""",
            (now, now, _select_limit(limit, shard_count)),
        ).fetchall()

        for row in candidates:
            if len(claimed) >= limit:
                break
            portal_id = row["id"]
            if shard_count > 1 and not in_shard(portal_id, shard_count, shard_index):
                continue
            attempt_id = new_attempt_id()
            expires = _iso_plus(lease_seconds)
            conn.execute(
                """UPDATE company_registry
                   SET lease_owner = ?, lease_attempt_id = ?, lease_acquired_at = ?, lease_expires_at = ?
                   WHERE id = ?""",
                (worker_id, attempt_id, now, expires, portal_id),
            )
            full = conn.execute("SELECT * FROM company_registry WHERE id = ?", (portal_id,)).fetchone()
            claimed.append(dict(full))
    return claimed


def claim_verification_batch(
    *, worker_id: str, limit: int, lease_seconds: int, shard_count: int = 1, shard_index: int = 0,
    statuses: tuple[str, ...] = ("DISCOVERED", "CANDIDATE"),
) -> list[dict]:
    now = utcnow()
    claimed: list[dict] = []
    placeholders = ", ".join("?" for _ in statuses)
    with db_session() as conn:
        candidates = conn.execute(
            f"""SELECT id FROM registry_portals
                WHERE enabled = 1 AND verification_status IN ({placeholders})
                  AND (verify_lease_expires_at IS NULL OR verify_lease_expires_at <= ?)
                ORDER BY id ASC
                LIMIT ?
                FOR UPDATE SKIP LOCKED""",
            [*statuses, now, _select_limit(limit, shard_count)],
        ).fetchall()

        for row in candidates:
            if len(claimed) >= limit:
                break
            portal_id = row["id"]
            if shard_count > 1 and not in_shard(portal_id, shard_count, shard_index):
                continue
            attempt_id = new_attempt_id()
            expires = _iso_plus(lease_seconds)
            conn.execute(
                """UPDATE registry_portals
                   SET verify_lease_owner = ?, verify_lease_attempt_id = ?,
                       verify_lease_acquired_at = ?, verify_lease_expires_at = ?
                   WHERE id = ?""",
                (worker_id, attempt_id, now, expires, portal_id),
            )
            full = conn.execute("SELECT * FROM registry_portals WHERE id = ?", (portal_id,)).fetchone()
            claimed.append(dict(full))
    return claimed
