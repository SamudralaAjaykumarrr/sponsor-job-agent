# Distributed Workers (Phase 6)

Extends Phase 5's local multi-process worker fleet
(`docs/worker-architecture.md`) to genuinely separate machines, coordinating
only through a shared database.

## Worker identity

`app.workers.identity.WorkerIdentity` (unchanged fields: `worker_id`,
`hostname`, `pid` -- deliberately no candidate PII) gains:

- `worker_version`: `app.version.WORKER_SOFTWARE_VERSION`.
- `schema_version`: `app.migrations.CURRENT_SCHEMA_VERSION` this worker's
  code expects.
- `capability_version`: a short hash of every known provider's
  name+support-level+version (`app.version.capability_fingerprint()`) --
  changes whenever provider capabilities change, so a dashboard/operator
  can notice a worker running with a stale provider set.
- `backend`: `"sqlite"` or `"postgres"`.

All four are persisted on the `workers` row (`upsert_worker()`) and visible
on `/fleet`.

## Startup compatibility check

`Worker._check_schema_compatibility()` runs before a worker registers
itself: refuses to start (raises) if the live database's schema version is
*behind* what this worker's code expects (would hit real "column doesn't
exist" errors); only warns and proceeds if the database is *ahead* (a
rolling deployment with some workers already upgraded -- safe, since every
migration is additive). See `docs/database-migration.md`.

## Orphan reaper

`app.workers.reaper.reap_orphans(stale_after_seconds=...)` marks a worker
`OFFLINE` once its heartbeat is stale past a configurable threshold
(`ORPHAN_WORKER_STALE_SECONDS`, default 6x `WORKER_HEARTBEAT_SECONDS` --
deliberately several heartbeat intervals, not one, so ordinary jitter is
never mistaken for a crash). Called once per worker cycle
(`Worker.run()`'s loop) and exposed as a manual admin action
(`POST /fleet/reap-orphans`).

**This does not recover leases.** Lease recovery is, exactly as in Phase 5,
independent of this function ever running at all -- a lease's own
`lease_expires_at`/`verify_lease_expires_at` passing is the only mechanism
that frees a crashed worker's held work. The reaper only updates the
`workers` table's `status` column for dashboard/operator visibility.

## Shared circuit breaker + rate limiting

Both already lived in `provider_circuit_state`, a normal table Phase 5
built -- the moment `DATABASE_URL` points at a shared Postgres instance,
every worker process (on any machine) reads/writes the same row, so there
is exactly one fleet-wide circuit state and one fleet-wide
inflight-concurrency counter per provider. No new mechanism was needed for
"distributed rate limiting" (CLAUDE.md Phase 6 section 18) -- the existing
DB-backed permit/lease model (`app.workers.circuit.acquire_inflight_slot`)
already is that mechanism at real multi-machine scale.

Real Postgres validation of this (`scripts/multi_machine_simulation.py`,
run against real Postgres in `tests/test_multi_machine_simulation.py`)
proved:
- one simulated host's failures open the circuit; every other simulated
  host sees the same OPEN state immediately.
- a shared concurrency limit is never exceeded across simulated hosts
  racing for slots concurrently.

## Multi-machine simulation

`scripts/multi_machine_simulation.py` represents `host-A/worker-1`,
`host-A/worker-2`, `host-B/worker-1`, `host-C/worker-1` sharing one database
and processing synthetic portal work (provider name
`simulated-provider-fixture`, never colliding with a real provider). It
drives the real leasing/circuit/repo/reaper modules concurrently from
separate threads with separate DB connections (what actually matters for
correctness -- Postgres/SQLite serialize at the connection/transaction
level, not the Python thread level), verifying:

- unique lease ownership (no double-claims)
- shared circuit-breaker state
- shared rate/concurrency control
- worker heartbeats recorded correctly
- orphan recovery

Run standalone: `python -m scripts.multi_machine_simulation [--database-url
postgresql://...]`. Covered as an actual test in both modes:
`tests/test_multi_machine_simulation.py`.

## Honest limitation

This is real multi-*process*/multi-*thread* validation against a real
PostgreSQL server, proving the coordination logic is correct for
concurrent, independent connections. It is not a test across physically
separate machines/network -- Docker (which would have let this build spin
up a real multi-container demo) was unavailable in this environment
(confirmed: no `docker` binary in this WSL distro). The distinction that
matters for correctness (connection-level serialization, not
process/machine-level) is unaffected by this limitation, but it's stated
here rather than glossed over.
