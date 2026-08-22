# Browser Session Reconstruction and Ownership

## Honest scope: reconstruction, not reattachment

Phase 10 already documented this limitation and Phase 11 keeps it exactly as honest: this project
makes **no claim** of true cross-process browser reattachment. `app.applications.browser_runtime`
keeps live Playwright objects in an in-process `_REGISTRY` dict, keyed by `session_id` --
that registry is empty in any OTHER process, including a fresh instance of the SAME process after
a restart.

What Phase 11 adds is making the RECOVERY path itself more honest, ownership-safe, and countable:

1. If the browser is still live in THIS process, `resume_session()` just re-scans the current
   page (unchanged from Phase 10).
2. If the browser/process is gone and the session's last known status was pre-submission (any
   `PAUSED_*`, `ACTIVE`, `READY_FOR_FINAL_SUBMIT`, `STARTING`), a **fresh** browser safely reopens
   at the session's saved `application_url` and rediscovers the form from scratch. This is a
   `reconstructed_count` increment (a new `browser_assist_sessions` column), not a "resumed"
   claim.
3. If the last known status was `AWAITING_USER_SUBMIT`, the outcome is NEVER guessed -- the
   session becomes `SUBMISSION_STATUS_UNKNOWN` for explicit human reconciliation (unchanged from
   Phase 10).

`config.BROWSER_SESSION_RECONSTRUCT_ENABLED` (default `True`) lets an operator force path 2 to
require an explicit human restart instead, without touching code.

## Only reconstructable state is ever persisted

`browser_assist_sessions` columns used for recovery: `application_url`, `provider`,
`form_fingerprint`, `current_step`, `stage`, `step_confidence`, `answers_version`,
`resume_artifact_hash`, `user_action_reason`, `execution_id`, `reconstructed_count`. Never a
serialized browser/page/context object, never a password/MFA code/cookie/auth token.

## Session ownership (distributed leasing)

`app.applications.browser_session.claim_session()`/`renew_session_lease()`/
`release_session_lease()` is the same atomic `UPDATE ... WHERE (unleased OR lease-expired)`
pattern every other queue in this project uses -- correctness comes from the database's own
single-writer serialization, verified live under real PostgreSQL with 8 concurrent claimers in
`tests/test_browser_session_postgres.py::test_concurrent_claim_only_one_worker_wins` (exactly one
ever wins).

Phase 11's change: `app.applications.browser_assist`'s four browser-touching orchestration
functions (`start_session`, `resume_session`, `advance_step`,
`attempt_user_submit_reconciliation`) now actually CALL this claim/release pair -- Phase 10 built
the leasing primitives but never wired them into the orchestration layer, meaning two concurrent
callers for the same session could both try to drive the same in-process browser. Now:

```python
owned, session = _claim_or_conflict(session_id)
if not owned:
    return {"ok": False, "detail": "session is currently owned by another worker/process", ...}
try:
    ...  # the actual browser work
finally:
    browser_session.release_session_lease(session_id)
```

The lease is held ONLY for the duration of one orchestration call, and released again
unconditionally at the end -- regardless of the resulting status. This is deliberate (CLAUDE.md
Phase 11 section 27): a `PAUSED_*` session must never hold a lease forever, or no other worker
could ever pick it back up.

`claim_session()` was made re-entrant for the SAME `worker_id` (a new `OR lease_owner = ?` clause
in its `WHERE`) so that `mark_user_action_complete()`, which internally delegates to
`resume_session()`, does not conflict with its own just-acquired lease. `_WORKER_ID =
f"proc-{os.getpid()}"` is stable within one process and distinct across processes, so real
cross-process conflicts are still caught exactly as before.

## Doctor checks

- `paused_session_holding_lease` (warning): a `PAUSED_*` session still holding an unexpired
  lease -- should have been released.
- `browser_session_owner_conflict` (serious): `lease_owner` and `worker_id` disagree while the
  lease is unexpired -- `claim_session()` always sets both together atomically, so disagreement
  indicates corrupted bookkeeping, not two simultaneous legitimate owners (the schema's partial
  unique index on `active=1` already makes true dual ownership of one job's session impossible).

## Honest limitations

- A worker crash mid-form-fill is recovered by reconstruction (fresh browser, full rediscovery),
  never by resuming exactly where the crashed process left off.
- Reconstruction always starts back at the session's saved `application_url` -- a mid-form-flow
  page (e.g. step 3 of a Workday application) is generally NOT independently bookmarkable, so
  reconstruction after a crash mid-flow typically restarts from the beginning of the form, not
  step 3. This is a genuine, disclosed limitation, not a bug to silently work around.
