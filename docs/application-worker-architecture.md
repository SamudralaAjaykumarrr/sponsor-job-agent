# Application Worker Architecture

`app/applications/worker.py::ApplicationWorker` is the Phase 9 standalone daemon that
continuously drives the Phase 8 executor pipeline. It reuses
`app.applications.executor.process_execution()` completely unchanged — this module only adds
leasing (already built in Phase 8), submission circuit-breaker bookkeeping, per-attempt
history, worker identity/heartbeat/capability declaration, drain-mode, and graceful shutdown
around it, mirroring `app.workers.runner.Worker`'s shape for the discovery fleet.

## Running it

```
python -m app.applications.worker run                 # one worker, continuous
python -m app.applications.worker run --once           # one bounded cycle then exit
python -m app.applications.worker run --workers 4       # local multi-process supervisor
python -m app.applications.worker run --drain           # start already in DRAINING status
python -m app.applications.cli worker ...                # same, via the operations CLI
python -m app.applications.cli drain WORKER_ID [--resume]
```

`ApplicationWorker.run()` refuses to start if `APPLICATION_EXECUTOR_ENABLED` is false —
an application worker must never even attempt to claim work while the executor kill switch
is off.

## Worker separation (CLAUDE.md Phase 9 section 3)

`ApplicationWorker` declares **only** `APPLICATION_PREPARE`/`APPLICATION_SUBMIT` via
`app.workers.repo.upsert_worker(..., capabilities=...)`. It never imports or touches
`app.workers.queue`/`app.workers.leasing` (the discovery poll/verification queues) — a
discovery-only worker (which never sets `capabilities`) can never claim application work
because `app.applications.queue.claim_execution_batch` is an entirely separate claim path
over a different table (`application_executions`, not `company_registry`/`registry_portals`).

## Claim loop

One cycle (`_run_cycle()`):

1. Check the worker's own `workers` row for `status == DRAINING` (see "Drain mode" below) —
   if draining, heartbeat and return without claiming anything.
2. `app.applications.queue.claim_execution_batch()` — same atomic
   `UPDATE ... WHERE (unleased OR expired)` pattern as Phase 5's discovery leasing; correctness
   comes from the database's own single-writer serialization (SQLite) / row-level locking
   (Postgres), never application-level locking.
3. For each claimed execution: check the **submission** circuit breaker
   (`app.applications.circuit`) for that job's actual application-provider adapter name (not
   the raw `job.provider` string — resolved via `get_application_provider(job).name`, since a
   generic-fallback job still shares the `generic` provider's breaker). If open or the
   provider is at its (deliberately tiny, default `1`) concurrency limit, the item gets a
   short **cooldown lease extension**, never a bare release — releasing outright would busy-spin
   claim/cancel/reclaim across concurrent workers sharing one provider's tight budget
   (the same bug class Phase 5 caught and fixed for discovery; see `APPLICATION_SKIP_COOLDOWN_
   SECONDS`).
4. Otherwise, call `process_execution(execution_id, allow_submission=not draining)` — the
   entire prepare→map→fill→validate→(submit)→confirm pipeline runs, completely unchanged from
   Phase 8, inside one synchronous call.
5. Record one `application_attempts` row (`app.applications.attempts`) with the outcome, feed
   the circuit breaker **only if a genuine submission attempt actually occurred** this call
   (`SUBMITTED`/`APPLIED`/`SUBMISSION_CONFIRMED`/`SUBMISSION_STATUS_UNKNOWN`/
   `PERMANENT_SUBMISSION_FAILURE`/`RETRYABLE_SUBMISSION_FAILURE` — never for
   `NEEDS_USER_ACTION`/`SUBMISSION_READY`/`VALIDATION_REQUIRED`, which mean `submit()` was
   never reached), and release the lease.

## Distributed leasing (SQLite + PostgreSQL)

Both backends already existed from Phase 8 (`app.applications.queue`). Phase 9 fixed one real
gap: `_ACTIVE_CLAIMABLE_STATUSES` originally only included `QUEUED` — but
`executor.process_execution()`'s very first write moves status to `STARTED` almost
immediately, so a crash any time after that point (before reaching a terminal or
human-actionable status) would have left the row **permanently unclaimable**, even once its
lease expired, since it would never again match a `status IN ('QUEUED')` filter. Fixed: the
claimable set now includes every "the worker was actively mid-pipeline" status (`STARTED`,
`FORM_DISCOVERED`, `FORM_MAPPED`, `FORM_FILLED`, `SUBMITTING`, `SUBMITTED`) — lease expiry
alone is now sufficient to recover any of them, matching Phase 5's discovery-queue guarantee.
Including `SUBMITTING`/`SUBMITTED` is made safe by the crash-recovery fix below.

The network call (the actual provider HTTP request inside `provider.submit()`/
`discover_form()`) always happens **outside** any `db_session()` transaction, same as every
prior phase's rule.

## Crash recovery: the SUBMITTING/SUBMITTED resume guard

The single most important correctness property added this phase.
`executor.process_execution()` now begins with:

```python
if execution["status"] in (ExecutionStatus.SUBMITTING.value, ExecutionStatus.SUBMITTED.value):
    # never call submit() again -- convert straight to SUBMISSION_STATUS_UNKNOWN
```

This means: no matter *when* a worker crashes relative to the `provider.submit()` call — before
it, during it, or after it returned but before the confirmation step ran — resuming that
execution (whether via a re-claim after lease expiry, or a manual "Retry Preparation" click)
can never result in a second real submission attempt. The request may or may not have reached
the provider; the code treats that as unknowable and stops, exactly like a genuine timeout
does. See `tests/test_application_worker_crash_recovery.py`.

## Pre-submission revalidation (CLAUDE.md sections 24-27)

Immediately before the `SUBMITTING` transition, `process_execution()` re-fetches the job fresh
from the database and re-derives:

1. **Job still exists** — `get_job()` returning `None` → `ExecutionStatus.JOB_NO_LONGER_ACTIVE`.
2. **Job still active**, if the provider can genuinely tell —
   `ApplicationProvider.check_job_still_active(job)` (optional hook, default `None`/"not
   checkable"; `MockATSProvider` implements it for the `job_removed` scenario).
3. **Fresh eligibility** — `evaluate_executor_eligibility(fresh_job)` re-run from scratch. A
   hard-skip result (e.g. a discovery-cycle JD re-analysis flipped sponsorship to
   `NO_SPONSORSHIP`, or employment type to `CONTRACT`) becomes `JOB_NO_LONGER_ACTIVE` — a hard
   stop, never a submission. A softer eligibility drop (e.g. `CONFIRMED_SPONSOR` →
   `LIKELY_SPONSOR`) simply falls back to the normal ASSIST/`SUBMISSION_READY` path via the
   *fresh* eligibility result, rather than the stale one computed minutes earlier.

Rate-limit and duplicate checks (already present in Phase 8) also run against this same fresh
job object.

## Drain mode (CLAUDE.md section 13)

An operator flips a worker into `DRAINING` via `app.applications.worker_admin.request_drain()`
(`POST /application-workers/{worker_id}/drain`, or `python -m app.applications.cli drain
WORKER_ID`) — a plain `UPDATE workers SET status='DRAINING'`, never touching a lease directly.
The worker itself polls its own row at the top of every cycle:

- **Stops claiming new executions** entirely.
- **Keeps heartbeating** (status stays `DRAINING`) so it remains visible as alive to operators.
- Any execution already claimed *before* the drain request finishes its current
  `process_execution()` call, but with `allow_submission=False` — the full prepare/map/fill/
  validate pipeline still runs (finishing "safe in-progress preparation"), but the function
  stops at `SUBMISSION_READY` instead of ever calling `submit()` ("do not start new
  submission").

Resuming: `resume_from_drain()` / `POST /application-workers/{worker_id}/resume-drain`.
Actually stopping the process still uses SIGTERM (`request_stop()`), exactly like the
discovery worker.

## Orphan recovery

Reuses `app.workers.reaper.reap_orphans()` unchanged — it already operates generically on the
shared `workers` table regardless of declared capability, marking a stale-heartbeat worker
`OFFLINE` for dashboard visibility. Lease recovery itself remains driven exclusively by
`lease_expires_at` passing, independent of whether the reaper ever runs, matching the existing
Phase 6 rule.

## Local supervisor

`app/applications/supervisor.py::ApplicationSupervisor` mirrors
`app.workers.supervisor.Supervisor` exactly (no shell, bounded worker count via
`APPLICATION_SUPERVISOR_MAX_WORKERS`, SIGINT/SIGTERM forwarding, prefixed log streaming) but
spawns `python -m app.applications.worker run` with no shard flags — the application queue is
a single shared claim pool (intentionally low volume; see CLAUDE.md section 36), so sharding
it would add complexity with no benefit.
