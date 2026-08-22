# Phase 8 — Application Executor

## Objective

Take an eligible FULL_TIME U.S. job from `READY_TO_APPLY`/`REVIEW_REQUIRED` through a safe,
auditable execution pipeline: prepare package -> map fields -> fill draft -> validate -> submit
*only where explicitly permitted* -> confirm -> `APPLIED`. Everything else stops at
`NEEDS_USER_ACTION` with a preserved draft.

## Design decisions

- **Two-layer state model**, mirroring the Phase 7 `sponsorship_decisions` pattern:
  `jobs.application_state` stays the coarse, dashboard-facing summary (a handful of new values
  added: `EXECUTION_QUEUED`, `NEEDS_USER_ACTION`, `SUBMITTING`, `SUBMISSION_STATUS_UNKNOWN`,
  `SUBMISSION_FAILED`, `DUPLICATE_APPLICATION_BLOCKED`), while `application_executions` carries
  the full, versioned, auditable execution status machine
  (`app.applications.models.ExecutionStatus`). A job can have multiple execution rows over time
  (retries after a fixed problem); only one may be active (non-terminal) at once, enforced by
  `app.applications.duplicate`.
- **Eligibility is re-derived, not trusted**, at queue time (`app/applications/eligibility.py`)
  independently of whatever `application_state` the pipeline already computed — defense in depth
  matching the durable rule "no automated submission unless positively FULL_TIME".
- **`ApplicationProvider`** (`app/applications/provider.py`) is a new, separate interface from
  `JobProvider` (discovery). Adapters implemented: Greenhouse (form discovery **live-verified**
  against the real public `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`
  Job Board API — this genuinely returns structured field name/label/type/required/choices,
  including the EEOC demographic questions and a sponsorship question on a real posting), Lever
  (public API verified to expose only `hostedUrl`/`applyUrl` — no structured question schema, so
  Lever is honestly `ASSIST_ONLY`/`UNSUPPORTED` for form discovery), and a generic
  `AssistOnlyProvider` fallback (Ashby/Workable/SmartRecruiters/BambooHR/Breezy/Recruitee/Workday)
  that only supplies the known apply URL — none of these were live-tested for structured field
  discovery, so none claim more than `UNSUPPORTED` discovery / `ASSIST_ONLY` overall.
- **No real submission is implemented for any real ATS.** Every real adapter's
  `capabilities.submission_supported = False` and `automation_policy = ASSIST_ONLY`. The only
  provider with `submission_supported = True` is the deterministic, in-process `MockATSProvider`
  (`app/applications/mock_ats.py`), used for end-to-end testing of the executor mechanics
  themselves (queue -> map -> fill -> validate -> submit -> confirm) without touching a real ATS,
  per CLAUDE.md section 52.
- **Employment type**: `app/matching/employment_type.py` gets an additive
  `classify_employment_type()` returning a positive `EmploymentType` enum (`app.models`) rather
  than the existing permissive boolean `is_full_time()` (kept unchanged — still used by
  `app.agent.cycle`'s discovery-time filter). The executor gate requires a *positive* `FULL_TIME`
  classification; `UNKNOWN` never auto-submits (queues ASSIST-only / `NEEDS_USER_ACTION`),
  matching the CLAUDE.md Phase 8 durable rule verbatim.

## Modules

- `app/applications/models.py` — enums/dataclasses (ExecutionStatus, ExecutionMode,
  AutomationPolicy, PolicyReason, FieldCategory, FieldConfidence, ApplicationField,
  ApplicationCapabilities, FormSnapshot, DraftResult, ValidationResult, SubmitResult,
  ConfirmationResult).
- `app/applications/eligibility.py` — the pre-execution gate (section 2).
- `app/applications/mapping.py` — deterministic label/alias field-matching engine (section 14).
- `app/applications/schema.py` — candidate-profile -> `ApplicationField` mapping (sections 8-13).
- `app/applications/provider.py` — `ApplicationProvider` ABC + registry.
- `app/applications/providers_greenhouse.py`, `providers_lever.py`, `providers_generic.py`.
- `app/applications/mock_ats.py` — deterministic local mock ATS + `MockATSProvider`.
- `app/applications/repo.py` — persistence for `application_executions` /
  `application_answer_snapshots` / `application_audit_log`.
- `app/applications/executor.py` — orchestrates prepare -> map -> fill -> validate -> (submit).
- `app/applications/queue.py` — execution queue on top of `application_executions` leasing
  columns, reusing the same atomic-`UPDATE ... WHERE` claim pattern as `app.workers.leasing`.
- `app/applications/duplicate.py`, `rate_limit.py`, `reconcile.py`, `doctor.py`, `metrics.py`,
  `cli.py`.

## Honest limitations (see CLAUDE.md section 71 / this doc's own section below)

- Discovery support (Phase 3-7) is not submission support — a provider fully supported for
  *finding* jobs may still be `UNSUPPORTED`/`ASSIST_ONLY` for *applying*.
  Only Greenhouse form discovery is live-verified in this phase; Lever, Ashby, Workable,
  SmartRecruiters, BambooHR, Breezy, Recruitee, Workday application forms were not.
- Fixture/mock-ATS submission tests are not equivalent to a real production ATS submission.
  `MockATSProvider` is the only provider this phase's automated tests actually submit through.
- No real job application is submitted anywhere in this phase's development or its test suite.
