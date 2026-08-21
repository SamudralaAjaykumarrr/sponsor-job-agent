# Phase 3 — ATS/Job-Source Coverage Expansion

Phase 3 is discovery/ingestion infrastructure: it extends Phase 2's provider architecture
from 2 ATSes (Greenhouse, Lever) to a uniform model covering 16, adds a capability
classification system, a SQLite-backed company/tenant registry (the Phase 4 mass-importer
foundation), a provider-detector, hardened HTTP behavior, cross-provider dedup with
provenance, and adaptive per-tenant polling. It does **not** implement broad scraping,
anti-bot bypass, or auto-submission — see Security below.

See also: `docs/provider-capabilities.md`, `docs/provider-development.md`,
`docs/company-registry.md`, `docs/architecture.md`, `docs/autonomous-agent.md`.

## What was built

- **Capability model** (`app/providers/capabilities.py`) — every provider declares
  `ProviderCapabilities` (support level + exactly which fields are structured vs.
  unavailable). Exposed programmatically (`app/providers/registry.py::all_capabilities`)
  and on the dashboard (`/providers`).
- **Normalized job model** (`app/providers/base.py::RawJobPosting`) — extended with
  `company_identifier, city, state, country, remote_status, source_url, salary_currency,
  salary_period, department, team, office, provider_metadata`, all optional/never
  fabricated. Mirrored onto the `jobs` table via additive columns.
- **11 new connectors**: Ashby, Workable, SmartRecruiters (the three called out explicitly
  in the spec, all FULL), plus BambooHR (PARTIAL), Recruitee, Breezy HR (FULL), Comeet
  (EXPERIMENTAL), Workday (PARTIAL), and Teamtailor/Jobvite/Pinpoint/JazzHR/iCIMS/Oracle
  (UNSUPPORTED — detection + registry only, no fabricated discovery). See the full matrix
  below.
- **HTTP hardening** (`app/providers/http_client.py`) — every connector routes through
  `build_client`/`get_json`/`post_json`: bounded connect/read timeouts, bounded retries
  with exponential backoff (transient 429/5xx/network errors only, `Retry-After` respected),
  a response-size cap, and a shared descriptive `PROVIDER_USER_AGENT`.
- **Pagination safety** — every paginated connector (Workable, SmartRecruiters, Workday)
  stops on an empty page or a page that re-serves already-seen IDs, and is bounded by
  `MAX_PAGES_PER_PROVIDER`/`MAX_JOBS_PER_PROVIDER`.
- **Bounded concurrency** (`app/providers/concurrency.py`) — multi-tenant providers
  (Greenhouse/Lever/Ashby with several boards configured) fan out with a hard concurrency
  cap (`PROVIDER_CONCURRENCY_LIMIT`), never one request per tenant unbounded.
- **Provider detector** (`app/providers/detector.py`) — given a URL, returns
  `(provider, confidence, tenant_identifier, evidence)`. Never reports high confidence
  without a deterministically-extractable tenant.
- **Company/tenant registry** (`app/registry/`) — SQLite-backed `CompanyRegistryEntry`
  model + repo, designed for Phase 4's 10k–100k+ row importer. See
  `docs/company-registry.md` for the adaptive-polling rules and health model.
- **Cross-provider dedup + provenance** (`app/discovery/dedup.py::canonicalize_url`,
  `app/jobs_repo.py::record_provenance`/`list_provenance`) — dedup order is now stable
  provider ID → canonical URL → (only when no URL exists at all) a company/title/location
  fingerprint. Every source a job was seen from is retained in `job_provenance`, even when
  it dedupes into one job row.
- **Adaptive scheduling** (`app/registry/scheduling.py`) — deterministic, not ML: speed up
  on new-job yield, slow down on empty yield, back off on failure, all clamped to
  `[PROVIDER_MIN_POLL_MINUTES, PROVIDER_MAX_POLL_MINUTES]`.
- **Observability** — `discovery_log` table (per-tenant-per-cycle: latency, jobs
  received/new/duplicate/filtered, error type) and `discovery_cycles` now allocated at
  cycle start (`start_discovery_cycle`/`finalize_discovery_cycle`) so per-tenant rows can
  reference the in-progress cycle_id.
- **Dashboard** — `/providers` (capability matrix + live tenant health per provider) and
  `/registry` (per-tenant health, add-entry form, provider filter), linked from the main
  dashboard nav. `/discovery-log` JSON endpoint.
- **Freshness source** — `FreshnessSource.PUBLISHED_AT` vs `FIRST_SEEN`, stored per job;
  a provider without a real timestamp (Workday's relative `postedOn`) always resolves to
  `FIRST_SEEN` rather than fabricating an absolute date.

## Provider matrix

| Provider | Support | Discovery | Live tested this session | Limitations |
|---|---|---|---|---|
| Greenhouse | FULL | Yes | ✅ `gitlab` — 5/5 normalized | None material. |
| Lever | FULL | Yes | ✅ `leverdemo` — 5/5 normalized | None material. |
| Ashby | FULL | Yes | ✅ `ashby` (dogfooded) — 5/5 normalized | None material. |
| Workable | FULL | Yes | ⚠️ not verified — no known-valid public tenant found this session; fixture tests pass | List+detail both required; unverified live tenant only, connector code untested against a real account this session. |
| SmartRecruiters | FULL | Yes | ✅ `SmartRecruiters` (dogfooded) — 5/5 normalized | None material. |
| Recruitee | FULL | Yes | ⚠️ not verified — guessed subdomains 404'd; fixture tests pass | Same caveat as Workable. |
| Breezy HR | FULL | Yes | ✅ `breezy` (dogfooded) — 3/3 normalized | None material. |
| BambooHR | PARTIAL | Yes | ⚠️ not verified — guessed subdomain redirected to marketing site; fixture tests pass | No public JD detail endpoint even when a tenant is found — description always empty. |
| Comeet | EXPERIMENTAL | Yes (config-gated) | Not attempted — requires a real company+embed-token pair not available this session | Unverified response schema; requires manually-sourced public embed token. |
| Workday | PARTIAL | Yes | ✅ `workday.wd5.myworkdayjobs.com/Workday` (dogfooded) — 3/3 normalized, `postedOn` correctly left unparsed | Hosting number + site name are tenant-specific and must be configured exactly; some tenants front this with bot protection this app won't bypass. |
| Teamtailor | UNSUPPORTED | No | N/A (detection-only) | No verified public unauthenticated discovery endpoint. |
| Jobvite | UNSUPPORTED | No | N/A | Same. |
| Pinpoint | UNSUPPORTED | No | N/A | Same. |
| JazzHR | UNSUPPORTED | No | N/A | Public API requires a key. |
| iCIMS | UNSUPPORTED | No | N/A | Tenant-specific session/CSRF requirements. |
| Oracle Recruiting Cloud | UNSUPPORTED | No | N/A | Tenant-specific, non-guessable parameters. |

"Dogfooded" = the ATS vendor's own careers page runs on their own product, so it is a
real, currently-live public tenant, not a synthetic fixture.

## Testing

- **205 tests passing** (87 original Phase 2 tests, unmodified in behavior, + 118 new
  Phase 3 tests), zero live-network dependency in the suite (`httpx.MockTransport`
  fixtures throughout).
- New coverage: every implemented connector's normalization + per-tenant error isolation
  + malformed-payload handling + (where paginated) pagination termination; the detector's
  URL patterns; the capability matrix; the HTTP client's retry/backoff/size-cap/timeout
  behavior; the registry's CRUD + adaptive scheduling + health computation + migration
  safety; cross-provider dedup and URL canonicization (including the "don't wrongly merge
  two different requisitions" regression this test suite caught and fixed — see
  `app/agent/cycle.py::_process_raw_job`); freshness-source fallback; a failing-tenant
  isolation scenario; a many-tenant scheduling scenario; and full acceptance scenarios A/B/C/G
  driving a fixture straight through discovery → dedup → sponsorship classification →
  resume generation.

## Migration verification

`init_db()` is fully additive: new tables (`company_registry`, `job_provenance`,
`discovery_log`) created with `CREATE TABLE IF NOT EXISTS`; new `jobs` columns added via
`ALTER TABLE ... ADD COLUMN` guarded by `PRAGMA table_info`. `tests/test_registry.py::
test_migration_preserves_existing_jobs_and_state` inserts a job + a discovery cycle, runs
`init_db()` twice more, and asserts both rows are byte-for-byte unchanged.

## Provider health behavior

Computed on read from `consecutive_failures`/`last_success_at`
(`app/registry/scheduling.py::compute_health`): HEALTHY (0–2 failures, at least one prior
success) / DEGRADED (3–9, or never yet succeeded) / FAILING (≥10). A failing tenant backs
off (interval doubles, bounded by `PROVIDER_MAX_POLL_MINUTES`) and is simply skipped on
future cycles until due again — it never blocks other tenants, including other tenants of
the same provider, in the same cycle (`tests/test_discovery_registry_cycle.py::
test_scenario_e_failing_tenant_marked_degraded_others_still_process`).

## Dedup / provenance behavior

Order: stable `(provider, external_job_id)` → canonical URL (tracking params stripped,
host lowercased, trailing slash removed, job-identifying params like `gh_jid` preserved)
→ company/title/location fingerprint **only when no URL exists at all**. The last rule
change is deliberate and was driven by a failing test: an earlier version fell back to the
fingerprint even when a canonical URL was present but didn't match anything, which wrongly
merged two distinct real requisitions that happened to share title/company/location text.
Every source a job was seen from is retained in `job_provenance` (provider, source_url,
provider_job_id, discovery_cycle_id, first/last_seen_at) even after dedup collapses it into
one job row.

## Dashboard changes

`/providers` (capability matrix + aggregated tenant health per provider), `/registry`
(per-tenant table + add-entry form + provider filter), `/discovery-log` (JSON), and a
"Source provenance" section on the job detail page. Nav links added to the main dashboard.

## Live smoke-test results

Executed against real public endpoints with `max_jobs=5`, no login, no anti-bot bypass,
low request volume (see method above). Failures never fail the automated test suite —
they're reported here as observed, separately from the fixture-based unit tests.

```
greenhouse       tenant=gitlab                         HTTP=OK           fetched=5  normalized=5
lever            tenant=leverdemo                      HTTP=OK           fetched=5  normalized=5
ashby            tenant=ashby                           HTTP=OK           fetched=5  normalized=5
smartrecruiters  tenant=SmartRecruiters                 HTTP=OK           fetched=5  normalized=5
breezy           tenant=breezy                          HTTP=OK           fetched=3  normalized=3
workday          tenant=workday.wd5.myworkdayjobs.com   HTTP=OK           fetched=3  normalized=3
recruitee        tenant=recruitee (unverified)          HTTP=OK(0 jobs)   fetched=0  normalized=0
workable         tenant=workable (unverified)           HTTP=OK(0 jobs)   fetched=0  normalized=0
bamboohr         tenant=bamboohr (unverified)           HTTP=OK(0 jobs)   fetched=0  normalized=0
```

6 of 9 attempted providers confirmed working end-to-end against real, currently-live
tenants. The other 3 (Recruitee, Workable, BambooHR) have passing fixture tests and correct
clean-failure behavior (no crash, no fabricated jobs, isolated per-tenant) but no verified
real tenant was found in this session — do not claim they work in production until
confirmed against a real account.

## Security limitations (unchanged, expanded scope)

No CAPTCHA bypass, no credential theft, no stealth browsing, no anti-bot circumvention, no
proxy rotation, no authentication bypass, no rate-limit evasion — anywhere, for any
provider. This is why 6 target providers (Teamtailor, Jobvite, Pinpoint, JazzHR, iCIMS,
Oracle) are UNSUPPORTED rather than "implemented via workaround." `submission_supported`
is `False` for every provider — this phase adds discovery, not submission.

## Exact remaining gaps

- Workable/Recruitee/BambooHR connectors are fixture-verified only; no live tenant was
  confirmed in this session — verify against a real account before relying on them.
- Comeet requires manually sourcing a company's public embed token; not exercised live.
- Workday tenant configuration is fully manual (exact base URL per tenant) — no
  auto-discovery of hosting number/site name, by design (never guess a tenant's plumbing).
- Company registry is empty by default in a real deployment; Phase 4's bulk importer
  (not built here) is what populates it at scale.
- Adaptive polling constants (backoff multipliers, health thresholds) are simple and fixed,
  not tuned against real long-running data — reasonable defaults, not validated at scale.

## Recommended Phase 4

Build the bulk company/tenant importer described in `docs/company-registry.md` (bulk
`insert_entry` from an external company list + `app/providers/detector.py` to guess
provider/tenant from a bare careers URL, with a human-review queue for low-confidence
detections), plus real verification passes against Workable/Recruitee/BambooHR/Comeet
accounts, before scaling tenant count materially beyond what one laptop's
`PROVIDER_CONCURRENCY_LIMIT` can handle in a `DISCOVERY_INTERVAL_MINUTES` window.
