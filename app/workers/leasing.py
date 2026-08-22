"""Database-backed leasing: the mechanism that lets multiple worker
processes (local, or in the future on separate machines) share the same
pool of portals without ever double-polling one concurrently.

Design (see docs/polling-leases.md for the full write-up):

  - Each claim is a single `UPDATE ... WHERE (unleased OR lease expired)`
    statement per candidate row. SQLite serializes all writers against one
    database file (WAL mode + busy_timeout, see app/db.py), so once one
    worker's UPDATE commits, a second worker's UPDATE for the same row sees
    the fresh lease_owner/lease_expires_at and its WHERE clause no longer
    matches -- it claims 0 rows for that id. This is what "atomic
    acquisition" means here: correctness comes from SQLite's single-writer
    serialization, not from any lock this code takes out itself.
  - A lease has an owner (worker_id), an attempt_id (so the eventual
    completion can be matched back to the exact claim that made it, even
    across a crash/retry), and an expiry. A worker that crashes mid-poll
    simply never clears the lease -- it naturally becomes reclaimable once
    lease_expires_at passes, satisfying "worker crash must not permanently
    lock work" without any crash-detection logic being required at all.
  - Sharding (REGISTRY_SHARD_COUNT/REGISTRY_SHARD_INDEX) is applied in
    Python against the candidate id list before the claim UPDATE runs, so a
    worker never even attempts to lease a portal outside its shard.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db import backend as db_backend
from app.db import db_session
from app.registry.sharding import in_shard
from app.workers.repo import new_attempt_id

# Overfetch factor: pull more due candidates than `limit` requires, since
# some may already be leased by another worker (still valid) or outside this
# worker's shard, without needing a second round trip.
_OVERFETCH = 4


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def utcnow() -> str:
    return utcnow_dt().isoformat()


def _iso_plus(seconds: int) -> str:
    return (utcnow_dt() + timedelta(seconds=seconds)).isoformat()


def claim_poll_batch(
    *, worker_id: str, limit: int, lease_seconds: int, shard_count: int = 1, shard_index: int = 0,
) -> list[dict]:
    """Claims up to `limit` due, unleased-or-lease-expired company_registry
    rows (the operational poll queue) for this worker. Returns full row
    dicts (including the freshly-assigned attempt_id) for exactly the rows
    this worker now owns.

    On the Postgres backend, delegates to app.workers.leasing_postgres's
    `SELECT ... FOR UPDATE SKIP LOCKED`-based claim (CLAUDE.md Phase 6
    section 7) -- more efficient under contention than the WHERE-guarded
    UPDATE loop below, though that loop is also correct on Postgres (MVCC's
    read-committed re-check semantics), just less efficient."""
    if db_backend() == "postgres":
        from app.workers import leasing_postgres

        return leasing_postgres.claim_poll_batch(
            worker_id=worker_id, limit=limit, lease_seconds=lease_seconds,
            shard_count=shard_count, shard_index=shard_index,
        )
    now = utcnow()
    claimed: list[dict] = []
    with db_session() as conn:
        candidates = conn.execute(
            """SELECT id FROM company_registry
               WHERE enabled = 1
                 AND (next_poll_at IS NULL OR next_poll_at <= ?)
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
               ORDER BY (next_poll_at IS NULL) DESC, next_poll_at ASC
               LIMIT ?""",
            (now, now, limit * _OVERFETCH),
        ).fetchall()

        for row in candidates:
            portal_id = row["id"]
            if shard_count > 1 and not in_shard(portal_id, shard_count, shard_index):
                continue
            attempt_id = new_attempt_id()
            expires = _iso_plus(lease_seconds)
            cur = conn.execute(
                """UPDATE company_registry
                   SET lease_owner = ?, lease_attempt_id = ?, lease_acquired_at = ?, lease_expires_at = ?
                   WHERE id = ? AND enabled = 1
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (worker_id, attempt_id, now, expires, portal_id, now),
            )
            if cur.rowcount == 1:
                full = conn.execute("SELECT * FROM company_registry WHERE id = ?", (portal_id,)).fetchone()
                claimed.append(dict(full))
            if len(claimed) >= limit:
                break
    return claimed


def claim_verification_batch(
    *, worker_id: str, limit: int, lease_seconds: int, shard_count: int = 1, shard_index: int = 0,
    statuses: tuple[str, ...] = ("DISCOVERED", "CANDIDATE"),
) -> list[dict]:
    """Same mechanism as claim_poll_batch, over registry_portals rows that
    still need the verification pipeline run against them. Same Postgres
    SKIP LOCKED delegation as claim_poll_batch above."""
    if db_backend() == "postgres":
        from app.workers import leasing_postgres

        return leasing_postgres.claim_verification_batch(
            worker_id=worker_id, limit=limit, lease_seconds=lease_seconds,
            shard_count=shard_count, shard_index=shard_index, statuses=statuses,
        )
    now = utcnow()
    claimed: list[dict] = []
    placeholders = ", ".join("?" for _ in statuses)
    with db_session() as conn:
        candidates = conn.execute(
            f"""SELECT id FROM registry_portals
                WHERE enabled = 1 AND verification_status IN ({placeholders})
                  AND (verify_lease_expires_at IS NULL OR verify_lease_expires_at <= ?)
                ORDER BY id ASC
                LIMIT ?""",
            [*statuses, now, limit * _OVERFETCH],
        ).fetchall()

        for row in candidates:
            portal_id = row["id"]
            if shard_count > 1 and not in_shard(portal_id, shard_count, shard_index):
                continue
            attempt_id = new_attempt_id()
            expires = _iso_plus(lease_seconds)
            cur = conn.execute(
                """UPDATE registry_portals
                   SET verify_lease_owner = ?, verify_lease_attempt_id = ?,
                       verify_lease_acquired_at = ?, verify_lease_expires_at = ?
                   WHERE id = ? AND enabled = 1
                     AND (verify_lease_expires_at IS NULL OR verify_lease_expires_at <= ?)""",
                (worker_id, attempt_id, now, expires, portal_id, now),
            )
            if cur.rowcount == 1:
                full = conn.execute("SELECT * FROM registry_portals WHERE id = ?", (portal_id,)).fetchone()
                claimed.append(dict(full))
            if len(claimed) >= limit:
                break
    return claimed


def release_poll_lease(portal_id: int, *, expected_attempt_id: Optional[str] = None) -> None:
    """Releases a lease early (successful/failed completion) so the row is
    immediately reclaimable rather than waiting for expiry. Guarded by
    expected_attempt_id when given, so a worker can never release a lease it
    no longer owns (e.g. because its own lease already expired and another
    worker re-claimed the row)."""
    with db_session() as conn:
        if expected_attempt_id is not None:
            conn.execute(
                """UPDATE company_registry SET lease_owner=NULL, lease_attempt_id=NULL,
                     lease_acquired_at=NULL, lease_expires_at=NULL
                   WHERE id = ? AND lease_attempt_id = ?""",
                (portal_id, expected_attempt_id),
            )
        else:
            conn.execute(
                """UPDATE company_registry SET lease_owner=NULL, lease_attempt_id=NULL,
                     lease_acquired_at=NULL, lease_expires_at=NULL WHERE id = ?""",
                (portal_id,),
            )


def release_verification_lease(portal_id: int, *, expected_attempt_id: Optional[str] = None) -> None:
    with db_session() as conn:
        if expected_attempt_id is not None:
            conn.execute(
                """UPDATE registry_portals SET verify_lease_owner=NULL, verify_lease_attempt_id=NULL,
                     verify_lease_acquired_at=NULL, verify_lease_expires_at=NULL
                   WHERE id = ? AND verify_lease_attempt_id = ?""",
                (portal_id, expected_attempt_id),
            )
        else:
            conn.execute(
                """UPDATE registry_portals SET verify_lease_owner=NULL, verify_lease_attempt_id=NULL,
                     verify_lease_acquired_at=NULL, verify_lease_expires_at=NULL WHERE id = ?""",
                (portal_id,),
            )


def extend_poll_lease(portal_id: int, attempt_id: str, *, lease_seconds: int) -> bool:
    """Heartbeat/renewal for long-running work -- extends an owned lease
    without changing ownership. Returns False if the lease was already lost
    (expired and reclaimed by someone else, or never owned)."""
    expires = _iso_plus(lease_seconds)
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE company_registry SET lease_expires_at = ? WHERE id = ? AND lease_attempt_id = ?",
            (expires, portal_id, attempt_id),
        )
        return cur.rowcount == 1


def extend_verification_lease(portal_id: int, attempt_id: str, *, lease_seconds: int) -> bool:
    expires = _iso_plus(lease_seconds)
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE registry_portals SET verify_lease_expires_at = ? WHERE id = ? AND verify_lease_attempt_id = ?",
            (expires, portal_id, attempt_id),
        )
        return cur.rowcount == 1


def count_active_poll_leases() -> int:
    now = utcnow()
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM company_registry WHERE lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (now,),
        ).fetchone()["c"]


def count_active_verification_leases() -> int:
    now = utcnow()
    with db_session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM registry_portals WHERE verify_lease_expires_at IS NOT NULL AND verify_lease_expires_at > ?",
            (now,),
        ).fetchone()["c"]
