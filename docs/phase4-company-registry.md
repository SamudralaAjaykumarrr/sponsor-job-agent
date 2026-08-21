# Phase 4 — Company / Career-Portal Registry: Plan

## Objective

Build the registry **acquisition, verification, lifecycle, and operational tooling** layer
that lets the system scale from a handful of manually-added tenants (Phase 3) toward
ingesting and verifying 1,000 → 10,000 → 50,000+ → 100,000+ real corporate career portals.
This is infrastructure, not fake scale — registry size is never a target metric on its own.

## Relationship to Phase 3

Phase 3 shipped `company_registry` (`app/registry/models.py::CompanyRegistryEntry`), the
**operational polling table** consumed directly by `app/agent/cycle.py::_discover_from_registry`.
That table and its polling/scheduling behavior are **not rebuilt** — they are preserved
byte-for-byte and remain the only thing the discovery cycle reads from.

Phase 4 adds a **new, richer acquisition/verification layer** on top:

```
RegistrySource (CSV/JSON/JSONL/page-discovery)
        → RegistryCandidate
        → registry_companies / registry_portals  (DISCOVERED)
        → tenant extraction + structural checks    (CANDIDATE)
        → verification pipeline (live, bounded)    (VERIFIED)
        → identity/quality checks pass             (ACTIVE)
        → sync bridge mirrors provider+tenant into the EXISTING company_registry row
          (insert/update via app.registry.repo, unchanged schema/unique index)
        → existing Phase 3 discovery cycle polls it exactly as before
```

Only `registry_portals` rows with `verification_status` in `{VERIFIED, ACTIVE}` are ever
mirrored into `company_registry` for production polling. A portal that regresses
(`STALE`/`QUARANTINED`/`DISABLED`) has its mirrored `company_registry` row disabled
(`enabled=False`), never deleted — provenance and poll history are retained.

## New modules (`app/registry/`)

| File | Responsibility |
|---|---|
| `models.py` (extended) | `Company`, `CareerPortal`, `RegistryProvenance`, `PortalStatus`, `VerificationResult`, `DiscoveryStatus` enums — additive, existing `CompanyRegistryEntry` untouched. |
| `normalize.py` | Deterministic company name + domain normalization. |
| `url_canon.py` | Career/ATS URL canonicalization (tracking-param/fragment/slash/case-insensitive, tenant-preserving). |
| `store.py` | CRUD + dedup lookups + paginated/bounded queries for companies/portals/provenance. |
| `quality.py` | Rule-based confidence score + human-readable reasons (no opaque probability). |
| `verification.py` | Bounded live verification pipeline; VERIFIED/FAILED/AMBIGUOUS/UNSUPPORTED/TEMPORARY_FAILURE. |
| `lifecycle.py` | Health/backoff bookkeeping, STALE demotion, ATS-migration detection. |
| `sharding.py` | Deterministic `portal_id → shard` assignment (`REGISTRY_SHARD_COUNT`/`REGISTRY_SHARD_INDEX`). |
| `sync.py` | Bridges VERIFIED/ACTIVE portals into the existing `company_registry` operational table. |
| `page_discovery.py` | Bounded, safe, public-only career-page link discovery for a given company domain. |
| `importers.py` | `RegistrySource` interface + CSV/JSON/JSONL importers, batch/dry-run/idempotent upsert engine. |
| `doctor.py` | Registry integrity checker. |
| `analytics.py` | DB-derived aggregate snapshot/breakdown stats. |
| `export.py` | Streaming/batched JSONL (or JSON) export, no candidate data. |
| `cli.py` | `python -m app.registry.cli {import,validate,stats,export,doctor,verify}`. |

## Data model additions (all additive `CREATE TABLE IF NOT EXISTS`, no `ALTER` on existing tables)

`registry_companies`, `registry_portals`, `registry_provenance`,
`registry_portal_health_events`, `registry_migrations`, `registry_import_batches`.

`registry_portals.verification_status` carries the full lifecycle:
`DISCOVERED → CANDIDATE → VERIFIED → ACTIVE → DEGRADED/STALE/QUARANTINED → DISABLED`.
`discovery_status` separately records *how* the candidate was found
(`IMPORTED`/`DETECTED`/`PAGE_DISCOVERY`/`MANUAL`) — a different axis than lifecycle state.

## What is explicitly out of scope this phase

Per CLAUDE.md section 38: LinkedIn/Indeed auto-apply, CAPTCHA/MFA bypass, stealth browsers,
uncontrolled crawling, distributed deployment, ML ranking, email tracking, ATS form
submission. `submission_supported` stays `False` everywhere.

## Real seed data policy

Only a small number of real, independently-verifiable public companies are seeded, each with
explicit provenance (`source_type="manual_seed"`), and only entries that pass **live**
verification during this session are marked VERIFIED — everything else stays
CANDIDATE/DISCOVERED. No invented tenants, no invented domains, no invented scale.

## Results (2026-08-21)

Implemented end-to-end: data model, normalization, URL canonicalization, bulk CSV/JSON/JSONL
import with idempotent upsert + provenance, a bounded live verification pipeline (raw structural
probe + best-effort enrichment), deterministic quality scoring, lifecycle transitions (including
permanent-vs-temporary-failure-aware demotion and ATS-migration detection), deterministic
sharding, a sync bridge into the unchanged Phase 3 operational polling table, safe bounded
career-page discovery, a registry doctor, real DB-derived analytics, streaming export, a CLI
(`python -m app.registry.cli`), and dashboard filters/portal-detail/POST actions.

312 tests pass (205 unmodified Phase 3 + 107 new). A synthetic 1k/10k/50k/100k benchmark (isolated
temp DB only) showed bounded queries staying flat and bulk import/export scaling linearly and
comfortably (see `docs/registry-scaling.md` for exact numbers). Live validation against 20 real
public companies across 3 provider families reached 19/20 `ACTIVE` and correctly quarantined the
1 ambiguous case rather than guessing (see `docs/acceptance_verification.md` for the full
transcript, including a real bug this session's own live run caught and fixed in the
verification probe).

Full acceptance-scenario detail: `docs/acceptance_verification.md` "Phase 4" section.
Recommended Phase 5: see that document's final report / the session's closing summary — in
short, a distributed poller matched to the sharding groundwork built here, plus scaling up real
(not synthetic) registry acquisition using the import/verify tooling now in place.
