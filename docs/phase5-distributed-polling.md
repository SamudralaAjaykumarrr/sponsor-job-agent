# Phase 5: Distributed Polling Execution Layer

Turns the Phase 3/4 discovery + registry architecture into a
production-oriented, fault-tolerant polling execution system: multiple local
worker processes (and, with a shared database, multiple machines in the
future) can share the work of polling a fleet of verified career portals
without double-polling, losing work, hammering providers, or corrupting
state. Also adds the acquisition tooling to grow the REAL verified registry.

Read this doc first; it links to the detailed ones:

- **`docs/worker-architecture.md`** — the `Worker` execution loop, module
  map, retry/circuit-breaker mechanics, graceful shutdown, queue
  abstraction.
- **`docs/polling-leases.md`** — the atomic leasing mechanism in depth
  (this is the load-bearing correctness guarantee of the whole phase).
- **`docs/registry-acquisition.md`** — the resumable batch executor that
  grows the real registry.
- **`docs/fleet-operations.md`** — operator's guide: CLI, dashboard,
  running 1/2/4 workers locally, the dead-letter workflow.
- **`docs/scaling-claims.md`** — the honest-numbers policy and exactly what
  this phase did and didn't prove about scale.

## What's genuinely new vs. what's reused unchanged

**New (this phase):**
- `app/workers/` — the entire package (identity, models, repo, leasing,
  queue, retry, circuit, schema_check, dead_letter, metrics, runner,
  supervisor, cli).
- `app/registry/acquisition.py` — resumable acquisition batch executor.
- 8 new/changed database tables/columns, all additive (`app/db.py`).
- `/fleet` and `/acquisition` dashboard pages + `/fleet/metrics` JSON.
- `registry acquire`/`batches`/`resume` CLI subcommands.
- `scripts/worker_benchmark.py`.

**Reused, unmodified in behavior:**
- `app.agent.cycle.process_raw_job` (the entire fetch→filter→dedupe→store→
  analyze pipeline) — the worker calls the exact same function the legacy
  scheduler does.
- `app.registry.verification.verify_portal` / `lifecycle.py` /
  `sync.py` — the entire Phase 4 verification/lifecycle/sync pipeline.
- `app.registry.scheduling` — the Phase 3 deterministic adaptive-polling
  backoff rules; the worker calls `mark_poll_result` exactly like the
  legacy `_discover_from_registry` did.
- `app.registry.sharding` — the Phase 4 hash-based shard assignment,
  now actually enforced by `app/workers/leasing.py`.
- Every sponsorship/gate/scoring/resume-generation rule in
  `app/pipeline.py`, `app/sponsorship/`, `app/matching/`, `app/resume/` —
  untouched.

## Architecture at a glance

```
Verified ACTIVE portal (company_registry row)
        |
        v
claim_poll_batch()  <-- atomic, sharded, bounded ---------- app/workers/leasing.py
        |
        v
circuit.may_attempt() + acquire_inflight_slot()  ----------- app/workers/circuit.py
        |
        v
probe (raises) -> schema_check -> fetch_jobs() -------------- reused Phase 3/4 code
        |
        v
process_raw_job() (dedupe/store/analyze/generate) ----------- app/agent/cycle.py, unchanged
        |
        v
mark_poll_result() (scheduling) ------------------------------ app/registry/repo.py, unchanged
        |
        v
record_attempt() + release lease ----------------------------- app/workers/repo.py, app/workers/leasing.py
        |
        v
next_poll_at in the future -> claimable again once due
```

The verification queue (`registry_portals` rows still `DISCOVERED`/
`CANDIDATE`) runs through the identical claim→execute→record→release shape,
substituting Phase 4's `verify_portal`/`apply_verification_outcome`/
`sync_portal_to_operational_registry` for the fetch/store/analyze step.

## What was actually verified this phase (see the completion report for full numbers)

- **Atomic leasing**: proven with real, separate OS processes (not just
  threads) — 6 concurrent processes racing to claim 40 rows: 40 claimed,
  0 duplicates.
- **Local multi-worker acceptance**: 100 synthetic portals, 4 real worker
  threads across 4 shards, in `tests/test_acceptance_scenarios_phase5.py`
  — every eligible portal attempted exactly once, no duplicate leases,
  crash recovery, retry, dead-letter (below threshold, correctly not
  triggered), empty-board handling, and re-poll deduplication all in one
  deterministic run.
- **Synthetic benchmark**: 1k/10k/50k/100k synthetic `company_registry`
  rows in an isolated temp DB — bounded due-queries stay under ~25ms even
  at 100k rows; 8-worker contention drains 100k rows with zero duplicate
  claims (`scripts/worker_benchmark.py`).
- **Limited live validation**: 2 real worker processes against the actual
  small verified registry (19 real ACTIVE portals), real internet, real
  jobs discovered and run through the full downstream analysis/resume
  pipeline — see `docs/scaling-claims.md` and the completion report.
- **Real registry growth**: a new 6-company real seed run through the
  acquisition pipeline against the real database with real live
  verification — 3 verified/active, 3 correctly left as unverified
  candidates (a wrong guessed tenant, not a bug).
- **312 pre-existing tests still pass, unmodified in behavior** (one test's
  hardcoded CLI-subcommand list was updated to include the 3 new
  subcommands — an expected, not a regression). 110 new Phase 5 tests
  added, all passing.

## Honest limitations (see the completion report's full list)

- SQLite is a real multi-*process*, single-machine concurrency mechanism —
  it is not a distributed database; multi-machine operation needs
  PostgreSQL + an external queue (see `docs/scaling-claims.md`).
- Schema-drift detection and true per-request retry/permanent
  classification depend on a raw structural probe, which only exists for
  the 10 providers `app/registry/probe.py` implements — by construction the
  only providers that can ever reach `ACTIVE` status, so there is no
  silent coverage gap for anything the fleet actually polls, but a
  manually-added `company_registry` entry for an un-probed provider gets
  weaker observability (fetch_jobs() still swallows its own errors there,
  same as pre-Phase-5).
- `PROVIDER_CONCURRENCY_DEFAULT` (default 3) means a single bounded
  `--once` cycle does not always fully drain a provider with many tenants
  in one pass — continuous operation converges over successive cycles;
  this is intentional rate-limiting behavior, not a bug, but it means
  `--once` is a testing/scripting tool, not how a real deployment should
  run.
