# Operations Runbook (CLAUDE.md Phase 15 section 79)

Consolidated day-to-day operator reference. Each section links to the subsystem doc with
full detail rather than duplicating it here.

## Start / stop

```bash
./start.sh                                    # dashboard, foreground, auto-reload
python -m app.workers.cli run                 # discovery worker (optional, separate process)
python -m app.applications.worker run         # application worker (optional; requires APPLICATION_EXECUTOR_ENABLED=true)
```

Stop any of the above with Ctrl-C (or `kill <pid>` / `pkill -f "uvicorn app.main:app"` for
a backgrounded dashboard). Workers finish their current lease/attempt before exiting
(`*_SHUTDOWN_GRACE_SECONDS`) -- see `docs/worker-architecture.md`,
`docs/application-worker-architecture.md`.

## The one-click agent

The normal way to run this project day to day: start the dashboard as above, then click
**START AGENT** (or `POST /agent/start`). One background orchestrator then coordinates
discovery, resume optimization, application preparation, and execution on its own schedule
-- no need to separately launch `app.workers.cli run` / `app.applications.worker run` for
ordinary single-machine use. **STOP AGENT** (`POST /agent/stop`) finishes/releases current
safe work before stopping. `GET /agent/status` reports the full state (desired/actual,
last/next cycle, per-cycle counters). If the process restarts while the desired state was
`RUNNING`, it resumes automatically on the next startup -- no manual re-start needed. See
`docs/one-click-agent.md`.

The standalone worker processes above remain fully supported and safe to run alongside the
agent (or instead of it, for a real multi-machine deployment) -- both claim from the same
leased queues, so nothing double-processes.

## Workers

- Discovery fleet: `docs/distributed-workers.md`, `docs/fleet-operations.md`. Status via
  `/fleet` or `python -m app.workers.cli status`.
- Application fleet: `docs/application-worker-architecture.md`. Status via
  `/application-workers` or `python -m app.applications.cli status`.
- A crashed worker's abandoned lease recovers on its own once `lease_expires_at` passes --
  no manual intervention needed, no heartbeat-based "is it alive" check to run.
- To drain a worker before a deploy: the `/application-workers/{id}/drain` dashboard
  action (backed by `app.applications.worker_admin.request_drain`, application fleet); the
  discovery fleet's reaper (`app/workers/reaper.py`) marks stale workers `OFFLINE`
  automatically.

## Database

- Default: SQLite at `data/app.db`, zero setup.
- Shared/production: PostgreSQL via `DATABASE_URL` -- `docs/postgres-backend.md`,
  `docs/deployment-postgres.md`.
- Migrations run automatically on every process startup (`app.migrations.run_pending()`,
  idempotent) -- there is no separate manual migration step to remember.
- Backup/restore: `docs/backup-restore.md`.

## Health / readiness / metrics

- `GET /health` -- liveness only, use for a container orchestrator's liveness probe.
- `GET /readiness` -- DB reachable + schema compatible, use for a readiness probe.
- `GET /metrics` -- Prometheus text exposition (`METRICS_ENABLED=true`, default on).
- `GET /version` -- release/schema/optimizer/classifier version identifiers.

## Doctors

Run before trusting a deployment, and periodically thereafter:

```bash
python -m app.doctor                          # global -- aggregates every doctor below
python -m app.registry.cli doctor
python -m app.sponsorship.cli doctor
python -m app.applications.cli doctor
python -m app.resume_optimizer.cli doctor
python -c "from app.agent.doctor import run_doctor; r = run_doctor(); print(r.as_dict())"
```

All read-only; a nonzero exit means at least one SERIOUS issue. See each subsystem's own
doc for what its checks mean.

## Backups

See `docs/backup-restore.md`.

## Common failures

See `docs/troubleshooting.md` for the full list (Chromium missing shared libraries,
PostgreSQL unavailable, port already in use, provider CAPTCHA, Workday variability, schema
drift, stale resume, unknown submission status).

## Browser dependencies

Browser-assist and the real-browser test suite are optional (`playwright`, plus
`playwright install chromium`). See `docs/troubleshooting.md`'s non-root workaround for a
missing `libnspr4.so`/`libnss3.so`-style error.

## Provider degradation

- Discovery circuit breaker (`app.workers.circuit`) and submission circuit breaker
  (`app.applications.circuit`) are independent and both self-heal -- neither permanently
  disables a provider; both eventually retry via a `HALF_OPEN` probe after their cooldown.
- Provider health (`app.applications.provider_health`, real-browser ASSIST flow) never
  auto-disables anything either -- a `DEGRADED`/`CAPTCHA_BLOCKED`/`AUTH_GATED` reading only
  ever surfaces for operator review (`/applications/provider-health`).
- Dead-lettered portals/providers (persistent permanent failures) require an explicit
  `python -m app.workers.cli dead-letter --requeue <id>` -- never auto-requeued.

## User-action queue

Any job that stopped for a human (CAPTCHA, login/MFA, an unresolved legal/attestation
question, job-identity mismatch/unverified, a duplicate-application detection, a form
change) surfaces as `NEEDS_USER_ACTION` on the dashboard's main table and on
`/applications`, `/applications/browser-sessions`. Resolve from there; nothing in this
queue auto-resolves itself.
