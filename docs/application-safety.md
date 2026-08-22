# Application Executor Safety

## The FULL_TIME hard gate

Durable rule (also in `CLAUDE.md`): **no automated application submission
may occur unless the job has been positively classified as FULL_TIME.**
`app.matching.employment_type.classify_employment_type()` must return
exactly `EmploymentType.FULL_TIME`; `UNKNOWN` is never treated as FULL_TIME
for submission purposes. This is the first, unconditional check in
`app.applications.eligibility.evaluate_executor_eligibility()`.

An explicit non-full-time signal (CONTRACT/C2C/PART_TIME/INTERNSHIP/
TEMPORARY/SEASONAL/FREELANCE) hard-skips before the job ever reaches the
executor — `eligibility.hard_skip=True`, `blocking_category="EMPLOYMENT_TYPE"`.
An ambiguous/silent job (`UNKNOWN`) is still allowed to *enter the queue*
for ASSIST-only preparation, but `auto_submit_eligible` is always `False`.

## Safe defaults / kill switch

`APPLICATION_EXECUTOR_ENABLED` and `AUTO_SUBMIT_ENABLED` both default to
`false`. `app.applications.executor.queue_application()` raises
`ExecutorDisabledError`/`AutoSubmitDisabledError` rather than silently
no-op-ing, so the CLI/dashboard can show an honest message. Both flags are
printed on every startup (`app/main.py`'s `lifespan`) — never silently
enabled. Discovery/analysis/resume generation are never gated by either
flag.

## AUTO_PERMITTED gating (CLAUDE.md Phase 8 section 45)

`app.applications.executor._auto_submit_permitted()` requires **all** of:

1. `APPLICATION_EXECUTOR_ENABLED=true` and `AUTO_SUBMIT_ENABLED=true`
2. the execution's mode is `AUTO_PERMITTED` (never inferred/upgraded from ASSIST)
3. `eligibility.auto_submit_eligible` (FULL_TIME positive, `sponsorship_status
   == CONFIRMED_SPONSOR`, resume claim-check passed, answers complete)
4. `provider.capabilities.submission_supported` is `True`
5. this specific draft's `validate()` result is `AutomationPolicy.PERMITTED_AUTO`
   (no CAPTCHA/MFA/auth-required, no unresolved required field of any kind)

Any single failed condition falls back to ASSIST behavior — the draft is
preserved at `SUBMISSION_READY` or `NEEDS_USER_ACTION`. There is no generic
"force submit" parameter anywhere in `app/applications/`.

Immediately before actually calling `submit()`, two more checks run:
`app.applications.rate_limit.check_rate_limits()` and
`app.applications.duplicate.check_duplicate()` — re-checked at this exact
moment because time has passed since the job was queued.

## CAPTCHA / MFA / auth

`ApplicationProvider.validate()` detects these on the specific `FormSnapshot`
(`form.captcha_present`/`form.mfa_required`) and returns
`AutomationPolicy.USER_ACTION_REQUIRED` with the matching `PolicyReason`.
No code path bypasses, solves, or works around any of these — there is no
CAPTCHA-solving, stealth-browsing, fingerprint-evasion, proxy-rotation, or
login-bypass code anywhere in this codebase. No password is ever stored.

## Duplicate protection (2 layers)

1. **Same job, concurrent workers**: `application_executions(job_id) WHERE
   active=1` is a partial UNIQUE index — the atomic, database-enforced
   guard. `app.applications.repo.create_execution()` observes a violation as
   `DuplicateExecutionError`. Verified under real concurrent threads
   (`tests/test_applications_concurrency.py`) and against real PostgreSQL
   (`tests/test_applications_postgres.py`).
2. **Same underlying posting, different job rows**:
   `app.applications.duplicate.check_duplicate()` checks
   provider+external_job_id, canonical_url, and company+title+location
   against jobs already `APPLIED` — catches a manually re-pasted duplicate
   (the manual-ingest path doesn't run Phase 3's discovery-cycle dedup).

## Idempotency / unknown outcomes

A submission whose outcome couldn't be determined (timeout, dropped
connection) becomes `ExecutionStatus.SUBMISSION_STATUS_UNKNOWN` and is
**never retried automatically** — `process_execution()` refuses to re-run
the pipeline for an execution already in this status (see the explicit
guard at the top of the function). Resolution is always an explicit human
action via `app.applications.reconcile.reconcile_execution()`.

## Confirmation before APPLIED

`ExecutionStatus.APPLIED` is only ever reached after
`ApplicationProvider.verify_confirmation()` returns `confirmed=True` with
real evidence (a confirmation id/URL/text). A `submit()` success alone
(`SubmitResult.success=True`) is not sufficient — if no confirmation
evidence is found, the execution becomes `SUBMISSION_STATUS_UNKNOWN`
instead, requiring reconciliation.

## Rate limiting

`MAX_APPLICATIONS_PER_HOUR` / `MAX_APPLICATIONS_PER_DAY` /
`MAX_APPLICATIONS_PER_COMPANY_PER_DAY` (safe defaults: 5/20/2) are enforced
by counting `application_audit_log`'s `submit_attempted` events in the
relevant window, queried live against the shared database — already
fleet-wide the moment `DATABASE_URL` points at shared Postgres.

## Privacy / logging

`application_audit_log` never logs field values, only `event_type` + a
short structural `detail` string. `application_answer_snapshots` minimizes
sensitive-category (demographic/legal/voluntary-disclosure/signature)
values to a bounded SHA-256 fingerprint rather than storing them verbatim.
No password, MFA code, session token, or CAPTCHA token is ever stored.
