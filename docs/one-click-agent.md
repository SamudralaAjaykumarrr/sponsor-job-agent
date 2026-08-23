# One-Click Autonomous Agent

The final user experience this feature builds:

```
OPEN WEBSITE -> click START AGENT -> leave it running
```

The agent then continuously finds eligible jobs, analyzes them, generates a
unique one-page tailored resume per job, prepares the application, executes
it as far as the provider safely/permittedly supports, and auto-submits only
where a legitimate, verified submission capability exists. Everything else
pauses at `NEEDS_USER_ACTION` / `READY_FOR_FINAL_SUBMIT` for you to review.

## The button

The dashboard's top section (`app/templates/dashboard.html`) shows:

- **Agent Status**: `STOPPED` / `STARTING` / `RUNNING` / `PAUSED` /
  `STOPPING` / `ERROR` (`app.agent.run_state.AgentRunState`), persisted in
  the `agent_run_state` table so a page refresh — or a process restart —
  never loses it.
- **START AGENT** / **STOP AGENT** (mutually exclusive, based on the current
  state) and **START AGENT (TEST MODE)** — a clearly separate, always-safe
  button (see "Test mode" below).
- Last cycle / next cycle / per-cycle counters (jobs processed, resumes
  generated, one-page success/overflow, applications prepared/submitted,
  needs-your-action, skipped, errors).

Both buttons are `POST`-only (`/agent/start`, `/agent/stop`) — there is no
`GET` route that can start the agent, matching the mutating-action
convention every other route in this project already follows.

## What one click actually turns on

`app.agent.orchestrator.AgentOrchestrator` is a single background asyncio
loop (mirroring `app.agent.scheduler.AgentScheduler`'s existing structure)
that, every `AGENT_INTERVAL_MINUTES` (default 15), runs one bounded cycle
through the stages this project already built, unmodified:

```
discovery (app.agent.cycle.run_discovery_cycle)
  -> resume optimization + one-page enforcement (app.resume_optimizer)
  -> application auto-prepare (app.applications.scheduler.run_cycle)
  -> application execution (app.applications.worker.ApplicationWorker)
```

It does **not** reimplement any of these — it only decides *when* they run,
and, for the two stages independently gated by a static `.env`-only flag
(`APPLICATION_EXECUTOR_ENABLED`, `APPLICATION_AUTO_PREPARE_ENABLED`),
temporarily raises that flag for as long as the agent is `RUNNING`,
restoring the operator's actual configured value the instant it stops
(`AgentOrchestrator._apply_config_overrides`/`_restore_config_overrides`).
**`AUTO_SUBMIT_ENABLED` is never touched by a normal run** — see
`docs/application-safety.md`'s "One-click agent" section for the full safety
picture, and `docs/one-page-resume-contract.md` for the resume half.

Discovery itself is turned on the existing way: `app.agent.state.set_enabled(True)`
— the same flag `/agent/toggle` already flips, kept working unchanged for
any existing caller.

## Restart recovery

If the process restarts while the desired state was `RUNNING`,
`app/main.py`'s `lifespan` calls `AgentOrchestrator.start()` again
automatically on the next startup — the user never has to re-click START
after a routine restart. This is safe because every stage the orchestrator
drives already has its own idempotent/leased claim mechanism (partial unique
indexes on `application_executions`/`resume_variants`, lease-expiry-only
recovery) independent of how the process starts; the orchestrator adds no
new state that could be duplicated by this.

## Stopping

`STOP AGENT` sets the desired state to `STOPPED` and waits (bounded, 90s)
for the in-flight cycle to reach a safe stopping point before flipping the
actual state — never an abrupt interruption of a possible in-flight
submission. Even if the wait times out and the loop is cancelled, an
execution genuinely mid-submission is separately protected by the existing
`SUBMITTING`/`SUBMITTED` crash-resume guard in
`app.applications.executor.process_execution()`, which converts an
interrupted execution to `SUBMISSION_STATUS_UNKNOWN` rather than ever
re-calling `submit()`.

## One blocked job never stops the rest

Every stage of `AgentOrchestrator._run_cycle_sync()` is wrapped in its own
try/except — a crash in resume optimization, say, still lets auto-prepare
and application execution run for that cycle, and the next cycle proceeds
normally regardless. Within a stage, the existing per-job isolation already
built into each stage (one bad provider/job never aborts the rest) is
unchanged.

## Needs Your Action

The dashboard's "Needs Your Action" section (`app.pipeline_dashboard.
build_needs_action_queue`) centralizes every blocker across the whole
pipeline — CAPTCHA/login/MFA, an unknown required field, a `LIKELY_SPONSOR`
review, a resume that couldn't reach one page — each row showing company,
role, current stage, what the agent already completed, and the exact action
required, linking to the job detail page's existing resolution controls
(Retry Preparation, Reconcile Submission, browser-assist Continue). Resolving
a blocker there always resumes the existing execution/session — it never
creates a duplicate application.

## Live Activity

`app.pipeline_dashboard.build_recent_activity` surfaces a rolling feed of
what just happened (state transitions + application audit events), showing
only already-public job-posting metadata (company/title) — never JD text,
resume content, or candidate profile fields.

## Test mode

`START AGENT (TEST MODE)` additionally:

1. Seeds a single, deterministic, idempotent `mock_ats` fixture job (fixed
   `external_job_id="agent-test-mode-fixture-1"` — re-clicking never creates
   a duplicate) via the exact same `app.pipeline.ingest_and_process()` entry
   point real discovery uses.
2. Temporarily allows `AUTO_SUBMIT_ENABLED` for the duration the agent is
   RUNNING in test mode (see `docs/application-safety.md`) — safe because
   `mock_ats` can never be a real job's provider.

One click then demonstrates the full loop end to end: discover fixture ->
FULL_TIME confirmed -> sponsorship confirmed -> JD analyzed -> unique
one-page resume produced -> claim check PASS -> ATS parse PASS -> application
prepared -> mock_ats submitted -> confirmation stored -> `APPLIED` -> the
agent continues. See `tests/test_agent_orchestrator.py::
test_full_test_mode_cycle_reaches_applied` for the automated version of this
acceptance scenario, and this doc's own manual verification below.

**No real production application is ever submitted in test mode or in
development** — the only `submission_supported=True` provider in this
project is `mock_ats`, a deterministic in-process fixture with no network
access.

## Config

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_INTERVAL_MINUTES` | 15 (falls back to `DISCOVERY_INTERVAL_MINUTES`) | Orchestrator cycle interval |
| `MAX_RESUMES_PER_CYCLE` | 10 | Bounded resume-optimization batch per cycle |
| `MAX_APPLICATIONS_PER_CYCLE` | 5 | Bounded auto-prepare batch per cycle |
| `MIN_ALIGNMENT_FOR_AUTO_PREPARE` | 40 | Minimum `internal_alignment_score` (0-100) to auto-prepare |
| `ONE_PAGE_RESUME_REQUIRED` | `true` | See `docs/one-page-resume-contract.md` |

`APPLICATION_EXECUTOR_ENABLED`, `APPLICATION_AUTO_PREPARE_ENABLED`, and
`AUTO_SUBMIT_ENABLED` remain exactly as documented in
`docs/application-safety.md` and `docs/phase9-production-application-workers.md`
— the agent's runtime override behavior is described above, not a change to
their defaults or independence from each other.

## Metrics

`GET /metrics` (Prometheus text format) now also includes, computed live
from the durable `agent_cycle_log`/`agent_run_state` tables (never an
in-process counter — see `app/agent/metrics.py`):
`agent_start_total`, `agent_stop_total`, `agent_cycles_total`,
`agent_jobs_processed_total`, `agent_resumes_generated_total`,
`agent_applications_prepared_total`, `agent_applications_submitted_total`,
`agent_user_action_total`, `agent_skipped_total`,
`one_page_resume_success_total`, `one_page_resume_overflow_total`,
`one_page_resume_compression_total`. No PII labels.

## Doctor

`python -m app.doctor` now also runs `app.agent.doctor`:
agent `RUNNING` but the background loop task is dead; agent `STOPPED` but an
application worker is still heartbeating as `WORKING`/`STARTING`; a static
source-inspection assertion that `app.applications.scheduler.run_cycle()`
still calls `evaluate_executor_eligibility()` (never trusts a cached flag).
`app.resume_optimizer.doctor` additionally checks: a `READY` resume variant
whose `page_count != 1`; an active application execution linked to a
promoted resume that isn't a one-page `READY` artifact.

## API

- `GET /agent/status` — legacy discovery-scheduler status, plus an
  `"orchestrator"` key with the full one-click-agent state.
- `POST /agent/start` (`test_mode: bool = false`) — returns quickly (`303`);
  all work happens in the background loop, never inside this request.
- `POST /agent/stop`
- `POST /agent/toggle` — kept working unchanged, the legacy Phase 2
  discovery-only toggle.
