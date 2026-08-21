# Registry Operations

## Registry doctor (`app/registry/doctor.py`)

```
python -m app.registry.cli doctor        # or GET /registry/doctor in the dashboard
```

Read-only integrity checks against the real DB, exits nonzero (CLI) when any **serious** issue is
found:

| Check | Severity | Meaning |
|---|---|---|
| `duplicate_canonical_portal` | serious | >1 enabled portal shares the same `canonical_url`. |
| `active_missing_tenant` | serious | An `ACTIVE`/`VERIFIED` portal has no `tenant_identifier`. |
| `verified_without_provenance` | serious | An `ACTIVE`/`VERIFIED` portal has zero provenance rows. |
| `unsupported_marked_active` | serious | An `ACTIVE`/`VERIFIED` portal's provider is `SupportLevel.UNSUPPORTED`. |
| `invalid_careers_url` / `invalid_canonical_url` | warning | Not a well-formed `http(s)://` URL. |
| `orphan_provenance` | warning | A provenance row references a `portal_id` that no longer exists. |
| `contradictory_domain_mapping` | warning | The same `primary_domain` maps to >1 differently-named company. |
| `impossible_scheduler_state` | warning | A `STALE`/`QUARANTINED`/`DISABLED` portal still has `next_poll_at` set. |

## Safe career-page discovery (`app/registry/page_discovery.py`)

Given a company domain, `discover_career_links(domain)`:

1. Fetches robots.txt (best-effort; missing/unparseable → allow-all, standard semantics) and
   skips any disallowed path.
2. Fetches a small **fixed** candidate path list: `/`, `/careers`, `/jobs`, `/about/careers`,
   `/company/careers`, `/about-us/careers` — never a general crawl.
3. On every fetched page, harvests `<a href>` links: off-company-domain links are direct ATS
   candidates; same-company-domain-family links (e.g. `about.example.com` vs `example.com`) with
   a career/job keyword are queued as one bounded follow-up hop (real careers pages often live on
   a subdomain one click from the homepage).
4. Runs the existing `app.providers.detector.detect_provider()` against every candidate link and
   returns the highest-confidence match, or `None`.

Hard bounds, all configurable in `.env`: `PAGE_DISCOVERY_MAX_PAGES` (default 9, shared across the
fixed paths and the one follow-up hop), `PAGE_DISCOVERY_TIMEOUT_SECONDS`,
`PAGE_DISCOVERY_MAX_RESPONSE_BYTES`, `PAGE_DISCOVERY_MAX_REDIRECTS`. No JS execution, no stealth
browser, no login pages, no LinkedIn/Indeed/search-engine scraping.

**Known limitation** (observed live this session against gitlab.com and retool.com): many real
company sites render their careers link client-side (JavaScript), so a plain HTTP GET never sees
the anchor even though a human browser would. This is an accepted trade-off, not a bug to
"fix" with a headless browser — CLAUDE.md explicitly forbids stealth browsing. Direct CSV/JSON
import (`docs/registry-import.md`) is the reliable path for such companies; page discovery is a
bonus signal, not the primary acquisition mechanism.

## Adaptive scheduling, sharding, backpressure

- **Portal-level scheduling** reuses the exact Phase 3 rules (`app/registry/scheduling.py`) once
  a portal is mirrored into `company_registry` — no second scheduler was built. See
  `docs/company-registry.md`.
- **Sharding** (`app/registry/sharding.py`): `shard_for_portal(portal_id, shard_count)` is a pure
  SHA-256-based hash, deterministic and collision-free by construction (each id maps to exactly
  one shard). `REGISTRY_SHARD_COUNT`/`REGISTRY_SHARD_INDEX` default to `1`/`0` (no behavior
  change locally). This is partitioning groundwork for a future distributed worker (Phase 7+) —
  no distributed infrastructure exists yet.
- **Backpressure**: every registry-scale query (`app/registry/store.py`) is bounded — `LIMIT` +
  keyset (`id > after_id`) pagination, never `SELECT *` over the whole table.
  `REGISTRY_DUE_BATCH_SIZE`, `REGISTRY_MAX_PORTALS_PER_CYCLE`,
  `DISCOVERY_CYCLE_TIME_BUDGET_SECONDS` are the configured ceilings; the existing Phase 3
  `list_due_for_poll(limit=200)` (`app/registry/repo.py`) already enforces this for the actual
  polling path.

## Operational commands

```
python -m app.registry.cli import <path>     # bulk import (docs/registry-import.md)
python -m app.registry.cli validate <path>   # dry-run, report only
python -m app.registry.cli stats             # real DB-derived snapshot + per-provider breakdown
python -m app.registry.cli export <path>     # JSONL/JSON export, no candidate data
python -m app.registry.cli doctor            # integrity checker
python -m app.registry.cli verify [--limit N] [--provider NAME]   # live verification pipeline run
```

`verify` is the operational loop: pulls up to `--limit` `DISCOVERED`/`CANDIDATE` portals
(`store.list_due_for_verification`), runs `verify_portal`, applies the lifecycle transition, and
syncs the result into the operational registry. Run it periodically (e.g. cron, or a future
scheduler hook) to advance newly-imported candidates toward `ACTIVE` — it is **not** wired into
the automatic discovery cycle's timer in this phase, since verification issues live network
requests to potentially many new tenants and should stay an explicit, rate-considered operation
distinct from the existing polling loop.

## Dashboard

`/registry` now shows, below the unchanged Phase 3 operational table: summary cards (real
`app.registry.analytics.snapshot()` counts), provider/status/support-level/enabled filters,
company/domain/tenant search, and a bounded (200-row) portal table linking to
`/registry/portals/{id}` (full detail: provenance, confidence reasons, health, sibling portals,
migration history) with safe POST actions — Verify, Recheck careers page, Enable, Disable,
Quarantine. `/registry/doctor` renders the same integrity report as the CLI.
