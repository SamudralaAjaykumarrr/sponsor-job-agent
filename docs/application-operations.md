# Application Executor Operations

## Enabling the executor

Both flags default `false`. In `.env`:

```
APPLICATION_EXECUTOR_ENABLED=true   # allow queuing/preparing applications
AUTO_SUBMIT_ENABLED=true            # allow AUTO_PERMITTED mode to actually submit
```

Startup always prints the live state of both:

```
Application executor: ON
Auto submit:          ON
```

## CLI

```
python -m app.applications.cli validate JOB_ID
python -m app.applications.cli queue JOB_ID [--mode ASSIST|AUTO_PERMITTED]
python -m app.applications.cli prepare JOB_ID [--mode ASSIST|AUTO_PERMITTED]   # queue + run once, synchronously
python -m app.applications.cli status
python -m app.applications.cli reconcile EXECUTION_ID --resolution {confirmed_applied,confirmed_not_submitted,manual_applied} [--confirmation-id ID] [--note NOTE]
python -m app.applications.cli doctor
```

`prepare` is the simplest way to run one job through the executor end to
end without standing up a worker loop — useful for manual/local operation.

## Dashboard

- `GET /applications` — bucketed view (Ready/Queued/Preparing/Needs
  Action/Submitting/Applied/Failed) with company/provider/work-arrangement/
  sponsorship filters, live metrics, and the executor on/off banner.
- `GET /applications/doctor` — the integrity report.
- Job detail page (`/jobs/{id}`): an "Application execution" card showing
  the current eligibility result and, once queued, the active execution's
  status, plus action buttons (Prepare Application, Queue Application,
  Retry Preparation, Reconcile Submission).
- JSON: `GET /api/applications/metrics`, `GET /api/jobs/{id}/eligibility`,
  `GET /api/executions/{id}` (execution + answer snapshot + audit log).

## Reconciling a SUBMISSION_STATUS_UNKNOWN execution

1. Manually check whether the application actually went through (open the
   ATS/email/confirmation page yourself).
2. If it did: `reconcile EXECUTION_ID --resolution confirmed_applied
   --confirmation-id "..."` — marks `APPLIED`.
3. If it didn't: `reconcile EXECUTION_ID --resolution confirmed_not_submitted`
   — marks `WITHDRAWN`, freeing the job to be queued again cleanly.

This is always an explicit human/operator action — nothing in this codebase
auto-reconciles or auto-retries a submission.

## Worker capabilities (distributed operation)

`app/applications/worker_capabilities.py`'s `WorkerCapability` enum
(`DISCOVERY`, `REGISTRY_VERIFY`, `APPLICATION_PREPARE`, `APPLICATION_SUBMIT`)
is the declared-capability model for a future distributed executor worker
fleet, following the same JSON-list-on-the-`workers`-row pattern as Phase
5-6's discovery workers (`app.workers.repo.upsert_worker(..., capabilities=...)`).
A worker that never passes `capabilities` is capability-less for the
executor queues by default — opt-in, not opt-out.

**Current implementation status**: the atomic claim primitives
(`app.applications.queue.claim_execution_batch`/`release_execution_lease`/
`extend_execution_lease`) are implemented and tested (including against
real concurrent threads and real PostgreSQL — see
`tests/test_applications_concurrency.py`, `tests/test_applications_postgres.py`),
but a standalone always-running executor worker daemon
(`app/workers/runner.py`'s equivalent for the application queue) was not
built in this phase — `queue_application()` + `process_execution()` are run
synchronously today (CLI/dashboard-triggered), which is sufficient for the
`ASSIST`-first product this phase targets. Building the daemon is
straightforward follow-on work using the same claim/lease/ack pattern as
`app.workers.runner`, and is the top item in the recommended Phase 9 list.

## Doctor checks

`python -m app.applications.cli doctor` (also `/applications/doctor`) is
read-only and exits nonzero on any serious issue: `applied_without_
confirmation`, `execution_missing_job`, `duplicate_active_execution`,
`wrong_resume_job_mapping`, `missing_answer_snapshot`,
`unsupported_provider_auto_submit`, `non_full_time_in_submission`,
`unknown_sponsorship_submitted`, `no_sponsorship_submitted`,
`likely_sponsorship_auto_submitted`, `submitted_without_permitted_policy`.

## Rate limits

Defaults: 5/hour, 20/day, 2/company/day. Change via `MAX_APPLICATIONS_PER_HOUR`
/ `_PER_DAY` / `_PER_COMPANY_PER_DAY`. Enforced by a live query against
`application_audit_log` — already correct across multiple worker processes
sharing one database (SQLite file or shared Postgres).
