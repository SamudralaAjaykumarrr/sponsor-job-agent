# Phase 6: Production-Scale Distributed Architecture

Phase 6 moves Sponsor Job Agent from a strong single-machine/local
multi-process architecture (Phase 5) toward a genuine production-scale
distributed architecture, without regressing any candidate-truthfulness or
sponsorship-protection rule established in Phases 1-5.

This document is the map; see the cross-referenced docs for the detail on
each subsystem.

## What actually changed

| Area | Phase 5 | Phase 6 |
|---|---|---|
| Database | SQLite only | SQLite (default) **or** PostgreSQL, same code, `DATABASE_URL` selects |
| Schema changes | Ad hoc idempotent DDL in `app/db.py` | Versioned migration framework (`app/migrations.py`), `schema_migrations` table |
| Work leasing | Per-row atomic `UPDATE...WHERE` (SQLite) | Same on SQLite; `SELECT...FOR UPDATE SKIP LOCKED` on Postgres (`app/workers/leasing_postgres.py`) |
| Worker identity | `worker_id`/hostname/pid | + software/schema/capability version + backend (`app/workers/identity.py`) |
| Crash detection | Lease expiry only | + orphan reaper marks stale workers `OFFLINE` for visibility (`app/workers/reaper.py`) |
| Provider errors | Swallowed internally, empty list on failure | `ProviderFetchResult` typed status (`app/providers/errors.py`), fed into circuit breaker/attempt history |
| Schema drift | Per-attempt boolean check | + persistent `provider_schema_drift` table, provider-wide drift feeds circuit breaker |
| Registry acquisition | Single-process resumable batch | + distributed, lease-based per-row checkpointing (`app/registry/acquisition_records.py`) safe for many workers |
| Domain seeding | `page_discovery` existed but unwired | Wired into a real bulk pipeline (`app/registry/domain_seed.py`) |
| Sponsorship intelligence | N/A | Storage foundation only (`app/sponsorship/evidence.py`) -- never confirms a job |
| Observability | Dashboard-only metrics | + `/metrics` (Prometheus text), `/readiness`, structured JSON logging, correlation ids |
| Migration tooling | N/A | `python -m app.db_migrate sqlite-to-postgres` |

## Critical goals and where they're satisfied

1. **PostgreSQL as a real shared backend** -- `app/db_postgres.py`,
   `docs/postgres-backend.md`.
2. **True multi-machine-safe worker coordination** -- `app/workers/
   leasing_postgres.py` (SKIP LOCKED), shared `provider_circuit_state`,
   `docs/distributed-workers.md`.
3. **Production queue/backend abstraction** -- `app/workers/queue.py`
   (`WorkQueue` interface, unchanged) backed by either SQLite or Postgres
   leasing underneath.
4. **Distributed worker simulation/integration test** --
   `scripts/multi_machine_simulation.py`, `tests/test_multi_machine_simulation.py`
   (runs against both backends; the Postgres variant is real, not mocked).
5. **Robust provider error propagation** -- `app/providers/errors.py`,
   `docs/provider-error-contract.md`.
6. **Larger legitimate registry acquisition** -- `app/registry/domain_seed.py`,
   `app/registry/acquisition_records.py`, `docs/registry-acquisition.md`.
7. **Production-grade monitoring/metrics** -- `app/observability/metrics.py`,
   `docs/production-observability.md`.
8. **Honest monitored-scale accounting** -- `docs/scaling-claims.md`
   (unchanged durable rule, extended for Phase 6 numbers).
9. **Zero regression in candidate truthfulness/sponsorship protections** --
   `app/sponsorship/classifier.py` is completely untouched; the new
   `app/sponsorship/evidence.py` table is never imported by it (see
   `docs/sponsorship_rules.md` and the durable rule restated in
   `app/sponsorship/evidence.py`'s own docstring).

## Real bugs this phase's own testing caught (and fixed)

Honesty over polish -- these were caught by actually running the new code
against real PostgreSQL and real live providers, not just imagined:

1. **`MAX(0, inflight - 1)` is SQLite-only.** SQLite's `MAX()` accepts 2+
   scalar arguments; Postgres's `MAX()` is aggregate-only (1 arg) and raised
   `UndefinedFunction`. Fixed with a portable `CASE WHEN` in
   `app/workers/circuit.py::release_inflight_slot`.
2. **A flat 4x overfetch in the Postgres SKIP LOCKED claim path caused
   worker starvation, not just inefficiency.** `SELECT ... FOR UPDATE SKIP
   LOCKED LIMIT limit*4` locks every row it selects, even the ones the
   caller's loop never actually claims -- one worker's single call could
   lock far more rows than it used, and concurrent workers' own SKIP LOCKED
   selects then saw nothing available. Fixed in `app/workers/
   leasing_postgres.py::_select_limit` -- only overfetch proportional to
   `shard_count`, never a flat multiplier.
3. **Concurrent worker startup against a shared Postgres database could
   deadlock, or hit a UniqueViolation on a system catalog entry, on
   concurrent schema DDL.** Both are well-known, expected PostgreSQL
   behaviors under concurrent `ALTER TABLE ADD COLUMN IF NOT EXISTS` /
   `CREATE TABLE IF NOT EXISTS` -- caught by a real 2-worker (and later
   6-thread) live run against real Postgres. Fixed structurally with a
   session advisory lock (`app/db_postgres.py::acquire_schema_lock`/
   `release_schema_lock`) that serializes schema DDL across every process
   sharing the database, with a bounded retry-with-jitter helper
   (`run_with_deadlock_retry`) as defense-in-depth.
4. **`app.providers.detector`'s Greenhouse rule mis-extracted the tenant
   from an API-shaped URL.** A real company's (Duolingo's) careers page
   linked directly to `boards-api.greenhouse.io/v1/boards/duolingo/departments`
   -- the naive "first path segment" heuristic extracted `"v1"` as the
   tenant instead of `"duolingo"`. Caught during this phase's own real
   domain-seed acquisition validation (see below). Fixed in
   `app/providers/detector.py::_rule_greenhouse`.
5. **Phase 5's provider-fetch error swallowing was architecturally
   invisible to the circuit breaker.** Not a crash, but a real correctness
   gap: a real `fetch_jobs()` failure after a successful structural probe
   was recorded as a healthy empty poll. Fixed via `ProviderFetchResult`
   (see `docs/provider-error-contract.md`) -- this was Phase 6's
   headline architectural fix, not an incidental bug.

## Honest limitations (see CLAUDE.md section 56 for the full list)

- A local multi-thread/multi-process Postgres test (this build's actual
  validation) is not proof of true cross-machine network behavior --
  Docker was unavailable in this build environment (confirmed: `docker`
  binary not found in this WSL distro), so the multi-service Docker Compose
  demo (`deploy/docker-compose.postgres.yml`) is written and YAML-validated
  but **not run** in this build. The coordination *logic* itself (leasing,
  circuit breaker, rate limiting, orphan reaping) was validated against a
  real PostgreSQL server (via `pgserver`, a bundled real Postgres binary
  requiring no root/Docker), which is what actually matters for
  correctness -- Postgres serializes at the connection/transaction level,
  not the process level, so multiple real processes on one machine and
  multiple real machines exercise identical code paths.
- The 1k/10k/50k synthetic DB-only benchmark (`scripts/
  phase6_scale_benchmark.py`) proves the leasing/query layer holds up at
  those row counts -- it says nothing about real network-polling capacity
  or true multi-machine throughput.
- Real registry acquisition validation in this build grew the registry by
  exactly 1 new VERIFIED portal (Duolingo, via the domain-seed pipeline)
  from 3 attempted real companies -- see `docs/registry-acquisition.md` for
  the exact counts. This is a small, honest number, not "the pipeline can
  onboard thousands of companies now" -- it proves the pipeline *works*,
  not that it has been run at scale.
- Registry acquisition still depends on legitimate source data the operator
  supplies; nothing in this phase scrapes search engines, LinkedIn, or
  Indeed, and nothing fabricates a company.
- Sponsorship historical evidence storage (`app/sponsorship/evidence.py`)
  is a Phase 7 foundation only -- no sponsorship intelligence engine exists
  yet, and current-job sponsorship status is never touched by it.
- Application submission remains ASSIST-only; nothing in Phase 6 changes
  that.
