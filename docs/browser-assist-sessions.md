# Browser-Assist Sessions

## Model

`browser_assist_sessions` (SQLite + Postgres, `app/migrations.py` migration 25) is one row per
browser-assist attempt for one `application_executions` row. It never stores a password, MFA
code, cookie, or raw auth token.

| Column | Purpose |
|---|---|
| `session_id` / `execution_id` / `job_id` / `provider` / `application_url` | Identity |
| `status` | `app.applications.browser_session.BrowserSessionStatus` |
| `active` | 1 while non-terminal; 0 once `CONFIRMED`/`CLOSED`/`EXPIRED` — backs a **partial unique index on `(job_id) WHERE active=1`**, the same pattern `application_executions` already uses, so two workers/dashboard clicks can never both start a second live session for a job that already has one. Verified live under real Postgres with 8 concurrent threads racing `claim_session()` — exactly one ever wins (`tests/test_browser_session_postgres.py`). |
| `current_step` / `total_steps_if_known` | Multi-step tracking |
| `form_fingerprint` | SHA-256 of the discovered field signature — compared on every resume/mark-user-action-complete so a changed form is never silently remapped |
| `mapped_field_count` / `unresolved_field_count` | What the last discovery pass found |
| `needs_user_action` / `user_action_reason` | Set whenever `status` is a `PAUSED_*` value |
| `confirmation_id` / `confirmation_url` / `confirmation_text_fingerprint` | Bounded, non-secret evidence captured after a manual submit |
| `lease_owner` / `lease_attempt_id` / `lease_expires_at` | Distributed ownership (same atomic `UPDATE ... WHERE (unleased OR expired)` pattern as every other queue in this project) |

## Status machine

```
STARTING -> DISCOVERING -> ACTIVE -> READY_FOR_FINAL_SUBMIT -> AWAITING_USER_SUBMIT -> CONFIRMED
                        \-> PAUSED_LOGIN_REQUIRED / PAUSED_CAPTCHA / PAUSED_MFA_REQUIRED
                        \-> PAUSED_LEGAL_QUESTION / PAUSED_UNKNOWN_FIELD / PAUSED_FORM_CHANGED
                        \-> PAUSED_PLATFORM_RESTRICTED / PAUSED_UNSUPPORTED_SUBMISSION
any non-terminal status -> SUBMISSION_STATUS_UNKNOWN (never guessed away)
any status -> CLOSED (explicit) / EXPIRED (stale-session reaper)
```

`CONFIRMED` / `CLOSED` / `EXPIRED` are the only terminal statuses (`active` flips to 0).
`SUBMISSION_STATUS_UNKNOWN` deliberately stays `active=1`, matching Phase 8's own
`application_executions` rule: an unresolved outcome keeps blocking a second concurrent attempt
until a human resolves it.

## The "persistent window" model, honestly

Playwright's sync API must be driven from one consistent OS thread. Each live session owns a
dedicated single-thread `ThreadPoolExecutor` (`app.applications.browser_runtime._LiveSession`)
that keeps the browser/context/page open across multiple separate calls into this module — one
dashboard request opens the browser and discovers the form; a *later* dashboard request (after
the candidate logs in by hand in that same visible window) calls `rediscover()` against the
*same* live page.

This only works while the owning process stays alive. If it restarts, the in-process registry
(`browser_runtime._REGISTRY`) is empty, and `resume_session()` makes an honest choice:

- If the session's last known status was pre-submission (any `PAUSED_*`, `ACTIVE`,
  `READY_FOR_FINAL_SUBMIT`, `STARTING`) — safe to restart. A fresh browser opens at the exact
  same `application_url`, and the same discovery/fill pass runs again from scratch. Never reuses
  a stale field mapping.
- If the session's last known status was `AWAITING_USER_SUBMIT` — the submission may or may not
  have gone through while the window was unreachable. This is **never guessed**: the session is
  marked `SUBMISSION_STATUS_UNKNOWN` and left for a human to reconcile.

This module never claims cross-process browser reattachment (e.g. via a saved CDP endpoint) as
a tested guarantee — that would be a stronger claim than what was actually built and verified.

## Lifecycle actions

| Function | What it does |
|---|---|
| `browser_assist.start_session(execution_id)` | Re-derives eligibility independently (never trusts a stale `application_state`), opens the browser, discovers + fills the form |
| `browser_assist.resume_session(session_id)` | Live: rediscovers the current page. Not live: restarts fresh (safe) or marks `SUBMISSION_STATUS_UNKNOWN` (unsafe) |
| `browser_assist.mark_user_action_complete(session_id)` | Same as `resume_session`, but only valid from a `PAUSED_*` status |
| `browser_assist.advance_step(session_id)` | Clicks a safe "Next"/"Continue" control (never a submit-shaped one), rediscovers the resulting page |
| `browser_assist.close_session(session_id)` | Always safe, even if the browser was already gone |
| `browser_assist.expire_stale_sessions()` | Reaps sessions idle past `BROWSER_SESSION_TIMEOUT_MINUTES` — never auto-submits or deletes the row |
| `browser_assist.attempt_user_submit_reconciliation(session_id)` | After a candidate manually clicks submit themselves, inspects the current page for genuine confirmation evidence; only that marks the linked execution `APPLIED` |

## Form-change / drift handling

Every discovery pass computes a field-signature fingerprint (`browser_runtime._fingerprint_fields`).
`resume_session()`/`mark_user_action_complete()` compare it to the session's stored fingerprint
and pause with `PAUSED_FORM_CHANGED` on any difference — never silently remap. `advance_step()`
is the one deliberate exception (`check_drift=False`): moving to a genuinely different page of a
multi-step form is *expected* to change the fingerprint; that is normal progression, not drift.
An earlier version applied the same check after every step advance and paused unusable — a real
bug this phase's own E2E suite caught (see `docs/phase10-real-ats-assist.md`).

## Metrics, doctor, dashboard

- `app.applications.metrics.collect_browser_assist()`: `browser_assist_sessions_active/paused`,
  `..._login_required`, `..._captcha_required`, `..._ready_for_submit`, `..._confirmation_unknown`,
  `..._confirmed`, `..._form_drift`, `..._failures`, `..._live_in_process`.
- `app.applications.doctor.run_doctor()` gained 9 browser-assist checks: session-without-
  execution, non-FULL_TIME/non-eligible-sponsorship session, stale-but-still-active session,
  confirmation-without-APPLIED-execution, a static scan that `browser_runtime` never grows a
  submit-click capability, and a forbidden-secret-field text scan.
- `/applications/browser-sessions` (list) and `/applications/browser-sessions/{id}` (detail,
  with Resume/Continue/Advance Step/Reconcile/Close actions) — linked from `/applications` and
  from each job's detail page.

## Testing

- `tests/test_browser_session_model.py` — pure DB-layer lifecycle/leasing/reaping.
- `tests/test_browser_runtime_unit.py` — pure helpers + the concurrency guard, no real browser.
- `tests/test_browser_assist_orchestration.py` — the full state machine with
  `app.applications.browser_runtime` mocked (gates, pauses, resume/crash-recovery, reconciliation).
- `tests/test_browser_assist_e2e.py` (marked `browser`) — real Chromium against
  `tests/browser_fixtures.py`'s local sandbox (multi-step, login, CAPTCHA, legal question,
  conditional field, form drift, manual-submit confirmation, file upload, never-clicks-submit).
- `tests/test_browser_session_postgres.py` (marked `postgres`) — the migration and the
  concurrent-claim guarantee under real PostgreSQL.
