# Application Reconciliation

## The durable rule (unchanged from Phase 8)

A submission whose outcome could not be determined (timeout, dropped connection) becomes
`ExecutionStatus.SUBMISSION_STATUS_UNKNOWN` and is **never retried automatically**.
`app.applications.reconcile.reconcile_execution()` is the only function that ever changes a
`SUBMISSION_STATUS_UNKNOWN` execution's status — a plain human/operator action via the CLI
(`python -m app.applications.cli reconcile ...`) or the dashboard
(`POST /executions/{id}/reconcile`).

## What Phase 9 adds: an automated *evidence-gathering* pass, not a new resolver

`app.applications.reconcile_worker.run_pass()` (CLAUDE.md Phase 9 section 8) does **not**
introduce a second way to resolve an execution. It:

1. Lists every `SUBMISSION_STATUS_UNKNOWN` execution.
2. For each, checks whether that job's `ApplicationProvider` declares
   `capabilities.confirmation_recheck_supported = True` — a truthful, per-provider flag,
   `False` for every real ATS adapter in this project (none of Greenhouse/Lever/generic expose
   a legitimate, candidate-usable "check my application status" interface). Any such execution
   is left completely untouched, exactly as Phase 8 already behaves.
3. Only for a provider that *does* support it (today: only the deterministic `MockATSProvider`,
   to prove the mechanism exists and works), calls the new optional
   `ApplicationProvider.check_submission_status(job, execution)` hook and inspects the result:
   - Genuine positive evidence (`confirmed=True` with a real confirmation id) → calls
     `reconcile_execution(execution_id, "confirmed_applied", ...)` — the *same* function a
     human would call, just with the evidence supplied automatically instead of typed in.
   - Genuine negative evidence (`confirmed=False`, meaning the provider's own record store has
     no entry for this submission at all) → calls `reconcile_execution(execution_id,
     "confirmed_not_submitted", ...)`.
   - No evidence either way (`None`) → left `SUBMISSION_STATUS_UNKNOWN`, unchanged, for a
     human.

Nothing here ever fabricates a confirmation. The mock ATS's own "server-side record"
(`mock_ats_server_records` table, populated by `MockATSProvider.submit()` even when the client
observes a timeout, simulating a request that genuinely reached the server) is what makes this
a *real* evidence lookup for testing purposes rather than a guess — see
`app/applications/mock_ats.py`'s `_record_server_side_submission()`.

## Running it

```
python -m app.applications.cli reconcile-worker [--limit N]
POST /applications/reconcile-worker/run           # dashboard manual trigger
RECONCILE_WORKER_ENABLED=true                     # (flag reserved for a future scheduled loop;
                                                   #  this phase ships the pass as an explicit,
                                                   #  operator-triggered action — see "Honest
                                                   #  limitation" below)
```

## Honest limitation

No real ATS adapter in this project implements `check_submission_status()` — as established in
`docs/application-provider-capabilities.md`, none of the ATSes this project discovers jobs from
expose a documented, candidate-usable, unauthenticated (or otherwise legitimately accessible)
"check my application's status" interface. This means, in production against real providers,
every `SUBMISSION_STATUS_UNKNOWN` execution today genuinely requires a human to check the
ATS/email directly and reconcile manually — the automated pass exists and is fully tested
against the mock fixture, but has nothing to automate yet against a real ATS. `RECONCILE_
WORKER_ENABLED` is defined for a future continuous-loop wrapper (mirroring the scheduler's
own on/off flag) but this phase does not run it on a timer by default; use the CLI/dashboard
trigger above.
