# Autonomous Orchestration Architecture

How `app.agent.orchestrator.AgentOrchestrator` ties every existing,
previously-independent-and-opt-in stage into one agent, and why it was built
this way rather than by duplicating any of those stages' logic.

## Before this feature

Per `docs/autonomous-agent.md`'s own accumulated history, this project had
grown FIVE separate opt-in stages, each with its own flag and (for two of
them) its own background scheduler:

1. Discovery (`app.agent.scheduler.AgentScheduler`, `AGENT_ENABLED`)
2. Resume optimization (`app.resume_optimizer.scheduler.
   ResumeOptimizationScheduler`, `RESUME_OPTIMIZATION_ENABLED`)
3. Application auto-prepare (`app.applications.scheduler.run_cycle()`,
   `APPLICATION_AUTO_PREPARE_ENABLED` + `APPLICATION_EXECUTOR_ENABLED`)
4. Application execution (`python -m app.applications.worker run`, a
   separately-launched standalone process)
5. Browser assist (`app.applications.browser_assist`, manually triggered)

A user wanting the full pipeline running had to understand and independently
configure/launch all five. This feature's job was to make "click START"
equivalent to all five being correctly coordinated, without weakening any of
the safety properties each one already had.

## Design: coordinate, never duplicate

`AgentOrchestrator` is deliberately thin. Its cycle body
(`_run_cycle_sync`) is four try/except-wrapped calls into existing,
unmodified entry points:

```python
run_discovery_cycle()                              # app.agent.cycle
optimize_resume(job_id)  # per eligible job          # app.resume_optimizer.optimizer
applications_scheduler.run_cycle(limit=...)         # app.applications.scheduler
ApplicationWorker(single_cycle=True).run()          # app.applications.worker
```

No stage's internal logic is reimplemented, forked, or partially copied.
The orchestrator only decides *when* these run and, for two of them,
*whether they're allowed to do anything at all this run* (see "Config
override, not a parallel gate" below).

## Why reuse `ApplicationWorker.run(single_cycle=True)` specifically

`python -m app.applications.worker run --once` already existed as a
sanctioned "run one bounded pass and exit" entry point (see
`app/applications/worker.py`'s own `main()`). The orchestrator creates
exactly one `ApplicationWorker` instance (lazily, on first use) and calls
`.run()` on it every cycle — the same worker identity is reused for the
orchestrator's whole lifetime, not a fresh one per cycle, and not a second,
parallel execution loop. This satisfies "reuse the existing queue/leasing
architecture" and "do not launch duplicate workers" literally: the
orchestrator's application-execution stage *is* an `ApplicationWorker`, just
driven by the agent's schedule instead of a standalone process's own sleep
loop. A real standalone worker fleet can run at the same time without
double-processing anything — `app.applications.queue.claim_execution_batch`'s
atomic lease claim is what actually serializes concurrent claimers,
regardless of how many `ApplicationWorker` instances (in-process or
standalone) exist.

## Config override, not a parallel gate

`APPLICATION_EXECUTOR_ENABLED` and `APPLICATION_AUTO_PREPARE_ENABLED` are
read as plain `app.config` module attributes by every consumer
(`app.applications.executor.queue_application`, `app.applications.scheduler.
run_cycle`, `ApplicationWorker.run`, the dashboard's status display, several
doctor checks). Rather than threading a second "or the agent is running"
condition through each of those call sites — which would create two
different, potentially-drifting notions of "is this enabled" — the
orchestrator sets the module attribute itself for the duration it's
`RUNNING`, snapshotting the operator's actual value first and restoring it
exactly on stop (`_apply_config_overrides`/`_restore_config_overrides`).
Every existing consumer sees a single, consistent truth with zero code
changes to those consumers. `AUTO_SUBMIT_ENABLED` deliberately follows a
different rule — see `docs/application-safety.md`.

This is safe because these are plain-Python module attributes read via
`from app import config; config.X` (attribute access at call time, not
`from app.config import X` which would bind the value at import time) —
already the pattern every consumer in this codebase uses, verified by
inspection before relying on it.

## Durable state, not an in-process flag

`app.agent.run_state` persists desired/actual state
(`STOPPED`/`STARTING`/`RUNNING`/`PAUSED`/`STOPPING`/`ERROR`) in a
single-row `agent_run_state` table, and every completed cycle's counters in
an append-only `agent_cycle_log` table — both read/written through
`app.db.db_session()` like every other piece of persisted state in this
project. This is what makes restart recovery and the `/metrics`
counters possible without an in-process-only accumulator that a restart
would silently reset.

## One stage failing never aborts the cycle

Each of the four stage calls in `_run_cycle_sync` has its own try/except; a
crash in one is logged and counted in `counters.errors`, and the remaining
stages still run for that cycle. The outer `_loop()` has its own top-level
try/except around the whole cycle as a final safety net — matching this
project's existing "one bad provider/job never aborts the rest" principle,
extended one level up to "one bad stage never aborts the rest."

## What this module intentionally does NOT do

- It does not implement any gate (FULL_TIME, sponsorship, claim-check,
  duplicate protection, rate limits, CAPTCHA/MFA detection) itself — every
  one of those lives exactly where it always did, in `app.pipeline`/
  `app.applications.eligibility`/`app.applications.executor`, and the
  orchestrator calls through them unchanged.
- It does not implement resume compression logic itself — that's
  `app.resume_optimizer.one_page` (see `docs/one-page-resume-contract.md`).
- It does not decide submission policy — `app.applications.executor.
  _auto_submit_permitted()` is untouched.
- It is not a distributed worker fleet coordinator — `app.workers.*` /
  `app.applications.worker.*`'s leasing and circuit-breaker machinery are
  reused, not replaced or reimplemented.

## Testing approach

`tests/test_agent_orchestrator.py` calls `AgentOrchestrator._run_cycle_sync()`
directly (the exact synchronous body the real asyncio loop calls every
interval) rather than waiting on real wall-clock sleep timers, keeping the
mandatory TEST MODE end-to-end acceptance test
(`test_full_test_mode_cycle_reaches_applied`) fast and deterministic while
still exercising the real, unmodified pipeline with zero manual button
clicks anywhere in the test.
