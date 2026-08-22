# Worker Architecture

The Phase 5 worker fleet turns the Phase 2/3 discovery pipeline and Phase 4
verification/lifecycle pipeline into something that can run continuously,
across multiple local processes (and, with a shared database, multiple
machines) without double-work, without hammering providers, and without
losing progress on a crash. All of it is additive on top of the unchanged
Phase 2-4 pipeline — see `app/agent/cycle.py::process_raw_job` (public
alias) and `app/registry/verification.py::verify_portal`, both reused
as-is by the worker.

## Module map

| Module | Responsibility |
|---|---|
| `app/workers/identity.py` | Stable worker id (`hostname-pid-random8`) — no candidate PII |
| `app/workers/models.py` | `WorkerStatus`, `AttemptStatus`, `CircuitState`, `LeasedWorkItem`, `AttemptRecord` |
| `app/workers/repo.py` | CRUD for `workers`, `poll_attempts`, `dead_letters`, `provider_circuit_state` |
| `app/workers/leasing.py` | Atomic claim/release/extend — see `docs/polling-leases.md` |
| `app/workers/queue.py` | `WorkQueue` abstraction (`claim_due_work`/`ack`/`retry`/`fail`/`extend_lease`) over leasing — see "Queue abstraction" below |
| `app/workers/retry.py` | Centralized retryable-vs-permanent classification + bounded exponential backoff |
| `app/workers/circuit.py` | Per-provider circuit breaker + cross-process inflight-concurrency slots |
| `app/workers/schema_check.py` | Schema-drift detection (distinct from an empty board) |
| `app/workers/dead_letter.py` | Dead-letter recording/requeue |
| `app/workers/metrics.py` | Honest fleet/monitoring metrics — see `docs/scaling-claims.md` |
| `app/workers/runner.py` | `Worker` — the actual claim→execute→record→release loop |
| `app/workers/supervisor.py` | Local dev process supervisor (spawns N `Worker` subprocesses) |
| `app/workers/cli.py` | `python -m app.workers.cli run\|status\|attempts\|dead-letter` |

## The `Worker` execution loop

```
Worker.run():
    register in `workers` table (STARTING)
    loop:
        _run_cycle()                      # one bounded cycle
        if stop requested or single_cycle: break
        heartbeat(IDLE); sleep(idle_sleep_seconds, interruptible)
    heartbeat(STOPPED)
```

One `_run_cycle()`:

```
start a discovery_cycles row (reuses the SAME table/observability the
    Phase 2 scheduler uses -- "Recent cycles" on the dashboard shows both)
while not stopped and time budget/portal cap not exceeded:
    poll_items    = SQLitePollQueue.claim_due_work(...)          # company_registry
    verify_items  = SQLiteVerificationQueue.claim_due_work(...)  # registry_portals
    if nothing claimed: break
    submit each item to a bounded ThreadPoolExecutor
        (POLL_WORKER_CONCURRENCY workers)
    for each completed item: heartbeat periodically
finalize the discovery_cycles row
```

Claiming happens **incrementally**, `DUE_WORK_BATCH_SIZE` at a time, never
"load every due portal" — the same backpressure philosophy Phase 4 already
established for verification (`REGISTRY_DUE_BATCH_SIZE`). `MAX_PORTALS_PER_
WORKER_CYCLE` and `POLL_CYCLE_TIME_BUDGET_SECONDS` bound one cycle
regardless of how much work is actually due.

## One poll attempt, step by step

For a `company_registry` item (`app/workers/runner.py::_execute_poll`):

1. **Circuit check** (`circuit.may_attempt`) — if the provider's breaker is
   `OPEN`, the claim is given a short cooldown (not released outright, see
   `docs/polling-leases.md`) and skipped this cycle.
2. **Concurrency slot** (`circuit.acquire_inflight_slot`) — if the provider
   is already at `PROVIDER_CONCURRENCY_DEFAULT` in-flight requests
   (tracked in a DB row shared by every worker process), same cooldown-skip.
3. **Raw structural probe** (`app.registry.probe`, reused from Phase 4) —
   this is what actually determines success/failure, because (unlike every
   `JobProvider.fetch_jobs()`, which deliberately swallows HTTP errors
   internally so one bad board never aborts a whole legacy discovery cycle)
   it *raises*. Every provider that can ever reach `ACTIVE` has a probe (see
   `app.registry.verification`'s own requirement), so this has full coverage
   of everything the fleet actually polls.
4. **Schema-shape check** (`app.workers.schema_check`) — for providers with
   a probe, the raw response is checked against its expected top-level
   shape *before* treating an empty result as a healthy empty board. A
   missing/mistyped field is recorded as `error_type=schema_drift`
   (healthy status, zero jobs, but flagged distinctly) rather than silently
   treated as "zero jobs currently."
5. **Real fetch** — only once the probe+shape check succeed,
   `provider.fetch_jobs()` (the existing, unmodified Phase 2/3 connector)
   is called for the actual normalized job list.
6. **Pipeline reuse** — each `RawJobPosting` goes through
   `app.agent.cycle.process_raw_job` unchanged: dedup (provider id → canonical
   URL → fingerprint fallback), store, `analyze_job`, gates, scoring,
   sponsorship classification, `generate_assist_outputs`. Nothing about the
   analysis pipeline changed for Phase 5.
7. **Scheduling** — `app.registry.repo.mark_poll_result` (Phase 3, unchanged)
   updates `next_poll_at`/`consecutive_failures`/health via the existing
   deterministic adaptive-polling rules.
8. **Attempt history + lease release** — one `poll_attempts` row is always
   recorded (bounded to the last 100 per portal), and the lease is always
   released — including via an outer safety-net `except Exception`, added
   after this phase's own live validation caught a real gap: a provider
   connector's internal error isolation didn't cover every exception type
   (`ResponseTooLargeError` escaped `GreenhouseProvider.fetch_jobs()`
   uncaught for an unusually large real board), which stranded a lease with
   zero attempt record until it expired. See
   `test_unexpected_fetch_jobs_exception_still_records_attempt_and_releases_lease`.

Verification-queue items (`_execute_verification`) follow the same
circuit/concurrency gating, then call `app.registry.verification.verify_portal`
+ `apply_verification_outcome` + `maybe_detect_migration` +
`sync_portal_to_operational_registry` — all unchanged Phase 4 code — wrapped
in the same attempt-recording/lease-release/dead-letter bookkeeping.

## Queue abstraction (future backend swap)

`app/workers/queue.py::WorkQueue` is an ABC with exactly four methods:
`claim_due_work`, `ack`, `retry`, `fail`, `extend_lease`. `SQLitePollQueue`
and `SQLiteVerificationQueue` are the only implementations today, both thin
wrappers over `app/workers/leasing.py`. `Worker` (the runner) only ever
calls through this interface — it has no SQLite-specific code of its own.

A future PostgreSQL/Redis/SQS-backed queue would implement the same four
methods:

- **PostgreSQL**: `SELECT ... FOR UPDATE SKIP LOCKED` replaces the
  `UPDATE ... WHERE unleased-or-expired` pattern; the four-method contract
  is unchanged, and works across real multiple machines since Postgres is a
  real client-server database.
- **Redis**: `claim_due_work` becomes a Lua script doing an atomic
  `ZRANGEBYSCORE` (due items) + `SETNX`-style claim with a TTL; `ack`/`fail`
  delete the claim key; `extend_lease` is `PEXPIRE`.
- **SQS**: `claim_due_work` is `ReceiveMessage` (SQS's own visibility
  timeout *is* the lease); `ack` is `DeleteMessage`; `fail`/`retry` are a
  no-op (the message becomes visible again once the timeout elapses) or an
  explicit `ChangeMessageVisibility` to shorten/lengthen it; `extend_lease`
  is `ChangeMessageVisibility`.

None of `app/agent/cycle.py`, `app/registry/verification.py`, or
`app/providers/*` would need to change for any of these — they only ever
see a `LeasedWorkItem`, never a queue implementation detail.

## Retry policy

`app/workers/retry.py::classify_exception` — mirrors the philosophy already
established in `app.registry.verification`: permanent HTTP codes (400/401/
403/404/410) are non-retryable; 429/5xx/timeouts/connection errors are
retryable; anything unrecognized is *conservatively* treated as retryable
(never permanently discard a portal on an ambiguous error). `Retry-After` is
already honored at the HTTP layer
(`app.providers.http_client.request_with_retries`'s own bounded retry loop,
unchanged from Phase 3) before this module's classification even runs.
`backoff_seconds(attempt_number)` is a capped exponential (30s, 60s, 120s,
...  capped at 1h by default) used for the verification-queue cooldown; the
poll-queue's actual cross-cycle schedule is `app.registry.scheduling`'s
existing deterministic backoff (unchanged), reflected into
`poll_attempts.next_retry_at` for visibility rather than recomputed.

## Circuit breaker

`app/workers/circuit.py` — one row per provider in `provider_circuit_state`,
shared by every worker process:

- **CLOSED** → normal operation.
- Trips **OPEN** when either 5 consecutive failures occur, or a rolling
  20-attempt window's failure rate reaches `CIRCUIT_BREAKER_FAILURE_
  THRESHOLD` (default 50%) with at least 5 samples observed.
- After `CIRCUIT_BREAKER_COOLDOWN_SECONDS`, the next `may_attempt()` call
  atomically transitions **OPEN → HALF_OPEN** and claims the single probe
  slot (`half_open_inflight`) in the same statement, so two workers racing
  here can't both get a probe through.
- Probe succeeds → **CLOSED** (full reset). Probe fails → **OPEN** again
  with a fresh cooldown. **A provider is never permanently disabled** — see
  `test_circuit_never_permanently_disables_a_provider`.
- A provider with no raw probe (only reachable via a manually-added
  `company_registry` entry that bypassed Phase 4 verification) has no real
  success/failure signal available (`fetch_jobs()` swallows its own HTTP
  errors) — the breaker deliberately does *not* record a fabricated
  "success" for it, and any HALF_OPEN probe slot it might have claimed via
  `may_attempt()` is explicitly released so it can never wedge the breaker.

Provider concurrency (`acquire_inflight_slot`/`release_inflight_slot`) is a
separate, simpler mechanism: an atomic `UPDATE ... WHERE inflight < limit`
counter per provider, also shared across every worker process. One
provider's exhausted budget never blocks a different provider — proven in
`test_provider_isolation_one_failing_provider_does_not_block_another` and,
live, in this phase's real 2-worker/19-real-portal validation run (ashby and
workday kept succeeding while greenhouse's tighter, shared budget throttled
its own larger tenant set).

## Graceful shutdown

`Worker.request_stop()` sets a `threading.Event`; `install_signal_handlers()`
wires `SIGINT`/`SIGTERM` to it (main-thread-only, per Python's signal
restrictions — the CLI entrypoint calls this, tests that drive `Worker`
directly must not). The loop checks the event between claim rounds and
between cycles; whatever batch is already in flight (bounded — at most
`DUE_WORK_BATCH_SIZE * POLL_WORKER_CONCURRENCY`-ish items) is allowed to
finish, which in practice is well within `WORKER_SHUTDOWN_GRACE_SECONDS`.
Any lease still held past that point (unusual, but possible for a hung
network request) is simply left for its normal expiry — no special
forced-release path exists, deliberately: it reuses the exact same
crash-recovery guarantee described in `docs/polling-leases.md` rather than
adding a second, harder-to-verify code path for "clean" shutdown.

## Local process supervision

`app/workers/supervisor.py::Supervisor` spawns `N` (bounded by
`SUPERVISOR_MAX_WORKERS`) `python -m app.workers.cli run --shard-index i
--shard-count N` child processes via `subprocess.Popen` with a fixed argv
list — **no shell involved anywhere**, so there is no shell-injection
surface regardless of input. `stop()` sends `SIGTERM` to every child and
waits (bounded by `WORKER_SHUTDOWN_GRACE_SECONDS`) before force-killing any
stragglers. See `docs/fleet-operations.md` for the exact commands.

## Phase 6: multi-machine workers

Everything above still describes local multi-process operation exactly.
What changed for genuine multi-machine operation:

- `WorkerIdentity` gained `worker_version`/`schema_version`/
  `capability_version`/`backend` (still no candidate PII). See
  `docs/distributed-workers.md`.
- `Worker.run()` now checks schema compatibility before registering itself
  and calls the orphan reaper once per cycle.
- On the Postgres backend, `app/workers/leasing_postgres.py` (`SELECT ...
  FOR UPDATE SKIP LOCKED`) replaces the SQLite-style WHERE-guarded UPDATE
  loop for the actual claim -- `app/workers/leasing.py`'s public functions
  are unchanged and dispatch automatically; `release_*`/`extend_*` stay
  backend-neutral (the WHERE-guarded UPDATE pattern is correct, just less
  optimal under contention, on both backends).
- The shared circuit breaker + inflight-concurrency counter
  (`provider_circuit_state`) were already DB-backed in Phase 5 -- pointing
  `DATABASE_URL` at a shared Postgres instance is what makes them genuinely
  fleet-wide across machines, with no code change required.

See `docs/distributed-workers.md` for the full detail and
`docs/postgres-backend.md` for the database layer this all sits on.
