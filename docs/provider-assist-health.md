# Provider Assist Health (Phase 13)

CLAUDE.md Phase 13 sections 11-12, 15-16, 54, 57.

`app.applications.provider_health` tracks whether a provider's real-browser
**ASSIST** flow (form discovery/fill) is currently trustworthy — a third,
deliberately separate concern from:

- `app.workers.circuit` — discovery-poll circuit breaker
- `app.applications.circuit` — application-**submission** circuit breaker

None of these three gate each other. A provider can have a healthy
discovery circuit, a closed submission circuit, and still be
`CAPTCHA_BLOCKED` here.

## States

`ProviderAssistHealth`: `HEALTHY`, `DEGRADED`, `VARIABLE`, `STALE`,
`CAPTCHA_BLOCKED`, `AUTH_GATED`, `SCHEMA_DRIFT`, `UNVERIFIED`,
`UNSUPPORTED`.

`compute_health(row)` is a pure, deterministic function over the stored
row — never cached, always recomputed live on every read (dashboard, API,
doctor, metrics all call the same function). Order of evaluation:

1. `captcha_observed` / `auth_gate_observed` not yet cleared by a later
   success → `CAPTCHA_BLOCKED` / `AUTH_GATED`.
2. No recent live validation (`last_live_validation` or `last_success`
   older than `CAPABILITY_EVIDENCE_MAX_AGE_DAYS`, or never observed at all)
   → `STALE` / `UNVERIFIED`.
3. `schema_drift_count >= 2` → `SCHEMA_DRIFT`.
4. `consecutive_failures >= 3` → `DEGRADED`.
5. Otherwise, a genuine recent success with `form_verified=1` → `HEALTHY`.

Recording evidence **never** auto-disables anything — a `DEGRADED`/
`STALE`/`SCHEMA_DRIFT`/`CAPTCHA_BLOCKED`/`AUTH_GATED` row only ever
surfaces for review (doctor, dashboard, metrics). This is the same
"never auto-disable a known-safe capability" principle Phase 11's
`capability_evidence` module established.

## Keying

`(provider, tenant, site)` — tenant/site default to `""` for providers
with no tenant concept (everything except Workday today). Rows are never
collapsed into one blanket per-provider claim; `list_health()` always
returns per-row entries.

## Wiring into the real browser runtime

`app.applications.browser_runtime._do_discover()` calls:

- `record_failure(provider, FailureKind.CAPTCHA, ...)` when a CAPTCHA
  element is observed.
- `record_failure(provider, FailureKind.AUTH_GATE, ...)` when a login wall
  is observed.
- `record_success(provider, ..., form_fingerprint=...)` whenever fields are
  genuinely discovered with no pause.

`clear_captcha_flag()` is available for an operator/reconciliation flow to
explicitly clear a stale flag after confirming the provider is reachable
again — never cleared automatically by mere timeout.

## Doctor coverage

`provider_healthy_from_stale_evidence` — a provider that was previously
genuinely form-verified (`form_verified=1`) but whose evidence has since
gone `STALE` is flagged for revalidation; application assist should
require review until revalidated rather than continuing to be silently
trusted.

## Dashboard / API

- `GET /applications/provider-health` — HTML table, one row per
  (provider, tenant, site).
- `GET /api/applications/provider-health` — JSON.
- `python -m app.applications.cli provider-health` — CLI report.

## Metrics

`app.applications.metrics.collect_phase13()` exposes
`provider_assist_health` (a `{provider/tenant/site: health}` map, never
collapsed), `provider_schema_drift_total`, `provider_capability_stale`,
`captcha_handoffs_total`, `login_handoffs_total`.
