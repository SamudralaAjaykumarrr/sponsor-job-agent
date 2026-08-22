# Fleet Operations

Operator-facing guide: running workers locally (1, 2, or 4+), the CLI, the
dashboard, and the dead-letter workflow.

## Running workers

### Single worker, foreground

```bash
python -m app.workers.cli run
```

Runs continuously until `Ctrl-C` (SIGINT) or SIGTERM, claiming poll +
verification work every cycle, sleeping briefly between cycles when nothing
is due. Add `--once` to run exactly one bounded cycle and exit (useful for
cron/scripting/testing).

### Multiple workers, one machine, no Docker

Two ways, both real, both tested:

**A. Separate terminals / background processes (most explicit):**

```bash
python -m app.workers.cli run --shard-index 0 --shard-count 2 &
python -m app.workers.cli run --shard-index 1 --shard-count 2 &
```

Each worker only claims portals whose `shard_for_portal(id, shard_count) ==
shard_index` — no overlap, proven deterministic in
`tests/test_workers_leasing.py`. `--shard-count 4` with four processes
(`--shard-index 0..3`) works identically for four workers.

**B. The local dev supervisor (one command spawns N):**

```bash
python -c "from app.workers.supervisor import Supervisor; Supervisor(4).run_until_interrupted()"
```

or via a tiny wrapper script if preferred — the class is
`app.workers.supervisor.Supervisor(worker_count, python_executable=None)`.
It spawns `worker_count` child `python -m app.workers.cli run --shard-index
i --shard-count N` processes (bounded by `SUPERVISOR_MAX_WORKERS`, default
8), prefixes each child's stdout/stderr with `[worker-i]`, and forwards
`SIGINT`/`SIGTERM` to all children on its own shutdown. No shell is
involved (`subprocess.Popen` with a fixed argv list) and there is no Docker
dependency.

The dashboard (`uvicorn app.main:app`) is a **separate, independent
process** in both cases — start it alongside the workers, not instead of
them:

```bash
./start.sh                       # dashboard, in its own terminal
python -m app.workers.cli run    # one or more of these, in others
```

## CLI reference

```
python -m app.workers.cli run [--shard-index N] [--shard-count N] [--once]
python -m app.workers.cli status
python -m app.workers.cli attempts [--limit N] [--status S] [--worker ID]
python -m app.workers.cli dead-letter [--requeue ID]
```

- `status` — every worker's identity/status/shard/heartbeat/counters,
  active lease counts (poll + verification), and the same fleet metrics
  snapshot the dashboard shows (see `docs/scaling-claims.md` for what these
  numbers do and don't mean).
- `attempts` — recent poll/verification attempt history, filterable by
  status or worker.
- `dead-letter` — lists open (unresolved) dead-lettered work items;
  `--requeue ID` re-enables the item, resets its failure counters, and
  resolves the dead-letter entry. Always a deliberate operator action —
  nothing is ever auto-requeued.

Every command runs `app.db.init_db()` first — safe to run at any time
against the real database; migrations are additive and idempotent.

## Dashboard

- `/fleet` — workers table (with a **STALE** badge for a worker whose
  heartbeat is older than 4× `WORKER_HEARTBEAT_SECONDS` and hasn't reported
  `STOPPED`), recent attempts, dead letters (with a one-click **Requeue**
  button), fleet metrics, and discovery-latency percentiles.
- `/acquisition` — registry-acquisition batch progress, with a **Resume**
  button on any `FAILED`/`PAUSED` batch.
- `/fleet/metrics` — the same metrics as JSON, for scripting/monitoring.

Both pages, and the JSON endpoint, are read-only except for the two
explicit action buttons (dead-letter requeue, batch resume) — no bulk or
destructive action is one click away, per CLAUDE.md's dashboard-safety rules.

## Dead-letter workflow

1. A work item (either queue) accumulates `DEAD_LETTER_MAX_ATTEMPTS`
   (default 8) **consecutive PERMANENT** failures (never counting transient
   429/5xx/timeout failures toward this).
2. It's disabled (`enabled=0` on its row — never claimed again) and a
   `dead_letters` row is created/updated, capturing the reason, attempt
   count, last error, and the exact attempt id that triggered it.
3. An operator investigates (check `attempts --worker`/`--status
   PERMANENT_FAILURE`, or the portal's detail page under `/registry`),
   fixes the underlying problem if there is one (e.g. a stale tenant
   identifier), and requeues via the CLI or dashboard.
4. Requeuing resets `consecutive_failures`/`consecutive_permanent_failures`
   to 0 and re-enables the row — it goes back into the normal due-scheduling
   flow exactly like any other portal.

The verification queue's own, separate demotion mechanism
(`REGISTRY_STALE_AFTER_PERMANENT_FAILURES`, Phase 4, unchanged) also feeds
into this same dead-letter table when a portal is quarantined for repeated
verification failure — so the CLI/dashboard dead-letter view is one place to
see everything that gave up, from either queue.

## What "healthy" looks like

From this phase's own real, live 2-worker run against the actual (small)
verified registry: `provider_circuits_open_or_half_open: 0`,
`dead_letters_open: 0`, `errors: 0` on every worker row, and
`monitoring_coverage_24h` converging toward 1.0 across successive cycles (as
it should — a single bounded cycle isn't guaranteed to fully drain a
provider with many tenants sharing a tight `PROVIDER_CONCURRENCY_DEFAULT`;
continuous operation, not `--once`, is what real deployment uses).

## Phase 6 additions

`/fleet` now also shows: database backend, schema version (current vs.
expected), queue backend description, worker software version, per-provider
circuit state (with force-probe/close admin actions), schema drift table,
and a "reap orphans now" button. New admin actions (all POST, all explicit,
none destructive): force-probe a circuit, close a circuit, mark a worker
offline, reap orphans. `/metrics` (Prometheus text) and `/readiness` are new
top-level endpoints. See `docs/production-observability.md`.

A real 2-worker run against a real Postgres database (migrated from this
project's actual `data/app.db` via `app.db_migrate`, then discarded) during
this phase's own live validation surfaced and fixed two real Postgres-
specific bugs (a `MAX()`-is-aggregate-only SQL portability bug, and a
concurrent-schema-DDL deadlock/race on worker startup) -- see
`docs/phase6-production-scale.md`'s "real bugs" section for both.
