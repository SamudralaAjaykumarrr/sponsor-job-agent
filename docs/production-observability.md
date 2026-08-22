# Production Observability

## Metrics (`app/observability/metrics.py`)

Every metric is a **live query at request time** -- nothing is accumulated
in-process (which would be wrong the moment more than one worker/dashboard
process exists) and nothing is estimated or extrapolated.

`collect()` returns a single dict, the source of truth for both
`/fleet/metrics` (JSON, unchanged Phase 5 shape) and `/metrics` (Prometheus
text, new):

```
database_backend, workers_online, workers_offline,
leases_active_poll, leases_active_verification,
attempts_total_1h, attempts_failed_1h,
jobs_fetched_total_1h, jobs_new_total_1h,
portals_active, portals_polled_1h, portals_polled_24h,
monitoring_coverage_24h,
poll_queue_depth, verification_queue_depth, retry_depth,
dead_letter_count, schema_drift_events_total,
provider_circuit_state{provider=...}, provider_failures_1h{provider=...},
provider_rate_limits_1h{provider=...},
discovery_latency_p50/p90/p95/p99_minutes, discovery_latency_sample_size
```

### Why not `prometheus_client`

Deliberately not added as a dependency. Every metric here is a fresh gauge
queried from the DB on each scrape -- `prometheus_client`'s core value
(managing in-process counters/histograms accumulated *between* scrapes)
doesn't fit this model, since there's no in-process state to manage. A
small, dependency-free text-format renderer
(`render_prometheus_text()`) is simpler and just as correct for gauges. If
a future phase adds genuine in-process counters (e.g. per-request timing
histograms), reconsider then.

### `/metrics` endpoint

Prometheus text exposition format (`# TYPE ... gauge` + `metric value`
lines), `GET /metrics`, gated by `METRICS_ENABLED` (default true; 404 when
disabled). Never exposes candidate PII -- every metric is
fleet/provider/queue shaped, never job or candidate content.

## Health vs. readiness

- `GET /health`: liveness only. **Never touches the database** -- a health
  check that depends on the DB can't distinguish "this process is stuck"
  from "the shared database is briefly slow". Always returns `{"status":
  "ok"}` if the process can respond at all.
- `GET /readiness` (`app/health.py::check_readiness`): database reachable +
  schema compatible. In Postgres mode this genuinely returns `ready: false`
  (HTTP 503) when the shared database is unreachable -- confirmed live: a
  syntactically valid but unreachable Postgres URL (`127.0.0.1:1`, nothing
  listening) produces a clean, fast `ready: false` response, never a hang
  or a leaked credential in the response body (only exception *type name*
  is reported on connection failure, never the raw driver message, which
  can sometimes embed a DSN).

## Structured logging (`app/observability/logging_config.py`)

Opt-in (`STRUCTURED_LOGGING_ENABLED=true`, off by default for a readable
local terminal). Emits one JSON object per log line: `timestamp`, `level`,
`component` (logger name), `message`, plus an **allowlist** of correlation
fields a call site may attach via `extra={...}`: `worker_id`, `attempt_id`,
`correlation_id`, `portal_id`, `portal_type`, `provider`, `tenant`,
`duration_ms`, `event`, `error_type`. Anything outside the allowlist is
silently dropped -- a deliberate choice so a future accidental
`extra={"candidate_email": ...}` can never start appearing in logs. Hard
rule (structural, not just discipline): never log candidate email/phone/
resume contents/secrets/DB passwords -- the allowlist's field names were
checked (`tests/test_phase6_structured_logging_and_correlation.py`) to
contain no PII-shaped names.

## Correlation IDs

One correlation id ties a polling attempt → provider request → job
normalization → pipeline together: `poll_attempts.attempt_id` itself (no
second, parallel id scheme is needed -- it already uniquely identifies one
worker's one attempt at one portal). `app.workers.runner` passes
`correlation_id=item.attempt_id` into `process_raw_job()`, which stamps it
onto the resulting `jobs.correlation_id` column (new, additive, defaults to
`""` for jobs ingested outside the worker fleet, e.g. a manual JD paste).
`workers.runner` also logs `poll_succeeded`/`poll_failed` events with this
same correlation id attached (see structured logging above), so a real
production log aggregator can trace one attempt end to end.

## Fleet dashboard (`/fleet`)

Added: database backend, schema version (current/expected), queue backend
description, worker software version, per-provider circuit state table (+
force-probe/close admin actions), schema drift table, orphan-reaper button,
per-worker "mark offline" action.

## Admin safety actions (all POST, all explicit, none destructive)

- `POST /fleet/circuit/{provider}/force-probe` -- bypass the cooldown timer,
  transition OPEN → HALF_OPEN immediately.
- `POST /fleet/circuit/{provider}/close` -- reset to CLOSED after a
  validated manual recovery check.
- `POST /fleet/workers/{worker_id}/mark-offline` -- explicit override when a
  worker is known dead before the heartbeat threshold would catch it.
- `POST /fleet/reap-orphans` -- run the orphan reaper on demand.
- `POST /fleet/dead-letter/{id}/requeue` (Phase 5, unchanged).

No "delete all"/purge/wipe controls exist anywhere in the app (verified by
`tests/test_phase6_observability_endpoints.py::test_no_destructive_delete_all_admin_route_exists`).
