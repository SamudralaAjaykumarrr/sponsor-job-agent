# Deployment: PostgreSQL + Multiple Workers (Example, Not Mandatory)

This describes an EXAMPLE production-shaped deployment (PostgreSQL +
dashboard + N workers), for local demonstration/testing. It is not
infrastructure this build deploys anywhere, and SQLite local mode
(`./start.sh`) continues to work exactly as before -- nothing here is
required.

## Docker image (`Dockerfile`)

One image, two roles via command override:

```
docker build -t sponsor-job-agent .
# dashboard
docker run -p 8000:8000 -e DATABASE_URL=postgresql://... sponsor-job-agent
# worker
docker run -e DATABASE_URL=postgresql://... sponsor-job-agent \
    python -m app.workers.cli run
```

- Non-root runtime (`useradd --uid 10001 sponsoragent`, `USER sponsoragent`).
- Small base (`python:3.12-slim`); no compiler toolchain needed
  (`psycopg[binary]` ships prebuilt wheels).
- `HEALTHCHECK` hits `/health` (liveness, never touches the DB -- see
  `docs/production-observability.md`).
- `.dockerignore` excludes `.env`, `data/`, `output/`, `candidate_data/`,
  `.git`, tests, docs, scripts -- no candidate data, database files, or
  generated resumes are ever baked into the image. Configuration
  (including `DATABASE_URL`) is supplied entirely via environment
  variables at `docker run`/compose time.

## Multi-service local demo (`deploy/docker-compose.postgres.yml`)

PostgreSQL + dashboard + 2 sharded discovery workers + 1 application worker
(Phase 9 addition), all sharing one `DATABASE_URL`:

```
cd deploy
POSTGRES_PASSWORD=<pick-one-yourself> docker compose -f docker-compose.postgres.yml up --build
```

- No hardcoded production secrets: `POSTGRES_PASSWORD` is required via
  `${POSTGRES_PASSWORD:?...}` -- compose refuses to start with no value
  rather than defaulting to something guessable.
- `worker-1`/`worker-2` are sharded (`REGISTRY_SHARD_COUNT=2`,
  `REGISTRY_SHARD_INDEX=0`/`1`) so they demonstrate partitioned, non-
  overlapping polling rather than two workers doing redundant work.
- `application-worker-1` runs `python -m app.applications.worker run` with
  `APPLICATION_EXECUTOR_ENABLED=true` (so it can claim/prepare queued
  applications) but `AUTO_SUBMIT_ENABLED` deliberately left unset (`false`)
  -- this example never submits anything anywhere. Set
  `AUTO_SUBMIT_ENABLED=true` yourself only after you've reviewed CLAUDE.md's
  safety rules and genuinely want that behavior.
- `STRUCTURED_LOGGING_ENABLED=true` on the workers, so `docker compose logs`
  shows JSON lines with correlation ids.

### Honest limitation

**Docker/Docker Compose was unavailable in this build's environment** (both
Phase 6 and this phase, confirmed again: `docker` resolves to a path under
the Windows host's Docker Desktop install, but the daemon is not reachable
from this WSL 2 distro -- "Docker Desktop WSL integration" is not enabled
here). The Dockerfile and compose file (including the new
`application-worker-1` service) are written and YAML-validated
(`python -c "import yaml; yaml.safe_load(...)"` succeeded), following the
project's existing conventions, but **this specific multi-service demo was
not built or run**. The distributed-coordination *logic* it would exercise
(leasing, circuit breaker, rate limiting, orphan reaping, crash recovery)
was validated directly against a real PostgreSQL server instead (via
`pgserver`, which needs no Docker/root) -- see `docs/distributed-workers.md`,
`docs/application-worker-architecture.md`,
`tests/test_applications_postgres_phase9.py`, and
`scripts/multi_machine_simulation.py`. If Docker is available in your
environment, running the compose file above is the natural next validation
step; it was not skipped by choice, only by environment constraint.

## Environment variables

See `.env.example`'s Phase 6 section: `DATABASE_URL`,
`ORPHAN_WORKER_STALE_SECONDS`, `POSTGRES_STATEMENT_TIMEOUT_MS`,
`METRICS_ENABLED`, `SCHEMA_DRIFT_CIRCUIT_TENANT_THRESHOLD`,
`SCHEMA_DRIFT_WINDOW_HOURS`, `STRUCTURED_LOGGING_ENABLED`.

## Startup UX

`./start.sh` prints a summary before launching (database backend, queue
backend, agent enabled, worker mode, dashboard URL) -- never prints
secrets (a `DATABASE_URL` password is never echoed).
