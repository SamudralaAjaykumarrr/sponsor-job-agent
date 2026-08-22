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

PostgreSQL + dashboard + 2 sharded workers, all sharing one
`DATABASE_URL`:

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
- `STRUCTURED_LOGGING_ENABLED=true` on the workers, so `docker compose logs`
  shows JSON lines with correlation ids.

### Honest limitation

**Docker/Docker Compose was unavailable in this build's environment**
(confirmed: the `docker` binary is not present in this WSL 2 distro, and
the host's Docker Desktop WSL integration was not enabled here). The
Dockerfile and compose file are written and YAML-validated
(`python -c "import yaml; yaml.safe_load(...)"` succeeded), following the
project's existing conventions, but **this specific multi-service demo was
not built or run** in this build. The distributed-coordination *logic* it
would exercise (leasing, circuit breaker, rate limiting, orphan reaping)
was validated directly against a real PostgreSQL server instead (via
`pgserver`, which needs no Docker/root) -- see `docs/distributed-workers.md`
and `scripts/multi_machine_simulation.py`. If Docker is available in your
environment, running the compose file above is the natural next
validation step; it was not skipped by choice, only by environment
constraint.

## Environment variables

See `.env.example`'s Phase 6 section: `DATABASE_URL`,
`ORPHAN_WORKER_STALE_SECONDS`, `POSTGRES_STATEMENT_TIMEOUT_MS`,
`METRICS_ENABLED`, `SCHEMA_DRIFT_CIRCUIT_TENANT_THRESHOLD`,
`SCHEMA_DRIFT_WINDOW_HOURS`, `STRUCTURED_LOGGING_ENABLED`.

## Startup UX

`./start.sh` prints a summary before launching (database backend, queue
backend, agent enabled, worker mode, dashboard URL) -- never prints
secrets (a `DATABASE_URL` password is never echoed).
