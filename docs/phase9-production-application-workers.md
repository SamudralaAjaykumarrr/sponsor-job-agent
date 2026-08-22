# Phase 9 — Production Application Workers

## Objective

Turn the Phase 8 synchronous executor (`queue_application()` + `process_execution()`, run
directly from the CLI/dashboard) into a real, distributed, production-operable application
execution *service*: a standalone worker daemon, a submission-specific circuit breaker, a
continuous scheduler, an automated (but never fabricating) reconciliation-evidence pass, a
much larger mock-ATS sandbox, an audited provider capability matrix, an optional visible-
browser preparation aid, and the operational surface (dashboard/doctor/metrics/docs) to run
all of it safely.

Nothing about *what* is safe to submit changed — every Phase 8 hard gate (FULL_TIME-only,
sponsorship CONFIRMED-only for auto-submit, no CAPTCHA/MFA bypass, duplicate protection,
rate limits, "never blindly retry an ambiguous outcome") is preserved exactly. Phase 9 is
entirely about *how continuously and how many machines* that same safe pipeline can run on.

## What Phase 8 had already built (reused, not rebuilt)

- `application_executions` already carried lease columns
  (`lease_owner`/`lease_attempt_id`/`lease_acquired_at`/`lease_expires_at`) and
  `app.applications.queue.claim_execution_batch`/`release_execution_lease`/
  `extend_execution_lease` already implemented the same atomic
  `UPDATE ... WHERE (unleased OR expired)` claim pattern as `app.workers.leasing`, tested
  under real concurrent threads and real PostgreSQL. Phase 9's worker daemon is the first
  thing that actually *drives* this queue continuously.
- `app.applications.worker_capabilities.WorkerCapability` (`DISCOVERY`, `REGISTRY_VERIFY`,
  `APPLICATION_PREPARE`, `APPLICATION_SUBMIT`) already existed as the declared-capability
  model. Phase 9's `ApplicationWorker` is the first process that actually declares it.

## Two real bugs this phase's own testing caught and fixed

1. **Resumed-mid-submission double-submit risk.** `executor.process_execution()` wrote
   `SUBMITTING` *before* calling `provider.submit()`, but had no guard for being invoked again
   against a row already sitting in `SUBMITTING`/`SUBMITTED` (e.g. a worker crashed between
   those two points). A second invocation would have called `submit()` a second time — a real
   double-submission risk. Fixed: `process_execution()` now detects this exact resume case and
   converts straight to `SUBMISSION_STATUS_UNKNOWN` without ever calling `submit()` again. See
   `docs/application-worker-architecture.md`'s "Crash recovery" section and acceptance
   scenarios D/E below.
2. **Postgres `DatatypeMismatch` on `jobs.sponsorship_conflict`.** `app.jobs_repo.insert_job`/
   `update_job` passed a Python `bool` straight through to psycopg, which maps it to
   Postgres's native `boolean` type — conflicting with the schema's `INTEGER` column (SQLite
   silently accepted the same code, which is exactly why this had never been caught before this
   phase's first Postgres test that runs a job through the *full* pipeline). Fixed with a
   `_coerce_sql_value()` helper that explicitly casts `bool -> int`, matching CLAUDE.md's own
   "boolean flags stay INTEGER in both backends" rule. Caught by
   `tests/test_applications_postgres_phase9.py`.
   Also fixed in the same pass: `app/db_postgres.py::_TABLES_WITHOUT_ID_PK` was missing the new
   `application_provider_circuit_state` table (its PK is `provider`, not `id`), which broke the
   `RETURNING id` lastrowid emulation on Postgres.
3. **`MockATSProvider.validate()` crashed on `form=None`.** Adding the `form_not_found` mock
   scenario exposed that `validate()` unconditionally read `form.captcha_present` — every other
   provider already guarded this; the mock now does too.

## New modules

- `app/applications/worker.py` — `ApplicationWorker`, the standalone daemon
  (`python -m app.applications.worker run`). See `docs/application-worker-architecture.md`.
- `app/applications/supervisor.py` — local multi-process supervisor
  (`python -m app.applications.worker run --workers N`).
- `app/applications/circuit.py` — submission-specific circuit breaker (separate table/module
  from `app.workers.circuit`'s discovery breaker).
- `app/applications/attempts.py` — per-attempt history (`application_attempts` table).
- `app/applications/scheduler.py` — continuous auto-prepare scheduler
  (`APPLICATION_AUTO_PREPARE_ENABLED`).
- `app/applications/budget.py` — deterministic daily budget accounting.
- `app/applications/reconcile_worker.py` — automated reconciliation *evidence* pass. See
  `docs/application-reconciliation.md`.
- `app/applications/capability_matrix.py` — truthful provider capability matrix. See
  `docs/application-provider-capabilities.md`.
- `app/applications/browser_assist.py` — optional Playwright-based visible-browser
  preparation aid. See `docs/application-browser-assist.md`.
- `app/applications/worker_admin.py` — drain/resume-from-drain operator actions.

## Config additions

See `.env.example`'s Phase 9 section for the full list
(`APPLICATION_AUTO_PREPARE_ENABLED`, `APPLICATION_WORKER_*`, `APPLICATION_PROVIDER_
CONCURRENCY_DEFAULT`, `APPLICATION_CIRCUIT_*`, `RECONCILE_WORKER_*`, `BROWSER_ASSIST_*`).
Every one defaults to `false`/conservative; none change `APPLICATION_EXECUTOR_ENABLED`/
`AUTO_SUBMIT_ENABLED`'s independent kill-switch semantics.

## Acceptance scenarios (CLAUDE.md Phase 9 section 42)

All implemented and tested; see the referenced test files for the exact assertions.

| # | Scenario | Test |
|---|---|---|
| A | FULL_TIME + CONFIRMED + mock provider → distributed worker → APPLIED | `test_applications_postgres_phase9.py::test_full_end_to_end_distributed_worker_reaches_applied` |
| B | Same application queued twice → one submission only | `test_applications_concurrency.py` (SQLite), `test_applications_postgres_phase9.py::test_duplicate_submission_race_only_one_execution_wins` (Postgres) |
| C | Two workers claim same execution → one owner only | `test_applications_postgres_phase9.py::test_four_concurrent_workers_never_double_claim_the_same_execution` |
| D | Worker dies before submit → lease recovered | `test_application_worker_crash_recovery.py::test_worker_dies_before_submit_lease_recovered_and_completes` |
| E | Worker dies after request may have been sent → `SUBMISSION_STATUS_UNKNOWN`, no blind retry | `test_application_worker_crash_recovery.py::test_resuming_a_row_stuck_in_submitting_never_calls_submit_twice` |
| F | CONTRACT never queued | `test_application_scheduler.py::test_contract_job_is_never_queued_by_scheduler` |
| G | LIKELY sponsorship never auto-submitted | `test_applications_gates.py` (Phase 8, unchanged) + `eligibility.auto_submit_eligible=False` |
| H | NO sponsorship → hard skip | `test_applications_gates.py` (Phase 8, unchanged) |
| I | CAPTCHA → `NEEDS_USER_ACTION` | `test_application_worker.py::test_worker_leaves_needs_user_action_for_captcha` |
| J | LOGIN/MFA → `NEEDS_USER_ACTION` | `test_application_mock_ats_expansion.py::test_login_required_blocks_auto_submit` |
| K | Job becomes inactive → no submit | `test_application_mock_ats_expansion.py::test_job_removed_scenario_blocks_before_submission` |
| L | JD changes to no sponsorship → no submit | `executor.process_execution()`'s pre-submit revalidation re-derives eligibility fresh; a hard-skip result becomes `JOB_NO_LONGER_ACTIVE` |
| M | Form schema changes → remap or stop | Phase 8's `application_form_baselines`/`check_and_record_baseline` (unchanged) |
| N | Daily budget exhausted → no new submissions | `app.applications.rate_limit`/`budget` (unchanged mechanism, new accounting view) |

## Honest limitations (CLAUDE.md section 57)

- A distributed executor does not mean every ATS supports auto-submit. As of this phase, the
  *only* provider with `submission_supported=True` is still the deterministic `MockATSProvider`
  — see `docs/application-provider-capabilities.md` for the full, truthful matrix.
- Browser form fill (`app.applications.browser_assist`) never implies permission to submit —
  it never clicks a submit/apply button, ever.
- CAPTCHA/MFA/login remain user action in every code path, worker-driven or not.
- Workday remains tenant-specific; no universal hidden auto-apply system exists or was built.
- No live production job application was submitted during this phase's development.
- Fixture (mock ATS) success is not real provider submission proof.
