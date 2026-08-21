# Polling Leases

How the Phase 5 distributed polling layer guarantees that no two workers ever
poll the same portal at the same time, and that a crashed worker never
permanently locks work. Implementation: `app/workers/leasing.py`.

## Why leasing lives on the domain tables, not a separate queue table

There are two independent lease-able pools:

- **Poll queue** — `company_registry` rows (the Phase 3 operational polling
  table; a row here is, by construction, an ACTIVE portal that's been
  mirrored in by `app/registry/sync.py`).
- **Verification queue** — `registry_portals` rows still in
  `DISCOVERED`/`CANDIDATE` status.

Rather than a generic `work_leases` table decoupled from these, Phase 5 adds
four nullable columns directly to each table:

```
lease_owner, lease_attempt_id, lease_acquired_at, lease_expires_at            -- company_registry
verify_lease_owner, verify_lease_attempt_id,
verify_lease_acquired_at, verify_lease_expires_at                             -- registry_portals
```

A row of `company_registry` (or `registry_portals`) already *is* one unit of
work — there's no benefit to indirection through a second table, and keeping
lease state on the same row means a single atomic `UPDATE ... WHERE` claims
it, with no join required.

## The claim algorithm

```python
def claim_poll_batch(*, worker_id, limit, lease_seconds, shard_count=1, shard_index=0):
    now = utcnow()
    candidates = SELECT id FROM company_registry
                 WHERE enabled=1 AND due (next_poll_at <= now)
                   AND (unleased OR lease_expires_at <= now)
                 ORDER BY ... LIMIT limit * OVERFETCH

    for portal_id in candidates:
        if not in_shard(portal_id, shard_count, shard_index):
            continue
        UPDATE company_registry
        SET lease_owner=?, lease_attempt_id=?, lease_acquired_at=?, lease_expires_at=?
        WHERE id=? AND (unleased OR lease_expires_at <= now)      # <- the atomic part
        if rowcount == 1:
            claimed.append(portal_id)
```

**Correctness comes from SQLite's own writer serialization, not from any
lock this code takes out itself.** SQLite (in WAL mode, see below) allows
many concurrent readers but only one writer at a time; once worker A's
`UPDATE` commits, worker B's `UPDATE` for the *same row* is evaluated against
the now-current `lease_expires_at`, and its `WHERE` clause no longer
matches — `rowcount` is 0, and worker B correctly claims nothing for that
row. This was verified with real, separate OS processes (not just threads,
which share a GIL and would prove less), see
`tests/test_workers_leasing.py` and the ad hoc multi-process check run
during this phase's own build (8 concurrent processes racing to claim 40
rows: 40 claimed, 0 duplicates, every time).

## Why sharding is applied in Python, not SQL

`shard_for_portal(portal_id, shard_count)` (from Phase 4's
`app/registry/sharding.py`, unchanged) is a pure hash function. Applying it
in Python against the already-fetched candidate list, before the claim
`UPDATE`, means a worker never even attempts to claim a row outside its
shard — cheaper than adding a `WHERE shard_column = ?` that would require
either a stored, denormalized shard column (which would need
re-materializing whenever `REGISTRY_SHARD_COUNT` changes) or a SQL
expression of the same hash (slower, less portable). The shard assignment is
proven, in tests, to (a) map every portal to exactly one shard, (b) never
overlap between shards, and (c) be stable across repeated calls (same
`portal_id` + `shard_count` always yields the same shard).

**Changing `REGISTRY_SHARD_COUNT` reshuffles assignments.** This is
inherent to hash-based sharding (not a Phase 5-specific limitation) — if you
resize a running fleet, some portals will map to a different shard index
than before. Because a lease is what's actually exclusive (not "shard
ownership" as a standing claim), this is safe: a portal simply becomes
claimable by whichever worker now owns its new shard once its current lease
(if any) expires. Nothing is silently reshuffled while actively leased.

## Lease lifecycle

| Event | What happens |
|---|---|
| Successful completion | `ack()` — lease columns set back to NULL immediately, row reclaimable right away (not waiting for expiry) |
| Retryable failure | `retry()` — lease released; the *real* next-attempt schedule is `next_poll_at` (already advanced by `app.registry.scheduling`'s existing exponential backoff) or, for the verification queue (which has no `next_poll_at`), a short cooldown applied via `extend_lease` instead of a bare release (see below) |
| Permanent failure | `fail()` — lease released; `consecutive_permanent_failures` incremented; may trigger dead-lettering (see `docs/fleet-operations.md`) |
| Worker crash | Lease is simply never released. Once `lease_expires_at` passes, `claim_*_batch`'s `WHERE` clause treats it as unleased again — **no crash-detection logic exists or is needed.** Verified in `test_worker_crash_recovery_lease_expires_and_is_reclaimed`. |
| Heartbeat / long-running work | `extend_lease()` renews `lease_expires_at` without changing ownership; returns `False` if the lease was already lost (already reclaimed by someone else) |

### The verification queue's backoff trick

`registry_portals` has no `next_poll_at` column (verification isn't
scheduled on a fixed cadence the way ACTIVE polling is). So a
`TEMPORARY_FAILURE`, an early `FAILED` (below the demotion threshold), or an
`UNSUPPORTED` provider — none of which move the portal out of
`DISCOVERED`/`CANDIDATE` — would otherwise be immediately reclaimable again,
causing a tight retry loop. Rather than adding a new column, the runner
reuses `extend_verification_lease()` as a cooldown: it "holds" the lease a
little longer (exponential backoff via `app.workers.retry.backoff_seconds`,
capped at 6 hours) instead of releasing it, so the row is provably not
reclaimed again until the cooldown passes. Proven in
`test_verification_queue_failed_portal_backs_off_not_hot_loop`.

The same mechanism (`_SKIP_COOLDOWN_SECONDS`, a short 5s cooldown) is also
used on the *poll* side when a claimed item is cancelled before ever being
attempted — because the provider's circuit is open, or its concurrency
budget is full. Releasing outright there would let many workers/threads
re-claim and re-cancel the same row in a tight busy-spin; a short cooldown
avoids that while still trying again well within the same or the next
cycle. This was a real bug caught during this phase's own local multi-worker
acceptance test (100 portals, 4 workers): before the fix, a shared
provider's tight concurrency budget produced a burst of claim → cancel →
immediate-reclaim → cancel cycles that inflated attempt history without
doing real work.

## SQLite concurrency configuration

`app/db.py::get_connection()` sets, on every connection:

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;
PRAGMA synchronous = NORMAL;
```

WAL lets readers (dashboard requests, CLI `status`/`attempts`) proceed while
a worker holds the write lock; `busy_timeout` makes a second writer *wait*
(up to 30s) for the first to finish rather than immediately raising
`database is locked`. All lease-claim transactions are short — the network
call (the actual HTTP request to a provider) always happens *outside* any
`db_session()` block, never inside one, per CLAUDE.md's SQLite-safety rules.

**Honest limit**: this is still one SQLite *file*. It gives correct,
serialized, multi-*process* concurrency on one machine (or one shared
filesystem) — it is not a distributed database, and does not extend to
multiple *machines* without a shared network filesystem (which SQLite does
not support safely) or a swap to a real client-server database. See
`docs/scaling-claims.md`.
