# Application State Machine

Two layers, mirroring the Phase 7 `sponsorship_decisions` pattern:

- **`jobs.application_state`** (`app.models.ApplicationState`) — the coarse,
  dashboard-facing summary. Pre-Phase-8 values (`READY_TO_APPLY`,
  `REVIEW_REQUIRED`, `APPLIED`, `INTERVIEW`, `REJECTED`, the `SKIPPED_*`
  family, `CLAIM_VALIDATION_FAILED`) are unchanged in meaning. Phase 8 adds:
  `EXECUTION_QUEUED`, `NEEDS_USER_ACTION`, `SUBMITTING`,
  `SUBMISSION_STATUS_UNKNOWN`, `SUBMISSION_FAILED`,
  `DUPLICATE_APPLICATION_BLOCKED`, `WITHDRAWN`.
- **`application_executions.status`** (`app.applications.models.ExecutionStatus`)
  — the fine-grained, per-attempt machine. `app.applications.repo.mirror_job_state()`
  is the only code that writes the coarse summary from the fine-grained one.

## ExecutionStatus values and transitions

```
QUEUED --(process_execution starts)--> STARTED
STARTED --(eligibility fails)--> PERMANENT_SUBMISSION_FAILURE  [terminal]
STARTED --(resume artifact bad)--> VALIDATION_REQUIRED
STARTED -----------------------------> FORM_DISCOVERED
FORM_DISCOVERED --(schema drift)-----> NEEDS_USER_ACTION
FORM_DISCOVERED -----------------------------------------> FORM_FILLED
FORM_FILLED --(validate() fails)-----> NEEDS_USER_ACTION
FORM_FILLED --(validate() ok, not auto-eligible)--> SUBMISSION_READY
FORM_FILLED --(validate() ok, AUTO_PERMITTED eligible)--> SUBMITTING
SUBMITTING --(status_unknown)--------> SUBMISSION_STATUS_UNKNOWN
SUBMITTING --(submit failed)---------> PERMANENT_SUBMISSION_FAILURE  [terminal]
SUBMITTING --(submit success)--------> SUBMITTED
SUBMITTED --(no confirmation evidence)--> SUBMISSION_STATUS_UNKNOWN
SUBMITTED --(confirmed)--------------> APPLIED  [terminal]
```

`DUPLICATE_APPLICATION_BLOCKED` and `WITHDRAWN` are reached from
`queue_application()`'s pre-check and `reconcile_execution()` respectively
(both terminal).

## `active` and duplicate protection

`application_executions.active` starts at `1` and flips to `0` only when
`status` reaches `app.applications.models.TERMINAL_STATUSES`:
`APPLIED`, `SUBMISSION_FAILED`, `PERMANENT_SUBMISSION_FAILURE`,
`DUPLICATE_APPLICATION_BLOCKED`, `WITHDRAWN`. A partial UNIQUE index on
`(job_id) WHERE active=1` means only one execution can be active per job at
a time — the atomic guard behind CLAUDE.md Phase 8 section 61/32.

`NEEDS_USER_ACTION`, `VALIDATION_REQUIRED`, and `SUBMISSION_STATUS_UNKNOWN`
are deliberately **not** terminal — they stay `active=1` so a second
concurrent execution attempt is blocked while a human resolves them, and so
`process_execution()` can be safely re-run (the "Retry Preparation"
dashboard action) once the underlying problem is fixed. The one exception:
`SUBMISSION_STATUS_UNKNOWN` specifically refuses to be re-run by
`process_execution()` (see `docs/application-safety.md`'s idempotency
section) — it can only move forward via `reconcile_execution()`.

## Coarse job-state mirror table

| ExecutionStatus | jobs.application_state |
|---|---|
| QUEUED, STARTED, SUBMISSION_READY | EXECUTION_QUEUED |
| VALIDATION_REQUIRED, NEEDS_USER_ACTION | NEEDS_USER_ACTION |
| SUBMITTING, SUBMITTED | SUBMITTING |
| SUBMISSION_CONFIRMED, APPLIED | APPLIED |
| SUBMISSION_FAILED, RETRYABLE_SUBMISSION_FAILURE, PERMANENT_SUBMISSION_FAILURE | SUBMISSION_FAILED |
| DUPLICATE_APPLICATION_BLOCKED | DUPLICATE_APPLICATION_BLOCKED |
| WITHDRAWN | WITHDRAWN |
| SUBMISSION_STATUS_UNKNOWN | SUBMISSION_STATUS_UNKNOWN |
| FORM_DISCOVERED, FORM_MAPPED, FORM_FILLED | (no mirror — job row unchanged) |

## Manual transitions

`app.applications.tracker.ALLOWED_MANUAL_TRANSITIONS` was extended
additively for the new states (e.g. `NEEDS_USER_ACTION -> APPLIED` for "Mark
Applied Manually" after a human completes a CAPTCHA-stopped application by
hand). There is no manual transition into any submitted/confirmed state that
bypasses a gate — every entry still requires the human to explicitly choose
it via the dashboard/CLI.
