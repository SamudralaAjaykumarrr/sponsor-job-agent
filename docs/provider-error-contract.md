# Provider Error Contract

## The gap this closes

Every provider connector's per-tenant fetch helper (e.g.
`GreenhouseProvider._fetch_board`) catches its own HTTP/parsing errors,
logs, and returns an empty list -- correct for the multi-tenant
static-config discovery path (one bad board must never abort the others),
but it meant the caller could never tell "this board legitimately has zero
jobs right now" apart from "this board's fetch just failed". A real
instance of this gap was caught live during Phase 5's own validation
(`ResponseTooLargeError` on an unusually large real board went unrecorded
until an outer safety net in `app/workers/runner.py` caught it as a last
resort) -- and again, differently, during this phase's own real 2-worker
live validation against real Postgres + real providers (see
`docs/phase6-production-scale.md`'s "real bugs" list).

## The fix: `ProviderFetchResult`, not a control-flow rewrite

Rewriting every connector's control flow to raise instead of swallow would
be a large, regression-prone diff across 11+ already-tested provider files.
Instead (`app/providers/errors.py`):

- `fetch_jobs()` is **completely unchanged** -- same signature, same
  swallow-and-return-`[]` behavior, all pre-Phase-6 tests pass unmodified.
- Each connector's existing `except` blocks around its per-tenant fetch gain
  **one additional line**: `self._last_error = exc` (a class-level default
  `None` on `JobProvider`, so no `__init__` changes needed anywhere).
- `JobProvider.fetch_jobs_result(max_jobs, tenant=...)` (new, in the base
  class) calls `fetch_jobs()`, then classifies the outcome:
  - jobs came back → `SUCCESS_WITH_JOBS` (even if an earlier tenant's
    fetch inside a multi-tenant call also hit a transient error --
    unambiguous success).
  - empty list, no stashed error → `SUCCESS_EMPTY` (a genuinely empty
    board is healthy, never a failure).
  - empty list, a stashed error → classified into one of the typed
    statuses below via `classify_exception()`.
  - `capabilities.discovery_supported is False` → `UNSUPPORTED`,
    short-circuited before even attempting a request.

For the Phase 4/5 per-tenant registry path
(`app.providers.registry.build_provider_for_tenant` always constructs a
single-tenant instance), this is exactly precise: one tenant, one outcome,
unambiguous.

## `ProviderFetchStatus`

```
SUCCESS_WITH_JOBS   SUCCESS_EMPTY
TIMEOUT             RATE_LIMITED           TEMPORARY_HTTP_FAILURE
PERMANENT_NOT_FOUND INVALID_TENANT         SCHEMA_DRIFT
RESPONSE_TOO_LARGE  UNSUPPORTED            MALFORMED_RESPONSE
UNKNOWN_FAILURE
```

`classify_exception()` maps:
- `ResponseTooLargeError` → `RESPONSE_TOO_LARGE`, retryable.
- `ProviderHTTPError` with `HTTP 401/403` → `INVALID_TENANT`, not retryable.
- `HTTP 400/404/410` → `PERMANENT_NOT_FOUND`, not retryable.
- `HTTP 429` → `RATE_LIMITED`, retryable.
- `HTTP 5xx` → `TEMPORARY_HTTP_FAILURE`, retryable.
- `httpx.TimeoutException` → `TIMEOUT`, retryable.
- `httpx.TransportError` → `TEMPORARY_HTTP_FAILURE`, retryable.
- `json.JSONDecodeError`/`ValueError`/`KeyError`/`TypeError` →
  `MALFORMED_RESPONSE`, retryable (ambiguous shape errors are always
  treated as retryable, matching `app.workers.retry.classify_exception`'s
  existing conservative philosophy).
- anything else → `UNKNOWN_FAILURE`, retryable.

`ProviderFetchResult` fields: `status`, `jobs`, `provider`, `tenant`,
`started_at`, `finished_at`, `latency_ms`, `http_status`, `retry_after`,
`error_type`, `error_message_safe` (truncated, never includes secrets),
`retryable`, `schema_fingerprint`.

## Wired into the worker runner

`app.workers.runner.Worker._execute_poll()` now calls
`provider_obj.fetch_jobs_result(...)` instead of `fetch_jobs()` directly.
On a real failure, `_handle_poll_failure()` (shared with the structural-
probe failure path, refactored out of the old `_handle_poll_probe_failure`)
feeds the circuit breaker, applies dead-letter/backoff bookkeeping, and
records the real `error_type`/`retryable` in attempt history -- all things
that were previously invisible for a post-probe fetch failure.
`tests/test_phase6_runner_provider_error_gap.py` reproduces the exact
scenario (probe succeeds, real fetch oversized) and proves the failure is
now recorded, not silenced.

## Per-provider audit

All 9 connectors with a real discovery implementation (Greenhouse, Lever,
Ashby, Workable, SmartRecruiters, BambooHR, Breezy, Recruitee, Comeet) plus
Workday (PARTIAL) got the one-line `self._last_error = exc` addition in
their top-level per-tenant fetch `except` blocks (not their per-job-detail
or per-job-normalize `except` blocks, which are separate, already-isolated
concerns). The 6 UNSUPPORTED connectors (`app/providers/unsupported.py`)
never attempt a request at all -- `fetch_jobs_result()` returns
`UNSUPPORTED` for them via the `discovery_supported` check, without
touching their code.

No provider's declared `ProviderCapabilities.support_level` changed as
part of this work -- this phase makes error *reporting* honest, it does not
newly implement discovery for any UNSUPPORTED provider.

## Response size / memory safety

Unchanged mechanism (`app.providers.http_client.ResponseTooLargeError`,
`PROVIDER_MAX_RESPONSE_BYTES`), now properly classified and recorded as a
real failure (see above) rather than silently absorbed. Confirmed live: a
real Greenhouse board (a real company in the registry) returned a >5MB/9MB
response during this phase's own live validation and was correctly
classified `RESPONSE_TOO_LARGE`, recorded, and the worker continued
without crashing or stranding the lease.

## Schema drift

Distinct table now (`provider_schema_drift`, see
`docs/production-observability.md`) tracks drift persistently
(`first_seen_at`/`last_seen_at`/`occurrence_count` per
`provider`+`tenant`+structural-signature, never raw payloads). Drift
affecting `SCHEMA_DRIFT_CIRCUIT_TENANT_THRESHOLD` (default 3) or more
DISTINCT tenants of the same provider within `SCHEMA_DRIFT_WINDOW_HOURS`
feeds the existing circuit breaker as a failure signal -- one oddball
tenant's drift never trips it alone.
