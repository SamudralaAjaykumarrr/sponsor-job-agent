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
is the declared-capability model. Phase 9's `app.applications.worker.
ApplicationWorker` is the standalone daemon that actually declares
`APPLICATION_PREPARE`/`APPLICATION_SUBMIT` and drives the queue continuously
— see `docs/application-worker-architecture.md` for the full design
(leasing, crash recovery, submission circuit breaker, drain mode,
supervisor). `queue_application()`/`process_execution()` remain available
for direct synchronous CLI/dashboard use exactly as in Phase 8; the worker
daemon is simply another caller of the same functions.

```
python -m app.applications.worker run [--once] [--workers N] [--drain]
python -m app.applications.cli worker [--once] [--workers N] [--drain]
python -m app.applications.cli drain WORKER_ID [--resume]
python -m app.applications.cli scheduler [--limit N]
python -m app.applications.cli reconcile-worker [--limit N]
python -m app.applications.cli budget
python -m app.applications.cli capability-matrix
```

## Dashboard (Phase 9 additions)

- `GET /application-workers` — worker fleet (id/host/status/capabilities/
  heartbeat/counters, drain/resume/mark-offline actions), submission
  provider circuits (force-probe/close), and recent attempt history.
- `GET /applications/capability-matrix` — the truthful provider matrix.
- `POST /applications/scheduler/run`, `POST /applications/reconcile-worker/run`
  — manual triggers (JSON result).
- `GET /api/applications/budget` — daily budget accounting.
- `/applications` now also shows fleet/budget/circuit summaries inline.

## Continuous scheduler (auto-prepare)

`app.applications.scheduler.run_cycle()` (`APPLICATION_AUTO_PREPARE_ENABLED`,
independent of `AUTO_SUBMIT_ENABLED` — CLAUDE.md Phase 9 section 37) finds
eligible `READY_TO_APPLY` jobs (ordered by the existing Phase 1-2
`priority_score`, which already encodes fresh/strong-match/CONFIRMED/
FULL_TIME/Remote>Hybrid>Onsite), skips anything with an active execution or
over its rate limit, and calls `queue_application()` — in `AUTO_PERMITTED`
mode only when `AUTO_SUBMIT_ENABLED` is *also* true and that specific job
already clears `auto_submit_eligible`, otherwise `ASSIST`. Never bypasses
any eligibility/duplicate/rate-limit gate — it is purely a caller of
`queue_application()`, with no direct access to form/submit machinery.

## Doctor checks

`python -m app.applications.cli doctor` (also `/applications/doctor`) is
read-only and exits nonzero on any serious issue. Phase 8 checks:
`applied_without_confirmation`, `execution_missing_job`,
`duplicate_active_execution`, `wrong_resume_job_mapping`,
`missing_answer_snapshot`, `unsupported_provider_auto_submit`,
`non_full_time_in_submission`, `unknown_sponsorship_submitted`,
`no_sponsorship_submitted`, `likely_sponsorship_auto_submitted`,
`submitted_without_permitted_policy`. Phase 9 additions:
`expired_execution_lease`, `orphan_execution_lease`,
`multiple_active_leases_same_job`, `duplicate_confirmation`,
`submission_capable_provider_without_policy`,
`auto_submit_enabled_for_unvalidated_provider`,
`unknown_submission_retried`, `non_full_time_queued`,
`non_confirmed_sponsorship_queued`, `rate_limit_accounting_inconsistency`.
Phase 10 additions (browser-assist sessions, see `docs/browser-assist-sessions.md`):
`browser_session_without_execution`, `browser_session_non_full_time`,
`browser_session_non_eligible_sponsorship`, `stale_browser_session_still_active`,
`browser_confirmation_without_applied_execution`, `browser_applied_without_confirmation`,
`unexpected_browser_auto_submit_capability`, `browser_session_forbidden_field`,
`browser_capability_matrix_claims_final_submit`.

## Scheduled background maintenance (Phase 10)

`app.applications.background_scheduler.background_scheduler` runs inside the FastAPI app's
lifespan (started/stopped alongside the existing discovery `scheduler`) and actually executes
two tasks that Phase 9 only ever defined config flags for:

- The reconciliation evidence pass (`app.applications.reconcile_worker.run_pass()`) every
  `RECONCILE_WORKER_INTERVAL_SECONDS`, when `RECONCILE_WORKER_ENABLED=true`.
- The browser-assist stale-session reaper (`app.applications.browser_assist.expire_stale_sessions()`)
  at roughly half of `BROWSER_SESSION_TIMEOUT_MINUTES`, when `BROWSER_ASSIST_ENABLED=true`.

Both are independently gated and a failure in one is logged and never stops the other or the
loop itself (`tests/test_application_background_scheduler.py`).

## Rate limits

Defaults: 5/hour, 20/day, 2/company/day. Change via `MAX_APPLICATIONS_PER_HOUR`
/ `_PER_DAY` / `_PER_COMPANY_PER_DAY`. Enforced by a live query against
`application_audit_log` — already correct across multiple worker processes
sharing one database (SQLite file or shared Postgres).

## Phase 11 operational additions

- `python -m app.applications.cli workday-tenants` — per-tenant/site Workday observation matrix.
- `python -m app.applications.cli capability-evidence [--provider NAME]` — dated evidence,
  flagging `[STALE]` rows older than `CAPABILITY_EVIDENCE_MAX_AGE_DAYS` (default 30).
- `/applications/workday-tenants` and `/applications/capability-evidence` dashboard pages.
- New config: `BROWSER_SESSION_RECONSTRUCT_ENABLED` (default `true`),
  `CAPABILITY_EVIDENCE_MAX_AGE_DAYS` (default `30`), `REAL_ATS_VALIDATION_ENABLED` (default
  `false`, gates `scripts/phase11_live_validation.py` only — never affects pytest).
- `python -m app.applications.cli doctor` gained 8 new checks — see
  `docs/browser-session-reconstruction.md` and `docs/ats-capability-evidence.md`.
