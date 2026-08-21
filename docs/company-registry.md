# Company / ATS Tenant Registry

`app/registry/` is the foundation for Phase 4's mass company/portal importer
(10,000–100,000+ rows). It is SQLite-backed (`company_registry` table, see `app/db.py`)
specifically so that scaling up is a data-loading problem, not a code-architecture problem.

## Schema (`app/registry/models.py::CompanyRegistryEntry`)

| Field | Purpose |
|---|---|
| `company_name` | Display name. |
| `company_domain` | Optional company website domain, for future cross-referencing. |
| `provider` | One of the names in `app/providers/registry.py::all_provider_names()`. |
| `tenant_identifier` | Provider-specific identifier — Greenhouse board token, Lever/Ashby/Recruitee/Breezy/BambooHR/Workable/Teamtailor/Pinpoint subdomain-or-slug, SmartRecruiters company identifier, Comeet `"company:token"`, or a Workday tenant's full CXS base URL. |
| `careers_url` | The human-facing careers page, for reference/verification. |
| `country` | Optional. |
| `enabled` | Disabling a tenant removes it from `list_due_for_poll()` immediately — no delete needed. |
| `verified_at` / `last_success_at` / `last_failure_at` / `last_error` | Observability. |
| `consecutive_failures` | Drives both backoff and the health status (see below). |
| `support_level` | Copied from the provider's capability at insert time — informational; the provider's own `ProviderCapabilities` is still the source of truth. |
| `last_polled_at` / `next_poll_at` / `poll_interval_minutes` | Adaptive scheduling state. |
| `average_job_yield` / `average_latency_ms` | Exponential moving averages updated after every poll. |

## Adaptive polling (`app/registry/scheduling.py`)

Deterministic rules, not ML, per CLAUDE.md's "don't build an overly complicated scheduler":

- **Success + new jobs found** → speed up: `interval *= 0.75`.
- **Success + zero new jobs** → slow down slightly: `interval *= 1.25`.
- **Failure** → back off from wherever the interval currently sits: `interval *= 2`.

Every interval is clamped to `[PROVIDER_MIN_POLL_MINUTES, PROVIDER_MAX_POLL_MINUTES]`
(`.env.example`), so a tenant is never polled unboundedly fast nor effectively abandoned.

## Health (`compute_health`)

| Consecutive failures | Health |
|---|---|
| 0–2, with at least one prior success | `HEALTHY` |
| 3–9, or never yet successfully polled | `DEGRADED` |
| ≥10 | `FAILING` |

Health is computed on read (not stored) so the threshold constants can change without a
migration. The dashboard's `/providers` page aggregates this per-provider
(`provider_health_summary()`); `/registry` shows it per-tenant.

## How the discovery cycle uses the registry

`app/agent/cycle.py::run_discovery_cycle()` runs two phases every cycle:

1. **Legacy/static** (`_discover_from_static_config`) — the Phase 2 path,
   `ENABLED_PROVIDERS` + `*_BOARD_TOKENS`/`*_COMPANY_SLUGS` env vars. Unchanged.
2. **Registry-driven** (`_discover_from_registry`) — `list_due_for_poll()` returns every
   enabled tenant whose `next_poll_at` has passed (or was never set), builds a
   single-tenant provider instance via `build_provider_for_tenant`, fetches (bounded by
   `MAX_JOBS_PER_PROVIDER`/`MAX_PAGES_PER_PROVIDER`), processes each posting through the
   same dedup/analyze/generate path as phase 1, then calls `mark_poll_result()` to update
   the adaptive schedule and writes one `discovery_log` row per tenant per cycle
   (`cycle_id, provider, company, tenant, started_at, finished_at, latency_ms,
   jobs_received, jobs_new, jobs_duplicate, jobs_filtered, error_type`).

A tenant that raises is caught, logged into the cycle's `errors` list, marked as a failed
poll (backs off, `consecutive_failures += 1`), and does **not** stop any other tenant —
including other tenants of the same provider — from being processed in the same cycle.

## Populating the registry

- **Dashboard**: `/registry` has an "Add a company/tenant" form (`POST /registry/add`).
- **Programmatically**: `app.registry.repo.insert_entry(CompanyRegistryEntry(...))`.
- **Demo seed** (off by default): set `REGISTRY_SEED_DEMO_DATA=true` to insert two
  illustrative public entries (GitLab/Greenhouse, Lever's own demo account) on first
  startup, only if the table is currently empty. Never auto-populates in a normal run.

Phase 4's bulk importer should call `insert_entry`/`update_entry` in a loop (or a bulk
`INSERT` against the same schema) — no code changes to the discovery cycle, scheduler, or
dashboard are required to go from a handful of rows to 100,000+.

## Phase 4 update: this table is now populated via the acquisition/verification layer

This document describes the table exactly as Phase 3 left it — **unchanged**. Phase 4 added a
richer acquisition/verification/lifecycle layer on top (`app/registry/store.py` +
`registry_companies`/`registry_portals`/`registry_provenance`, see
`docs/registry-import.md`/`registry-verification.md`), and the only new thing that touches *this*
table is `app/registry/sync.py::sync_portal_to_operational_registry`, which upserts a row here
(by the same `(provider, tenant_identifier)` unique index) once a Phase 4 portal reaches
`VERIFIED`/`ACTIVE`, and disables (never deletes) the row if the portal later regresses. The
manual `/registry/add` form and direct `insert_entry` calls documented above still work exactly
as before — nothing here was rebuilt.

In practice, populating the registry at scale now means: `python -m app.registry.cli import
companies.csv` → `python -m app.registry.cli verify` (or the dashboard's per-portal Verify
button) → the verified portals appear here automatically, ready for `app/agent/cycle.py` to poll
on its next due cycle.

**Phase 5 update**: rows in this table are now also polled by the distributed worker fleet
(`python -m app.workers.cli run`), not only by the legacy in-process scheduler — both read the
exact same `next_poll_at`/`mark_poll_result` scheduling this document describes, unchanged. What's
new is *how* a due row gets claimed: an atomic, sharded lease (four additive columns:
`lease_owner`/`lease_attempt_id`/`lease_acquired_at`/`lease_expires_at`) guarantees at most one
worker process ever polls a given row at a time, even with several worker processes running.
See `docs/polling-leases.md` and `docs/worker-architecture.md`. The single-import-then-verify
workflow above can now also be driven end-to-end as one resumable batch via `python -m
app.registry.cli acquire companies.csv` — see `docs/registry-acquisition.md`.
