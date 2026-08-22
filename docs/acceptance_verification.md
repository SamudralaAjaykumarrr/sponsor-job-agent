# Acceptance Verification

Verified 2026-08-21. Every item below was actually executed, not assumed.

| Criterion | Evidence |
|---|---|
| App starts | `./start.sh` launched uvicorn; `curl /health` -> `{"status":"ok"}` |
| Dashboard loads | `curl /` -> HTTP 200, rendered HTML with job table |
| Manual JD ingestion works | `POST /jobs/ingest` (curl, live server) -> 303 redirect to new job detail page |
| Sponsorship classification works | `tests/test_sponsorship.py` (5 tests) + live curl: "Visa sponsorship available" -> CONFIRMED_SPONSOR; known employer -> LIKELY_SPONSOR; no match -> UNKNOWN |
| No-sponsorship jobs are skipped | `tests/test_pipeline.py::test_no_sponsorship_job_is_hard_skipped` + live curl: "unable to sponsor" -> NO_SPONSORSHIP, application_state=SKIPPED, no resume files generated |
| Work arrangement classification works | `tests/test_workarrangement.py` (5 tests): remote/hybrid/onsite/unknown, incl. "no remote" vs "remote" disambiguation |
| Freshness tracking works | `tests/test_freshness.py` (7 tests) covering all 5 tiers + fallback to first_seen_at + unparseable dates |
| Scoring works | `tests/test_matching_and_scoring.py` incl. remote+confirmed > onsite+likely, NO_SPONSORSHIP/UNKNOWN forced to score 0 |
| High-priority jobs are identified | Live job scored P1_REMOTE_CONFIRMED (120.0); dashboard "High Priority" filter maps to P1-P3 tiers |
| Tailored resume generation works | `tests/test_resume.py::test_generate_resume_only_uses_verified_data`; live server generated resume.docx/pdf/txt for a CONFIRMED_SPONSOR job |
| DOCX works | `write_docx` produces non-empty file (test + live curl download) |
| PDF works | `write_pdf` produces non-empty file (test + live curl download) |
| Unsupported claims are blocked | `tests/test_resume.py::test_claim_checker_blocks_fabricated_bullet/_skill/_employer` -- injected fabricated content is caught before it could reach output |
| Application answers work | `generate_application_answers` verified via live curl download of `application_answers.json` |
| Unknown factual answers become NEEDS_USER_INPUT | Live curl: blank profile -> every personal field in resume.txt and application_answers.json is literally `NEEDS_USER_INPUT`, never fabricated |
| Tracking works | `jobs` SQLite table with `application_state`, state-transition guard in `applications/tracker.py`, output files tracked 1:1 via `output/<job_id>/` + DB path columns |
| Tests pass | `pytest tests/ -q` -> 45 passed |
| `./start.sh` works | Ran directly; server bound to 127.0.0.1:8000 and responded to requests |
| No secrets committed | `candidate_data/` (private facts) and `data/app.db` are gitignored; verified with `git check-ignore -v`; no API keys/secrets in tracked files |

## Known MVP limitations (by design, per spec)

- `LIKELY_SPONSOR` reference list (`data/known_h1b_sponsors.json`) is a small illustrative bundled list, not a live USCIS data feed -- flagged as review-only everywhere it's used, never treated as proof.
- AUTO application-submission mode remains a stub for future use; ASSIST (prepare, never submit) is the only implemented mode.

## Phase 2 — Autonomous discovery agent (verified 2026-08-21)

Candidate profile is now fully populated (`candidate_data/profile.json`, 0 remaining
`NEEDS_USER_INPUT` fields, 48 skills / 3 employment entries / 2 projects) — real generated
resumes were exercised in this verification pass, not just the synthetic test fixture.

| Criterion | Evidence |
|---|---|
| `pytest` passes | `pytest tests/ -q` -> **87 passed** (45 original + 42 new), 0 failures |
| Provider normalization | `tests/test_providers.py` (Greenhouse + Lever, `httpx.MockTransport`, no live network in the suite) |
| Provider error isolation | `tests/test_providers.py::*_isolates_*_errors`; live-network run also isolated a real Lever read-timeout without aborting the cycle (see below) |
| Deduplication | `tests/test_new_gates.py` (fingerprint stability/uniqueness) + `tests/test_discovery_cycle.py::test_duplicate_posting_is_stored_once` (same external_job_id re-fetched -> 1 row, no duplicate package) |
| Freshness cutoff / gate | `app/agent/cycle.py::_pre_filter_reason`; live run against the real GitLab board showed 31/50 fetched jobs correctly discarded as older than the 3-day cutoff |
| Title/seniority filtering | `tests/test_new_gates.py` (7+ years hard skip; senior title without compatible-years evidence skipped; senior title WITH compatible years passes) + `tests/test_discovery_cycle.py::test_overly_senior_role_is_skipped` |
| Salary filtering | `tests/test_new_gates.py` (rejects only a published max below $80k; never rejects unpublished salary; `$90k-$120k` text-range extraction) |
| Sponsorship hard gate | `tests/test_discovery_cycle.py::test_no_sponsorship_is_hard_skipped` -> `SKIPPED_NO_SPONSORSHIP`, no package |
| Confirmed vs likely sponsor distinction | `tests/test_discovery_cycle.py::test_confirmed_sponsor_remote_reaches_ready_to_apply` (-> READY_TO_APPLY) vs `::test_likely_sponsor_reaches_review_required` (-> REVIEW_REQUIRED, package generated but flagged, never auto-submitted) |
| Full discovery-to-READY_TO_APPLY pipeline | `tests/test_discovery_cycle.py::test_full_cycle_all_five_scenarios_together` — all 5 required smoke scenarios in one cycle |
| Duplicate prevention (cross-cycle) | Same test, cycle re-run with identical postings -> `jobs_new=0`, row count unchanged |
| Provider failure isolation | `tests/test_discovery_cycle.py::test_provider_failure_does_not_abort_cycle` + live run (Lever timeout logged, Greenhouse jobs still processed) |
| Per-job failure isolation | `tests/test_discovery_cycle.py::test_per_job_error_isolated` |
| Scheduler loop | `tests/test_agent_scheduler.py` — survives a crashing cycle and keeps looping; stops cleanly; no-ops while disabled |
| Resume claim blocking (still enforced) | Unchanged `tests/test_resume.py`; pipeline now routes violations to `CLAIM_VALIDATION_FAILED` (`tests/test_pipeline.py` still passes with the real gating in front of it) |
| Dashboard routes | `tests/test_agent_dashboard.py` — `/agent/status`, `/agent/toggle`, `/jobs/{id}/regenerate`, `Review Required` filter, score-breakdown + pipeline-history rendering, manual state-transition history recording |
| Safe additive migration | `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` run against the real, pre-existing `data/app.db` — verified 0 rows lost, all new columns/tables present |
| Live end-to-end smoke test (real network, real candidate profile) | `run_discovery_cycle()` executed twice against live Greenhouse (`gitlab`) + Lever (`leverdemo`) boards. First run (default 3-day freshness cutoff): 25/50 fetched jobs correctly pre-filtered (19 non-US, 31 stale — some overlap), 0 stored. Second run (cutoff relaxed to 365d for verification only): 8 jobs stored — 7 non-STEM roles correctly `SKIPPED`, 1 "AI Engineer" correctly classified REMOTE/sponsorship-UNKNOWN and held at `ANALYZED` (no package — "do not apply" policy), Lever request timed out and was isolated with 0 cycle-level errors. |
| No secrets committed | `.env` added to `.gitignore` (already covered `.env`); `.env.example` has no real values; `git status`/`git diff` reviewed before nothing was committed by this session |

### Known Phase 2 limitations

- Default example sources (`GREENHOUSE_BOARD_TOKENS=gitlab`, `LEVER_COMPANY_SLUGS=leverdemo`) are illustrative only — most real postings on a general company board won't be CS/STEM roles or explicitly confirm sponsorship, and Lever's public demo account is fake data, not real jobs. Add real target-company board tokens/slugs in `.env` for meaningful discovery.
- Neither Greenhouse nor Lever reliably expose structured salary or sponsorship fields; the compensation gate falls back to a regex JD-text extractor, and sponsorship still relies on explicit JD phrasing or the local known-sponsor list per the original hard-gate rules.
- `MIN_MATCH_SCORE`/`FRESHNESS_MAX_DAYS`/board selection are blunt, user-tunable knobs, not adaptive.

## Phase 3 — ATS coverage expansion (verified 2026-08-21)

Full detail in `docs/phase3-ats-coverage.md`. This section covers only the acceptance
criteria specific to Phase 3.

| Criterion | Evidence |
|---|---|
| `pytest` passes | `pytest tests/ -q` -> **205 passed** (87 Phase 2, unmodified in behavior, + 118 new Phase 3 tests), 0 failures |
| Provider capability model | `tests/test_provider_capabilities.py` — every provider's declared support level matches the documented matrix; no provider claims `submission_supported` |
| HTTP hardening (timeout/retry/backoff/size-cap/no-infinite-retry) | `tests/test_http_client.py` — bounded retries verified to stop at exactly `max_retries+1` attempts, `Retry-After` respected, response-size cap enforced by both header and actual body length |
| Provider detection + tenant extraction | `tests/test_provider_detector.py` (21 tests) — every target ATS's URL pattern, plus malformed/unknown-URL "no match" cases and confidence-never-overclaimed checks |
| Ashby / Workable / SmartRecruiters connectors | `tests/test_ashby_provider.py`, `tests/test_workable_provider.py`, `tests/test_smartrecruiters_provider.py` — normalization, per-tenant error isolation, malformed payloads, pagination (Workable/SmartRecruiters), max_jobs respected |
| Other ATS connectors (BambooHR/Recruitee/Breezy/Comeet) + unsupported stubs | `tests/test_other_ats_providers.py` — including BambooHR's documented no-description limitation and unsupported providers always returning `[]` without raising |
| Workday connector | `tests/test_workday_provider.py` — normalization, pagination, detail fetch, `postedOn` never fabricated into a timestamp, clean failure on a blocked/403 tenant (scenario G) |
| Company/tenant registry | `tests/test_registry.py` (16 tests) — CRUD, due-for-poll query, adaptive interval rules (speed up/slow down/back off), health thresholds, provider health aggregation, additive-migration safety with real pre-existing data |
| Registry demo seed | `tests/test_registry_seed.py` — off by default, idempotent |
| Cross-provider dedup + URL canonicalization | `tests/test_dedup_phase3.py` — tracking-param stripping, host/trailing-slash normalization, stable fingerprint; acceptance **scenario D** (same requisition from two providers -> one job, two provenance records); regression test proving two genuinely different requisitions with matching title/company/location are **not** wrongly merged when their stable IDs/URLs differ |
| Freshness source | `tests/test_dedup_phase3.py` — **scenario F**: no `published_at` -> `freshness_source=FIRST_SEEN` |
| Registry-driven adaptive discovery + tenant health | `tests/test_discovery_registry_cycle.py` — due-tenant processing, discovery_log rows, disabled tenants never polled, backoff prevents immediate repoll, **scenario E** (failing tenant marked degraded/failing, healthy tenant still processed same cycle), 25-tenant scheduling scale test |
| Bounded concurrency | `tests/test_concurrency.py` — order preserved, concurrency cap actually enforced under a real thread-timing test |
| Dashboard provider/registry routes | `tests/test_dashboard_phase3.py` — `/providers`, `/registry`, `/registry/add`, provider filter, `/discovery-log`, nav links present |
| Full acceptance scenarios A/B/C/G | `tests/test_acceptance_scenarios_phase3.py` — Ashby/Workable/SmartRecruiters fixtures driven through the complete pipeline to `READY_TO_APPLY`/`REVIEW_REQUIRED` with provenance recorded and re-run dedup verified; Workday 403/blocked tenant produces zero jobs and zero fabricated errors |
| DB migration safety | `tests/test_registry.py::test_migration_preserves_existing_jobs_and_state` — inserts a job + a discovery cycle, runs `init_db()` twice more, asserts both rows unchanged; new tables (`company_registry`, `job_provenance`, `discovery_log`) confirmed present via `sqlite_master` |
| Live smoke tests | Real public endpoints, `max_jobs=5`, no auth/anti-bot bypass: **Greenhouse (gitlab), Lever (leverdemo), Ashby (ashby), SmartRecruiters (SmartRecruiters), Breezy (breezy), Workday (workday.wd5.myworkdayjobs.com/Workday)** all confirmed fetching + normalizing real live postings. Workable/Recruitee/BambooHR were attempted against guessed tenant names that did not resolve to a real account — fixture tests pass, but these three are **not** live-verified this session; do not claim they work against a real account until confirmed. No test-suite failure resulted from any live attempt (per policy, live results never gate the automated suite). |
| `./start.sh` still works | Started directly; `/health`, `/`, `/providers`, `/registry` all returned HTTP 200; process cleanly stopped afterward |
| No secrets committed | `candidate_data/`, `data/app.db`, `.env` all still gitignored (`git check-ignore -v` re-verified); `git status`/`git diff` reviewed — no data/output/secrets staged; nothing committed by this session (explicitly not asked to commit) |

### Known Phase 3 limitations

- Workable, Recruitee, and BambooHR connectors are verified only against fixtures in this
  session — no live account was confirmed. Comeet requires a manually-sourced public embed
  token and was not exercised live at all.
- Teamtailor, Jobvite, Pinpoint, JazzHR, iCIMS, and Oracle Recruiting Cloud remain
  UNSUPPORTED for discovery (detection + registry representation only) — no safe public
  unauthenticated interface was found; see `docs/provider-capabilities.md` for the exact
  reasoning per provider.
- The company registry ships empty by default (an optional two-row illustrative seed exists
  behind `REGISTRY_SEED_DEMO_DATA=false`) — a real Phase 4 bulk importer is required to
  populate it at scale.

## Phase 4 — Company/career-portal registry (verified 2026-08-21)

Full detail in `docs/phase4-company-registry.md`, `docs/registry-import.md`,
`docs/registry-verification.md`, `docs/registry-operations.md`, `docs/registry-scaling.md`.

| Criterion | Evidence |
|---|---|
| `pytest` passes | `pytest tests/ -q` -> **312 passed** (205 Phase 3, unmodified in behavior, + 107 new Phase 4 tests), 0 failures, ~50s |
| Company normalization | `tests/test_registry_normalize.py` — legal-suffix stripping, whitespace/case, domain host/scheme/www/trailing-slash normalization, similar-name-different-domain non-collision |
| Portal URL canonicalization | `tests/test_registry_normalize.py` — tracking-param stripping while preserving tenant-identifying params (e.g. `gh_jid`), Workday tenant/site path preserved exactly, www/host-case normalization |
| Bulk CSV/JSON/JSONL import, idempotency, provenance | `tests/test_registry_import.py` (12 tests) — 100-row CSV import + re-import with zero duplicates, dry-run writes nothing, invalid rows reported not dropped, company-only rows create no portal, scenario C (ambiguous URL never gets a fabricated tenant) |
| Company/portal dedup, identity-mismatch quarantine | `tests/test_registry_store.py`, `tests/test_registry_verification.py::test_verify_portal_identity_mismatch_is_ambiguous` |
| Tenant extraction | Reuses/extends `tests/test_provider_detector.py` (Phase 3, unmodified) via `app.registry.importers` |
| Verification (permanent vs. temporary failure, unsupported) | `tests/test_registry_verification.py`, `tests/test_registry_probe.py` — 404 -> `FAILED`, timeout -> `TEMPORARY_FAILURE`, no tenant -> `FAILED` without a network call, `UNSUPPORTED` provider short-circuits cleanly |
| Registry quality score | `app/registry/quality.py`, exercised in `tests/test_registry_import.py` (confidence computed on import) and shown live in the seed data below |
| Migration detection, stale lifecycle | `tests/test_registry_verification.py`, `tests/test_acceptance_scenarios_phase4.py::test_scenario_f_*`/`test_scenario_e_*`/`test_scenario_g_*`/`test_scenario_h_*` |
| Adaptive scheduling (portal-level) | Reuses Phase 3 `app/registry/scheduling.py` unchanged, via `app/registry/sync.py`'s mirror into `company_registry` |
| Deterministic sharding | `tests/test_registry_sharding.py` — exactly-one-shard-per-id, no overlap, reasonable distribution, up to 2,000 ids |
| Due-portal batching, cycle budget | `tests/test_acceptance_scenarios_phase4.py::test_scenario_i_*` (5,000-row bounded due-query) + `scripts/registry_benchmark.py` (50k/100k) |
| Registry analytics | `tests/test_registry_analytics_export.py` — real DB-derived snapshot + per-provider breakdown, verified against a known mixed-status registry |
| Dashboard filters, portal detail, POST actions | `tests/test_registry_dashboard_phase4.py` (9 tests) — summary cards, status filter, portal detail page (provenance shown), enable/disable/quarantine actions, GET-not-allowed on a mutating route (405) |
| Export | `tests/test_registry_analytics_export.py` — JSONL/JSON, streamed, provenance included, no candidate-data strings present |
| Registry doctor | `tests/test_registry_doctor.py` (7 tests) — every check triggered individually + a clean-registry zero-issues case; live run against the real populated registry also returned 0 serious / 0 warnings |
| Safe migration | `app/db.py` — all Phase 4 tables are `CREATE TABLE IF NOT EXISTS`, zero `ALTER TABLE` on any existing table; `pytest` includes the unmodified Phase 3 `test_registry.py::test_migration_preserves_existing_jobs_and_state` |
| 100k-scale synthetic benchmark | `scripts/registry_benchmark.py --sizes 1000,10000,50000 --include-100k`, isolated temp DB only — see exact numbers in `docs/registry-scaling.md` |
| Limited live validation | See below |

### Live validation (real network, real public companies, `2026-08-21`)

`data/registry_seed/real_companies_seed.csv` — 20 real, independently-known public companies
across 3 provider families (15 Greenhouse, 4 Ashby, 1 Workday), each row carrying
`source=live_verification_2026-08-21` and the exact API URL tested as `source_url`.

1. `python -m app.registry.cli import data/registry_seed/real_companies_seed.csv` -> 20/20 rows
   created, 0 invalid, 20 companies created.
2. `python -m app.registry.cli verify --limit 25` -> live network run against real endpoints.
   **Caught a real bug during this run**: the Greenhouse structural probe originally requested
   `content=true` (full HTML job descriptions for every posting), which pushed two large tenants
   (Cloudflare, Databricks) over the 5MB response-size cap and mis-classified them as
   `TEMPORARY_FAILURE`. Fixed (`app/registry/probe.py`) to omit `content=true` for the structural
   probe (job descriptions aren't needed to confirm the endpoint works) — re-run succeeded for
   both.
3. Final live result: **19/20 -> `ACTIVE`** (mirrored into the operational `company_registry`
   table), **1/20 -> `QUARANTINED`** (`greenhouse/scaleai`: the live response's company name
   `"Scaleai"` didn't token-overlap with registry name `"Scale AI"` — the identity-safety check
   correctly refused to auto-activate an ambiguous match rather than silently accepting it; see
   the "Known limitation" note in `docs/registry-verification.md`).
4. `python -m app.registry.cli doctor` on the resulting real registry -> **0 serious issues, 0
   warnings**.
5. `python -m app.registry.cli stats` on the resulting real registry:
   ```
   Companies: 20  Portals: 20  Verified: 19  Active: 19  Healthy: 19
   Candidate: 0   Quarantined: 1  Stale: 0    Degraded: 0
   ```
6. Safe page discovery (`app/registry/page_discovery.py`) was also live-tested against
   `gitlab.com` and `retool.com`: correctly found each site's real careers-page link
   (`about.gitlab.com/jobs/`) among the harvested candidates, but neither site's careers page
   links to its ATS via a plain server-rendered `<a href>` (both render that link client-side via
   JavaScript), so `best_match` was `None` for both — an honest, expected limitation of a
   JS-free, non-stealth discovery mechanism, not a defect; documented in
   `docs/registry-operations.md`.
7. No live-network attempt above was allowed to affect the automated `pytest` suite's pass/fail
   status, per policy — all fixture-based tests use `httpx.MockTransport`.
8. Confirmed no side effects outside registry scope: the `jobs` table row count was unchanged
   before/after this session's registry work (Phase 4 is registry-only, not job discovery), and
   `candidate_data/profile.json` was not read or modified by any registry code path.

### Known Phase 4 limitations

- The real seed (20 companies) is deliberately small, per CLAUDE.md's no-fake-scale policy —
  reaching 50,000+ *verified* portals is now an operational bulk-import/verify exercise using the
  tooling built here, not something this session pretends already exists.
- Company-identity verification is a naive token-set comparison (see `scaleai` false-positive
  above) — correct in intent (never silently trust an ambiguous match) but occasionally requires
  one human click to clear a benign case.
- Safe page discovery cannot see JavaScript-rendered careers links (by design — no stealth
  browser). CSV/JSON bulk import remains the reliable acquisition path for such companies.
- No distributed poller exists yet — sharding is groundwork only. See `docs/registry-scaling.md`
  "What remains" for the full list. *(Superseded by Phase 5 below — a real distributed poller
  now exists and enforces sharding.)*

## Phase 5 — Distributed polling execution layer (verified 2026-08-21)

Full write-up: `docs/phase5-distributed-polling.md`, `docs/worker-architecture.md`,
`docs/polling-leases.md`, `docs/registry-acquisition.md`, `docs/fleet-operations.md`,
`docs/scaling-claims.md`.

| Requirement | Verification |
|---|---|
| Atomic lease acquisition, no double-poll | `tests/test_workers_leasing.py` (threaded + real multi-process ad hoc check: 6 OS processes, 40 rows, 0 duplicates); `tests/test_acceptance_scenarios_phase5.py` |
| Lease expiration / crash recovery | `test_worker_crash_recovery_lease_expires_and_is_reclaimed`, live-tested below |
| Worker heartbeat / identity / status | `tests/test_workers_graceful_shutdown.py`, `tests/test_workers_runner.py::test_heartbeat_and_final_status_recorded` |
| Sharding: partition + no overlap + deterministic | `test_shard_partition_covers_every_portal_exactly_once`, reused unchanged Phase 4 `app/registry/sharding.py` |
| Retry policy / Retry-After / bounded backoff | `tests/test_workers_retry_circuit.py`; Retry-After already covered by pre-existing `tests/test_http_client.py` |
| Circuit breaker (trip / half-open / never-permanent) | `tests/test_workers_retry_circuit.py` (17 tests) |
| Provider concurrency isolation | `test_provider_isolation_one_failing_provider_does_not_block_another`; live-confirmed below |
| Idempotent retry / no duplicate jobs | `test_idempotent_retry_does_not_duplicate_jobs` |
| Attempt history (bounded) | `tests/test_workers_runner.py` (attempt recorded for every path incl. cancellations) |
| Dead-letter / requeue | `tests/test_workers_dead_letter.py` (6 tests) |
| Schema drift vs. empty board | `tests/test_schema_drift.py` (17 tests, one per provider with a probe) |
| Acquisition batch + resume | `tests/test_registry_acquisition.py` (7 tests, incl. simulated-crash resume with zero duplicates) |
| Verification queue (separate lease, backoff, no hot-loop) | `test_verification_queue_failed_portal_backs_off_not_hot_loop` |
| Honest monitoring metrics (stored vs. actually-polled) | `tests/test_monitoring_metrics.py` (5 tests) |
| Discovery latency (real timestamps only) | `test_discovery_latency_ignores_fabricated_timestamps` |
| Dashboard fleet/acquisition routes | `tests/test_fleet_dashboard.py` (7 tests) |
| CLI (`run`/`status`/`attempts`/`dead-letter`, `acquire`/`batches`/`resume`) | `tests/test_workers_cli.py` (7 tests); `tests/test_registry_cli.py` updated |
| Safe migration, additive only | `tests/test_phase5_migration_and_sqlite_safety.py` (6 tests); real migration re-run against the actual populated `data/app.db` (see below) |
| SQLite WAL / busy-timeout, concurrent writers | `test_wal_mode_and_busy_timeout_configured`, `test_concurrent_writers_do_not_corrupt_or_deadlock` |
| Graceful shutdown | `tests/test_workers_graceful_shutdown.py` (5 tests) |
| Bounded due-query at scale | `tests/test_workers_bounded_queries.py`; full 1k/10k/50k/100k benchmark below |
| 4-worker local acceptance (100 synthetic portals) | `tests/test_acceptance_scenarios_phase5.py::test_local_four_worker_acceptance_scenario` |

### Synthetic scale benchmark (`scripts/worker_benchmark.py`, isolated temp DB)

```
size=1000    single_claim_50=0.0048s   8-worker drain of 950=0.150s   duplicate_claims=0
size=10000   single_claim_50=0.0071s   8-worker drain of 9950=0.464s  duplicate_claims=0
size=50000   single_claim_50=0.0139s   8-worker drain of 49950=1.845s duplicate_claims=0
size=100000  single_claim_50=0.0215s   8-worker drain of 99950=4.215s duplicate_claims=0
```

Bounded due-queries stay well under 25ms even at 100k rows; zero duplicate claims across 8
concurrent threads at every size. DB-only — says nothing about network capacity (see
`docs/scaling-claims.md`).

### Limited live validation (real network, real 2-worker run, `2026-08-21`)

Ran `python -m app.workers.cli run --shard-index {0,1} --shard-count 2 --once` as two real,
concurrent OS processes against the actual real, live-verified registry from Phase 4 (19 real
`ACTIVE` portals: 14 Greenhouse, 4 Ashby, 1 Workday) — no mocking, real internet, real candidate
profile downstream.

- Round 1: 11/19 portals successfully polled (`monitoring_coverage_24h: 0.58`) — the rest were
  correctly cooldown-deferred by `PROVIDER_CONCURRENCY_DEFAULT=3` (14 Greenhouse tenants sharing
  a budget of 3 concurrent requests within one bounded `--once` cycle).
- Rounds 2-3 (same command, re-run): coverage converged to 0.74, then 0.89, confirming continuous
  operation (not `--once`) is what closes the gap — expected, correct behavior, not a bug.
- **Live-caught and fixed a real bug during this run**: an uncaught `ResponseTooLargeError` from
  `GreenhouseProvider.fetch_jobs()` for an unusually large real board escaped every layer of
  per-tenant error isolation and crashed one worker's per-item task, leaving that portal's lease
  stranded with zero attempt record until natural expiry. Fixed with an outer safety-net
  `except Exception` around the fetch call in `app/workers/runner.py::_process_poll_item`,
  guaranteeing an attempt is always recorded and the lease always released regardless of failure
  cause. Regression test:
  `test_unexpected_fetch_jobs_exception_still_records_attempt_and_releases_lease`. Re-ran after
  the fix: zero further crashes.
- Final state: **444 real jobs** in the `jobs` table (up from 9 pre-existing), 1 `READY_TO_APPLY`
  (`CONFIRMED_SPONSOR`), 5 `REVIEW_REQUIRED` (`LIKELY_SPONSOR`), the rest correctly gated/skipped
  by the unchanged sponsorship/seniority/match pipeline. `python -m app.registry.cli doctor` and
  the fleet's own `dead_letters_open`/`provider_circuits_open_or_half_open` stayed at 0 throughout.
  `/`, `/fleet`, `/registry`, `/registry/doctor` all rendered correctly against the resulting real
  (non-trivial) database.

### Real registry growth test (`2026-08-21`)

`data/registry_seed/phase5_growth_seed.csv` (6 additional real companies) run through
`python -m app.registry.cli acquire ... --source-name phase5_growth_seed` against the real
database with real live verification: **6/6 records processed, 6 companies created, 6 portal
candidates, 3 verified+active (Dropbox, Affirm, Webflow), 3 correctly left unverified (DoorDash,
Plaid, Retool — guessed tenant slugs simply 404'd, an honest outcome, not a defect)**. Doctor: 0
issues throughout. See `docs/registry-acquisition.md` for the full breakdown.

### Real DB migration safety

`python -m app.db.init_db()`-equivalent (every CLI command calls it) was re-run against the real,
now-populated `data/app.db` multiple times across this session with zero data loss: existing
`jobs`, `application_state_history`, `discovery_cycles`, `registry_*`, and `company_registry` rows
were all preserved, and the full pre-existing 312-test suite continued passing unmodified
throughout Phase 5 development.

### Known Phase 5 limitations

- SQLite provides real multi-*process* (not multi-*machine*) concurrency — see
  `docs/scaling-claims.md` for the honest ceiling and the documented PostgreSQL/queue swap path.
- `PROVIDER_CONCURRENCY_DEFAULT`'s tight default (3) means a single bounded `--once` cycle does
  not always fully drain a provider with many tenants in one pass — by design (rate-limiting), but
  worth knowing before assuming one `--once` run proves full coverage; continuous operation
  converges over successive cycles, as demonstrated above.
- Schema-drift detection covers exactly the providers with a raw structural probe (10 today) —
  by construction the only providers that can ever reach `ACTIVE`, so there's no coverage gap for
  anything actually polled, but a provider manually added to `company_registry` without going
  through Phase 4 verification (bypassing the requirement) gets no real success/failure signal at
  all (pre-existing Phase 2/3 behavior, unchanged).
- The real verified registry is still small (26 companies / 22 active portals after this phase's
  growth test) — per CLAUDE.md's no-fake-scale policy, this was never inflated with synthetic
  rows; reaching materially larger real coverage is now a matter of running more legitimate,
  attributable acquisition batches with the tooling built here, not a code gap.
  "What remains" for the full list.

## Phase 6 acceptance verification

Verified 2026-08-22. Every item below was actually executed against real
SQLite and real PostgreSQL (`pgserver`), not assumed.

| Criterion | Evidence |
|---|---|
| SQLite backward compatibility | Full pre-Phase-6 suite (423 tests) + all Phase 6 additions run against SQLite: 478 passed |
| PostgreSQL backend works | `tests/test_postgres_backend.py` (real Postgres, 7 tests): init, insert/lastrowid, ON CONFLICT, rowcount, migrations, partial unique index |
| Postgres-safe leasing (SKIP LOCKED) | `tests/test_postgres_leasing.py` (real Postgres, 10 tests): 8 concurrent threads claim 200 portals, zero double-claims; crash recovery via lease expiry; verification-queue concurrent claims |
| sqlite-to-postgres migration tool | `tests/test_db_migrate.py` (real Postgres, 5 tests): dry-run, full migration + FK order, idempotent re-run, sequence advancement |
| Distributed acquisition checkpointing | `tests/test_phase6_distributed_acquisition.py` (5 tests): seed, concurrent claim, no duplicate companies, batch completion, crash-recovered row |
| Domain-seed pipeline | `tests/test_phase6_domain_seed_and_priority.py` (8 tests) + a REAL live run against 3 real companies (see `docs/registry-acquisition.md`) |
| Provider structured error result | `tests/test_provider_fetch_result.py` (15 tests) + `tests/test_phase6_runner_provider_error_gap.py` (the exact Phase 5 gap, closed and proven) |
| Persistent schema drift + circuit tie-in | `tests/test_phase6_schema_drift_persistence.py` (3 tests): single-tenant drift doesn't trip the circuit, provider-wide drift does |
| Worker identity/compatibility/orphan reaper | `tests/test_phase6_worker_identity_and_reaper.py` (6 tests) |
| Structured logging + correlation ids | `tests/test_phase6_structured_logging_and_correlation.py` (3 tests): PII-safe allowlist, correlation id flows attempt → job row |
| Multi-machine simulation | `tests/test_multi_machine_simulation.py` (SQLite + real Postgres): unique leases, shared circuit state, shared rate limit, orphan recovery |
| Distributed acceptance scenarios (A-J) | `tests/test_acceptance_scenarios_phase6.py` + cross-references to where each of the other 8 already live |
| `/health`, `/readiness`, `/metrics` | `tests/test_phase6_observability_endpoints.py` (9 tests), incl. `/health` never touching the DB and `/readiness` failing honestly when Postgres is unreachable |
| Real 2-worker live poll against real registry | Migrated the project's actual `data/app.db` (22 active portals) into a real, ephemeral Postgres and ran 2 real `Worker` instances for one cycle each — see `docs/phase6-production-scale.md` for the exact attempt/job counts and the 2 real bugs this run caught and fixed |
| Real domain-seed acquisition | 3 real companies (Shopify, DoorDash, Duolingo) → 1 ATS discovered → 1 portal VERIFIED with 5 real jobs seen; caught and fixed a real Greenhouse tenant-extraction bug |
| Synthetic scale benchmark, both backends | `scripts/phase6_scale_benchmark.py` run at 1k/10k/50k (SQLite) and 1k/10k (Postgres): zero duplicate claims at every size |
| No secrets committed | `.env.example`'s new `DATABASE_URL` line is blank; `deploy/docker-compose.postgres.yml` requires `POSTGRES_PASSWORD` via the environment, never hardcoded; verified no real credentials in any tracked file |
| `pytest` (default) unaffected | 478 passed, 20 deselected (postgres-marked) |
| `pytest -m postgres` | 20 passed, 478 deselected |

### Known Phase 6 limitations

See `docs/phase6-production-scale.md`'s "Honest limitations" section for
the full, unabridged list (Docker unavailability, synthetic-benchmark vs.
real-network-capacity distinction, the domain-seed pipeline's small real
sample size, sponsorship-evidence being Phase-7-foundation-only, etc.).

## Phase 7 acceptance verification

Verified 2026-08-22. Every item below was actually executed against real
SQLite and real PostgreSQL (`pgserver`), not assumed.

| Criterion | Evidence |
|---|---|
| SQLite backward compatibility | Full pre-Phase-7 suite (478 tests) + all Phase 7 additions run against SQLite: 571 passed |
| Evidence schema + idempotency | `tests/test_sponsorship_evidence_schema.py` (6 tests): normalization, snippet bounding, no-PII-fields, idempotent single + bulk insert |
| Identity resolution / aliases / relationships | `tests/test_sponsorship_identity.py` (11 tests): domain match, alias match, no-merge-on-similar-names, ambiguous → review, renamed/acquired companies, alias collision, relationship contradiction |
| Historical profile / recency / similarity | `tests/test_sponsorship_profile.py` (10 tests): recency buckets, strong-recent-technical, old-non-technical, trend, cache roundtrip, role/location similarity |
| Decision engine (CLAUDE.md section 43 examples A-G) | `tests/test_sponsorship_decision.py` (18 tests): all 7 required examples + 6 negation-safety phrases + versioning/JD-change/history-never-overrides |
| Government data importers | `tests/test_sponsorship_importers.py` (11 tests): USCIS + DOL LCA mapping, idempotent re-import, malformed rows, resumability, batching, dataset versioning |
| Review queue | `tests/test_sponsorship_review_queue.py` (2 tests) |
| Sponsorship doctor | `tests/test_sponsorship_doctor.py` (8 tests): orphan evidence, invalid fiscal year, alias collision, relationship contradiction, confirmed-without-evidence, no-sponsorship-not-hard-skipped, identity-review backlog |
| CLI | `tests/test_sponsorship_cli.py` (5 tests): import, stats, datasets, company, review-queue, doctor exit code |
| Dashboard + JSON API | `tests/test_sponsorship_dashboard.py` (13 tests): companies list/detail, review queue, doctor, identity-review resolve action, job-detail decision panel, all 4 new JSON endpoints, `/metrics` |
| Postgres compatibility | `tests/test_postgres_sponsorship.py` (real Postgres, 4 tests): schema creation, evidence insert + profile compute, decision versioning, idempotent insert — caught and fixed 2 real Postgres-specific bugs (see below) |
| End-to-end acceptance scenarios (1-8) | `tests/test_acceptance_scenarios_phase7.py` (9 tests, all 8 required scenarios + terminal-state safety) |
| Synthetic large-import benchmark | `scripts/sponsorship_benchmark.py` run at 10k/100k/500k (610k cumulative rows): import scales linearly, cached lookups stay flat — see `docs/phase7-sponsorship-intelligence.md` for exact numbers |
| Real-data validation | NOT RUN — no internet access in this build environment; importers fully implemented/tested against deterministic fixtures matching documented real formats |
| No secrets committed | No new `.env.example` entries required; verified no real credentials/raw datasets staged |
| `pytest` (default) unaffected | 571 passed, 24 deselected (postgres-marked) |
| `pytest -m postgres` | 24 passed, 571 deselected |

### Real bugs this phase caught and fixed

1. **Idempotent-insert full table scan**: `employer_sponsorship_evidence`'s
   idempotency check wasn't matching its own partial unique index (SQLite
   couldn't prove a bound parameter satisfied the index's `!= ''` clause),
   silently degrading a large import to O(n²) — a 110,000-row import that
   should take ~3s was measured taking 5+ minutes before the fix. Caught
   live while running the synthetic benchmark, not by any unit test (they
   only exercise small row counts). See
   `docs/phase7-sponsorship-intelligence.md` for the full writeup.
2. **Postgres NULL-parameter type inference**: `sponsorship_datasets`'s
   dataset-lookup query used a `(col IS NULL AND ? IS NULL)` pattern that
   psycopg couldn't type-infer (`IndeterminateDatatype`) when the parameter
   was `None` — fixed by switching to the portable `col IS NOT DISTINCT
   FROM ?` form (SQLite 3.39+ and PostgreSQL both support it). Caught by
   `tests/test_postgres_sponsorship.py` against a real Postgres server, not
   by the SQLite-only test suite.

### Known Phase 7 limitations

See `docs/phase7-sponsorship-intelligence.md`'s "Exact limitations" and
"Recommended Phase 8" sections for the full, unabridged list.

## Phase 8 acceptance verification

Verified 2026-08-21/22. Every item below was actually executed against real
SQLite, real PostgreSQL (`pgserver`), and a real, running dashboard process
(`uvicorn`) hitting the live `data/app.db` (read-only checks only — no
synthetic/mock job was ever inserted into the real database).

| Criterion | Evidence |
|---|---|
| SQLite backward compatibility | Full pre-Phase-8 suite (634 tests incl. all Phase 1-7) run against SQLite: 634 passed |
| FULL_TIME hard gate | `tests/test_applications_gates.py` (17 tests): positive classification for every `EmploymentType`, structured-field vs. free-text signal precedence, negative-signal-wins-over-coincidental-positive-text |
| Eligibility gate | Same file: US-location/CS-STEM/seniority/compensation/match-score/resume-artifact/answer-completeness/terminal-state checks, CONFIRMED vs LIKELY vs UNKNOWN vs NO_SPONSORSHIP branching |
| Field mapping engine | `tests/test_applications_mapping.py` (10 tests): EXACT/HIGH/MEDIUM/LOW confidence tiers, and the structural guarantee that legal/demographic fields can never resolve via the fuzzy MEDIUM path |
| Mock ATS + demographic defaults | `tests/test_applications_mock_ats.py` (3 tests): decline-to-self-identify default when unanswered, truthful answer when stated, missing-required-file-upload blocks auto-submit |
| Greenhouse adapter (fixture, real-shaped) | `tests/test_applications_providers_greenhouse.py` (3 tests) using a fixture payload modeled on a REAL live response (see below) |
| Provider capability matrix honesty | `tests/test_applications_provider_capabilities.py` (5 tests): only `mock_ats` claims `submission_supported`, Lever/generic honestly `UNSUPPORTED` |
| Rate limits | `tests/test_applications_rate_limits.py` (3 tests): hourly cap, per-company-per-day cap |
| Distributed duplicate-submission lock | `tests/test_applications_concurrency.py` (1 test): 8 real threads racing `queue_application()` for the SAME job produce exactly ONE execution row |
| Application doctor | `tests/test_applications_doctor.py` (3 tests): clean-after-normal-flow, catches a corrupted `APPLIED`-without-confirmation row, catches an orphaned execution |
| CLI | `tests/test_applications_cli.py` (4 tests): validate/prepare/status/doctor |
| Dashboard | `tests/test_applications_dashboard.py` (4 tests): `/applications`, `/applications/doctor`, executor-disabled 400, full prepare-to-APPLIED flow via HTTP |
| Postgres compatibility | `tests/test_applications_postgres.py` (real Postgres, 4 tests): schema creation, partial-unique-index duplicate guard, execution lifecycle, distributed queue claim (second worker gets nothing) |
| End-to-end acceptance scenarios A-J | `tests/test_acceptance_scenarios_phase8.py` (10 tests) — every lettered scenario in CLAUDE.md Phase 8 section 53, all passing on first real run after fixing test-setup issues (see "Real bugs" below) |
| Live dashboard verification | Started the real app against the real `data/app.db` (444 real jobs from Phases 1-7): `/`, `/jobs/{id}` (with the new Application execution card), `/applications`, `/applications/doctor`, `/api/applications/metrics`, `/health`, `/readiness` (schema_version 20/20) all returned 200 |
| Live doctor runs (real data) | `python -m app.applications.cli doctor` / `registry.cli doctor` / `sponsorship.cli doctor`: 0 serious issues each, against the real, unmodified `data/app.db` |
| No secrets/private data committed | `git status` shows no `.env`, no `data/app.db`, no `candidate_data/profile.json` staged (all gitignored, confirmed via `git check-ignore`) |
| `pytest -m "not postgres"` | 634 passed, 28 deselected |
| `pytest -m postgres` | 28 passed, 634 deselected |

### Live network validation (honest, bounded)

`https://boards-api.greenhouse.io/v1/boards/gitlab/jobs/{id}?questions=true`
(the real, public, documented Greenhouse Job Board API) was fetched live
during development and confirmed to return genuine structured application
fields, including a real sponsorship question and EEOC demographic
questions with decline-to-self-identify choices. `https://api.lever.co/v0/postings/leverdemo?mode=json`
was also fetched live and confirmed to expose NO structured question schema
(only `hostedUrl`/`applyUrl`) — this is why Lever is honestly `UNSUPPORTED`
for form discovery rather than guessed. **No submission request was ever
sent to any real ATS** — `GreenhouseApplicationProvider.submit()` is not
implemented (base class refusal only), matching CLAUDE.md's explicit
instruction not to submit real applications during development.

### Real bugs this phase caught and fixed

1. **`automation_policy` never persisted on the successful auto-submit
   path**: every branch that stopped short of submitting (`not validation.ok`,
   `not auto_ok`, rate-limited) correctly wrote `automation_policy`/
   `policy_reasons` to the execution row, but the actual successful-submit
   path fell straight through to `SUBMITTING`/`SUBMITTED`/`APPLIED` without
   ever writing them — leaving a genuinely-`PERMITTED_AUTO`-submitted
   execution's `automation_policy` column blank. Caught by
   `tests/test_applications_doctor.py`'s `submitted_without_permitted_policy`
   check firing on the FIRST real end-to-end APPLIED run, not by a unit
   test written to specifically probe it. Fixed by persisting both fields
   on the `SUBMITTING` transition.
2. **A bare structured `employment_type_raw` value of "Contract" wasn't
   classified**: the CONTRACT signal list only contained compound phrases
   ("contract-to-hire", "contractor", etc.), so a plain ATS field value of
   exactly `"Contract"` fell through to `UNKNOWN` instead of `CONTRACT`.
   Caught immediately by `tests/test_applications_gates.py`'s very first
   run. Fixed by adding the bare token.
3. **Test-design bugs, not product bugs, worth recording**: the first draft
   of the duplicate-application acceptance scenario (G) tried to insert two
   `jobs` rows with the identical `(provider, external_job_id)`, which
   correctly violates the Phase 3 unique index — the test was wrong, not
   the product; fixed by using a different `external_job_id` with the same
   company/title/location (a realistic manual-re-paste duplicate) instead.

### Known Phase 8 limitations

See `docs/phase8-application-executor.md`'s "Honest limitations" section and
`docs/application-operations.md`'s "Worker capabilities" section for the
full list — in short: only Greenhouse has live-verified form discovery
(submission still `ASSIST_ONLY` for it); Lever/Ashby/Workable/
SmartRecruiters/BambooHR/Breezy/Recruitee/Comeet/Teamtailor/Workday are
apply-URL-only; no real ATS submission was implemented or attempted; a
standalone distributed executor-worker daemon was not built (the atomic
claim primitives are implemented/tested, but `queue_application()`/
`process_execution()` run synchronously today via CLI/dashboard, not a
long-running worker loop) — see the recommended Phase 9 list.

## Phase 9 acceptance verification

Verified 2026-08-22.

| Criterion | Evidence |
|---|---|
| Full regression (SQLite) | `pytest -m "not postgres and not browser"`: **692 passed**, 37 deselected |
| Full regression (PostgreSQL, `pgserver`) | `pytest -m postgres`: **33 passed** (4 pre-existing Phase 6/8 + Phase 6/7 postgres files + 5 new `test_applications_postgres_phase9.py` + others), 696 deselected |
| Browser-assist tests | `pytest -m browser`: 4 tests, structurally correct (skip cleanly) — **could not fully execute** in this sandbox: Playwright installs, chromium binary downloads, but launching it fails on a missing system shared library (`libnspr4.so`) that requires root (`playwright install-deps`) to install, which this environment doesn't have. Not a code defect — the skip path itself is exercised and correct. |
| Static/compile check | `python -m compileall app scripts tests`: clean, exit 0 |
| Distributed leasing/duplicate-safety (real Postgres) | `tests/test_applications_postgres_phase9.py` (5 tests): 4 concurrent threads claiming a shared batch of 12 executions never double-claim; 6 threads racing `queue_application()` for the same job produce exactly 1 execution; a full `ApplicationWorker` cycle reaches `APPLIED` against real Postgres; the global hourly rate limit caps submissions across a single worker cycle; **4 real concurrent `ApplicationWorker` instances** processing a shared 16-job batch produce exactly one clean `APPLIED` execution per job with no dangling leases |
| Crash recovery | `tests/test_application_worker_crash_recovery.py` (4 tests): a row artificially left in `SUBMITTING`/`SUBMITTED` (simulating a crash) is resumed as `SUBMISSION_STATUS_UNKNOWN` without a second `submit()` call, twice in a row; a row never claimed past `QUEUED` (crash before submit) is recovered via lease expiry and completes normally; a row left mid-pipeline (`FORM_DISCOVERED`) is also lease-recoverable |
| Worker daemon | `tests/test_application_worker.py` (4 tests): claim→APPLIED via real mock ATS, CAPTCHA→`NEEDS_USER_ACTION` and never re-claimed, ASSIST-mode prep never feeds the submission circuit breaker, drain mode blocks new claims |
| Submission circuit breaker | `tests/test_application_circuit.py` (7 tests): closed-by-default, consecutive-failure trip, half-open probe + recovery, never-permanently-disabled, inflight-slot limit, and explicit proof the submission and discovery breakers are independent for the same provider name |
| Reconciliation evidence pass | `tests/test_application_reconcile_worker.py` (3 tests): genuine mock-ATS server-side evidence auto-resolves to `APPLIED`; genuine absence of evidence auto-resolves to `WITHDRAWN`; a provider without `confirmation_recheck_supported` is left completely untouched |
| Scheduler | `tests/test_application_scheduler.py` (7 tests): off when `APPLICATION_AUTO_PREPARE_ENABLED=false`; queues `ASSIST` by default; queues `AUTO_PERMITTED` only when `AUTO_SUBMIT_ENABLED` AND job eligibility both agree; never double-queues; respects the active-execution guard even against a stale `READY_TO_APPLY` state; respects rate limits; a `CONTRACT` job is never queued |
| Daily budget accounting | `tests/test_application_budget.py` (3 tests): PREPARE-only runs never count as submitted; a confirmed application counts both submitted and confirmed; `NEEDS_USER_ACTION` counted separately from failed |
| Application doctor (Phase 9 checks) | `tests/test_application_doctor_phase9.py` (5 tests): expired lease, orphan lease, duplicate confirmation, multiple simultaneous leases on one job, and a clean-database baseline with zero false positives |
| Attempt history | `tests/test_application_attempts.py` (4 tests): record/list, no secret-shaped columns, bounded history per execution, filter by worker/result |
| Mock ATS expansion | `tests/test_application_mock_ats_expansion.py` (11 tests): login-required, 429, 503, rejection, duplicate-application, multi-page (`total_steps`), conditional sponsorship field, job-removed, form-not-found, and the timeout-before-vs-after-submit evidence distinction |
| Capability matrix | `tests/test_application_capability_matrix.py` (4 tests): every registered provider present, only `mock_ats` claims submission/recheck support |
| Dashboard (Phase 9 pages) | `tests/test_application_dashboard_phase9.py` (6 tests): `/application-workers`, `/applications/capability-matrix`, budget/fleet sections on `/applications`, drain/resume-drain, manual scheduler/reconcile-worker triggers, submission-circuit admin actions |
| Live end-to-end run (ad hoc script, real SQLite) | ingest → `scheduler.run_cycle()` (queued) → `ApplicationWorker._run_cycle()` (APPLIED) → `run_doctor()` (0 serious, 0 warning) → capability matrix rendered → budget/fleet metrics all consistent (`submitted_today=1`, `confirmed_today=1`, `application_provider_circuit_state={'mock_ats': 'CLOSED'}`) |
| Live dashboard verification | Started the real app (fresh temp DB): `/`, `/applications`, `/application-workers`, `/applications/capability-matrix`, `/applications/doctor`, `/metrics`, `/health`, `/readiness`, `/fleet` all returned 200 |
| No secrets/private data committed | `git status`/`git diff` reviewed — no `.env`, `data/app.db`, `candidate_data/profile.json`, or `data/browser_assist_runtime/` staged; `.gitignore` extended for the new browser-assist runtime directory |

### Real bugs this phase caught and fixed

1. **Resumed-mid-submission double-submit risk** in
   `app.applications.executor.process_execution()` — a row left in
   `SUBMITTING`/`SUBMITTED` by a crash had no guard against `submit()` being
   called a second time on resume. Fixed with an explicit early-return to
   `SUBMISSION_STATUS_UNKNOWN`. Caught by this phase's own crash-recovery
   test design, not by an incidental failure.
2. **Postgres `DatatypeMismatch` on `jobs.sponsorship_conflict`** —
   `app.jobs_repo.insert_job`/`update_job` passed a raw Python `bool`
   through to psycopg, which maps it to Postgres's native `boolean` type,
   conflicting with the schema's `INTEGER` column. SQLite silently accepted
   the same code, which is exactly why no prior Postgres test (none of
   which ran a job through the *full* pipeline) had caught it. Fixed with an
   explicit `bool -> int` coercion helper. Caught live by
   `tests/test_applications_postgres_phase9.py`'s very first run.
3. **`app.db_postgres._TABLES_WITHOUT_ID_PK` missing the new
   `application_provider_circuit_state` table** (primary key `provider`, not
   `id`) — its `INSERT ... ON CONFLICT DO NOTHING` was getting an incorrect
   `RETURNING id` appended, raising `UndefinedColumn`. Caught by the same
   Postgres test run immediately after fixing bug 2.
4. **`MockATSProvider.validate()` crashed on `form=None`** — every other
   provider already guarded against `discover_form()` returning `None`;
   the mock never had to until this phase added the `form_not_found`
   scenario. Fixed with the same guard pattern as
   `GenericAssistOnlyProvider`/`LeverApplicationProvider`.
5. **Claimable execution statuses too narrow for real crash recovery** —
   `app.applications.queue._ACTIVE_CLAIMABLE_STATUSES` originally only
   included `QUEUED`, but `process_execution()`'s first write moves status
   to `STARTED` almost immediately, meaning a crash anywhere past that
   point would have left the row permanently unclaimable even after its
   lease expired. Found by design review while implementing the worker
   daemon (before writing the crash-recovery tests, which then confirmed
   the fix), not by a failing test surfacing it after the fact.

### Known Phase 9 limitations

See `docs/phase9-production-application-workers.md`'s "Honest limitations"
section for the full list. In short: `mock_ats` remains the only provider
with `submission_supported=True`; the automated reconciliation pass has
nothing to reconcile against for any real ATS (none expose a
`check_submission_status`-shaped interface); browser-assist is implemented
and unit-testable but could not be executed against a real headless
browser in this sandbox (missing root-only system libraries); the
multi-service Docker Compose demo (now including an `application-worker-1`
service) remains written and YAML-validated but unrun, for the same
Docker-unavailable reason recorded in Phase 6/`docs/deployment-postgres.md`.

## Phase 10 acceptance verification (verified 2026-08-22)

See `docs/phase10-real-ats-assist.md`, `docs/browser-assist-sessions.md`, `docs/real-ats-validation.md`.

| Item | Evidence |
|---|---|
| App starts / dashboard loads | `python -m pytest tests/test_browser_sessions_dashboard.py` (`TestClient`) + manual `uvicorn` startup with the new "Browser assist: ON/OFF" / "Browser mode: VISIBLE/HEADLESS" lines printed |
| Session model + lifecycle | `tests/test_browser_session_model.py` (14 tests): create/get/update, terminal-vs-non-terminal `active` flag, duplicate-active-session rejection, lease claim/release/expiry reclaim, stale-session reaping, summarize() bucket counts |
| Distributed session ownership | `tests/test_browser_session_postgres.py` (2 tests, real PostgreSQL via `pgserver`): migration + partial unique index enforced live, 8 concurrent threads racing `claim_session()` — exactly 1 ever wins |
| Browser-runtime unit tests (no real browser) | `tests/test_browser_runtime_unit.py` (10 tests): disabled-flag/not-installed guards, the `BrowserRuntimeBusy` concurrency bound, registry lifecycle, selector/fingerprint/decline-option helpers |
| Domain allowlist | `tests/test_domain_allowlist.py` (8 tests): known-provider match, unrelated-domain rejection, no wildcard for an unknown provider, same-original-host always allowed, redirect-to-known-domain allowed, redirect-to-unrelated-host rejected |
| Orchestration state machine (mocked runtime) | `tests/test_browser_assist_orchestration.py` (26 tests): FULL_TIME/sponsorship gates (acceptance B/C/H), CAPTCHA/login/legal/unknown-field/platform-restricted pauses, form-changed drift pause, resume live-vs-not-live-vs-crash-recovery (acceptance I/J), multi-step advance, post-manual-submit reconciliation confirming/failing/browser-lost (acceptance K), idempotent duplicate-session start, missing-resume-artifact block |
| **Real Chromium end-to-end** (acceptance A/D/E/F/G/H/K/L) | `tests/test_browser_assist_e2e.py` (11 tests) against the local mock-ATS sandbox (`tests/browser_fixtures.py`) — genuinely launched Chromium in this sandbox via a user-local `apt-get download` + `dpkg-deb -x` + `LD_LIBRARY_PATH` workaround (no root available); skips cleanly with a precise reason in any environment without a launchable Chromium |
| Doctor (Phase 10 checks) | `tests/test_applications_doctor_phase10.py` (8 tests): session-without-execution, non-FULL_TIME/non-eligible-sponsorship session, stale-active-session warning, LIKELY_SPONSOR correctly NOT flagged, confirmation-without-APPLIED, static no-auto-submit-capability scan, forbidden-secret-field text scan, clean-baseline zero-false-positive |
| Metrics | `tests/test_applications_metrics_phase10.py` (3 tests): all-zero on empty DB, correct per-status counts (confirmed sessions correctly excluded from "active"), live-in-process registry count |
| CLI | `tests/test_applications_cli_phase10.py` (3 tests): `browser-start`/`browser-status`/`browser-list`, honest failure for an ineligible job, `browser-reconcile`/`browser-close` |
| Dashboard | `tests/test_browser_sessions_dashboard.py` (6 tests): list/detail pages, start action (success + ineligible-job rejection + missing-execution rejection), close action, 404 for an unknown session |
| Browser-assist capability matrix | `tests/test_browser_capability_matrix.py` (6 tests): valid verification values, greenhouse/lever/ashby correctly `LIVE_FORM_VERIFIED`, no provider ever claims final-submit automation, CLI + dashboard render |
| Scheduled background maintenance | `tests/test_application_background_scheduler.py` (5 tests): clean start/stop, reconciliation pass runs only when enabled, stale-session reap runs only when enabled, neither runs when both disabled, one task failing never stops the other |
| **Real public ATS validation** (bounded, read-only, never submits) | `scripts/phase10_live_validation.py`, run 2026-08-22: Greenhouse (GitLab board, 24 fields, real CAPTCHA observed), Lever (`leverdemo`, 22 fields, real CAPTCHA observed), Ashby (`ashby`, 28 fields, real CAPTCHA observed) all `LIVE_FORM_VERIFIED`; SmartRecruiters honestly `NOT_TESTED` (landing page reached, not the form itself); Workday `NOT RUN` (Phase 3's dogfood tenant now redirects to a maintenance page); Workable `NOT RUN` (no known real tenant) — see `docs/real-ats-validation.md` |
| Full test suite, all three markers | 780 passed (default) + 15 passed (`-m browser`, real Chromium) + 35 passed (`-m postgres`, real PostgreSQL) = **830 total, 0 failures** |
| No secrets/private data committed | `git status`/`git diff` reviewed — no `.env`, `data/app.db`, `candidate_data/profile.json`, `data/browser_assist_runtime/`, or generated resume/session artifact staged; `.gitignore` extended (`runtime/`) |

### Real bugs this phase's own live testing caught and fixed

1. **Domain allowlist rejected every local `file://` fixture and, by the same logic, would have
   rejected any two same-host pages with no netloc.** `is_allowed_host_for_session()` treated an
   *empty current hostname* as automatically unsafe before ever comparing it to the original
   URL's hostname — but `file://` URLs legitimately have an empty hostname on both sides. Every
   one of the first 11 real-Chromium E2E tests failed with `PAUSED_PLATFORM_RESTRICTED` until
   this was fixed to check for an empty **current URL string**, not an empty hostname.
2. **A radio/checkbox group was labeled with its first option's own text, not the question.**
   `Will you now or in the future require sponsorship?` (a real `<fieldset><legend>` structure)
   was detected as label `"Yes"` because the DOM scan preferred a per-element label over the
   fieldset legend uniformly for every field type. Fixed so a **group** prefers its fieldset
   legend over any single option's own choice text (single-value fields keep the opposite,
   already-correct priority). Caught by the conditional-sponsorship-question E2E test against
   real Chromium — the field was silently never filled at all before the fix.
3. **`advance_step()` false-positived a form-changed pause on every intentional step advance.**
   The same fingerprint-drift check `resume_session()` correctly uses to catch an *unexpected*
   form change was also applied after an *intentional* multi-step advance, where the fields are
   supposed to be entirely different. Fixed with a `check_drift=False` path used only by
   `advance_step()`. Would have made every real multi-step form unusable.
4. **A latent bug in the Phase 8 "decline to self-identify" phrase list, inherited by the new
   browser-runtime code.** `DECLINE_TO_SELF_IDENTIFY_PHRASES` listed `"i don t wish to answer"`
   (space where the apostrophe was), but every caller normalizes a candidate's choice string via
   `.replace("'", "")`, which *deletes* the apostrophe rather than replacing it with a space
   (`"don't"` → `"dont"`, not `"don t"`) — so the safe-default match never actually fired for
   the literal phrase `"I don't wish to answer"` anywhere in the codebase (`mock_ats.py`,
   `providers_greenhouse.py`, and the new `browser_runtime.py`). This went unnoticed by existing
   Phase 8 tests only because the specific field those tests exercised (`veteran_q`) happened to
   be optional, so an unfilled field never blocked completion. Fixed by adding the actually-
   produced phrase forms to the shared `app.applications.schema.DECLINE_TO_SELF_IDENTIFY_PHRASES`
   list (both variants kept, defensively, in case a future caller normalizes differently).

### Known Phase 10 limitations

See `docs/phase10-real-ats-assist.md`'s "Honest limitations" section for the full list. In
short: no real production application was submitted, ever; SmartRecruiters' real application
form sits behind an extra "Apply" click this phase's bounded validation didn't follow; the
Phase 3 Workday dogfood tenant is currently offline (not re-verified with a substitute — never
guessed); Workable has no known real tenant to validate against; multi-step navigation was only
live-verified on the local sandbox fixture (the real Greenhouse/Lever/Ashby postings checked
were all single-page); cross-process browser reattachment (keeping a real browser window alive
across a full worker-process restart) is not implemented or claimed — a restarted process
either safely reopens a fresh window (pre-submission) or honestly reports
`SUBMISSION_STATUS_UNKNOWN` (submission may have been in flight).

### Phase 11 real bugs caught live (scripts/phase11_live_validation.py)

1. **Phase 10's final-submit phrase table incorrectly included "apply now."**
   `browser_runtime._SUBMIT_BUTTON_PHRASES` had `"apply now"` alongside genuine final-submit
   phrases — before this phase built a distinct apply-entry concept, this meant a landing page's
   safe "Apply Now" navigation control had no path to being distinguished from a final submit
   action. Fixed by moving apply-entry phrases into their own disjoint table
   (`app.applications.apply_entry.NAVIGATION_SAFE_PHRASES`).
2. **An ungated step-progress regex misread a real on-page date as step progress.** A live run
   against GitLab's genuine Greenhouse posting produced `step_progress_total=31` from an
   unrelated "7/31" date elsewhere on the page — the original `\d{1,2}\s*/\s*\d{1,2}` pattern had
   no context requirement at all. Fixed by requiring a `step`/`progress` keyword within 20
   characters before the numbers (`app.applications.apply_entry.parse_step_progress`).

### Known Phase 11 limitations

See `docs/phase11-ats-flow-hardening.md`'s "Recommended Phase 12" section for the full list. In
short: SmartRecruiters' real posting still has no safely-followable apply-entry control (a
control was found this phase but correctly classified `EXTERNAL_REDIRECT`, never clicked); a
genuinely live Workday tenant (Walmart, found via web search) gave INCONSISTENT apply-entry
results across two runs of the same URL, reported honestly rather than cherry-picked; Workable
was validated live for the first time this phase (a real tenant, 'flosum', found via web search)
but only a single-page form, no multi-step/login/confirmation observed; cross-process browser
reattachment remains unimplemented and unclaimed (unchanged from Phase 10) — Phase 11 only makes
the reconstruction path ownership-safe and countable (`reconstructed_count`), not a claim of true
reattachment.

## Phase 12 acceptance verification (verified 2026-08-22)

- App starts, dashboard loads, all Phase 1-11 functionality unchanged (default `pytest`: 956
  passed, up from Phase 11's documented 856 baseline).
- `pytest -m browser` (real Chromium via a documented non-root library workaround): 38 passed (27
  Phase 11 + 11 new Phase 12), 0 failed.
- `pytest -m postgres` (embedded `pgserver`): 35 passed, matching Phase 11's documented baseline
  exactly.
- `python -m app.applications.cli doctor`: 0 serious, 0 warnings on the real dev database.
- FULL_TIME hard gate and sponsorship gate: unchanged, re-verified via the existing Phase 8-11
  doctor checks (`_check_non_full_time_queued`, `_check_non_confirmed_sponsorship_queued`,
  `_check_browser_session_non_full_time`, `_check_browser_session_non_eligible_sponsorship`), all
  still passing with 0 issues.
- No real final submission was performed at any point during this phase's development or
  validation (verified: `_check_no_browser_auto_submit_capability` and
  `_check_real_provider_capability_auto_without_authorization` both pass; `mock_ats` remains the
  only `submission_supported=True` provider).
- SPA fixture E2E (`tests/test_browser_assist_phase12_e2e.py`, real Chromium): SPA landing
  rendering an Apply control late is detected and safely clicked, a client-side route change is
  detected, a dynamically-mounted multi-step form (including resume upload) is discovered and
  filled; an SPA that never renders times out cleanly (no hang); a genuine "Step 2 of 3" progress
  wizard is parsed EXACT while an unrelated "Posted 7/31" on the same page is never misread as
  progress; multiple same-destination apply controls resolve normally, multiple
  different-destination controls pause `PAUSED_AMBIGUOUS_APPLY_CONTROL`; a same-origin iframe form
  is discovered AND filled (a real bug — fields discovered but not fillable — was caught and fixed
  here); an open shadow-DOM form is discovered and filled; a closed shadow-DOM form is honestly
  `PAUSED_UNSUPPORTED_SUBMISSION`, never bypassed; a job-identity mismatch pauses
  `PAUSED_JOB_IDENTITY_MISMATCH`.
- Trusted redirect tests: `tests/test_trusted_redirects.py` (21 cases, pure/offline) plus a real
  live proof against GitLab's own corporate careers page (10/10 real `job-boards.greenhouse.io`
  links classified `TRUSTED_ATS_REDIRECT`) — see `docs/trusted-ats-redirects.md`.
- Multi-worker/Postgres ownership: unchanged mechanism (`browser_assist_sessions`'s partial
  unique index + `claim_session`'s atomic UPDATE), re-verified passing under real PostgreSQL via
  `tests/test_browser_session_postgres.py` with the new Phase 12 columns present.
- Greenhouse/Workable regression: both re-verified live this phase (see below) with identical or
  improved results — no regression.
- Bounded live validation (`scripts/phase12_live_validation.py`, real network + real Chromium):
  see `docs/real-ats-validation.md`'s Phase 12 update section for the full per-provider table.

### Phase 12 real bugs caught live (scripts/phase12_live_validation.py and tests/test_browser_assist_phase12_e2e.py)

1. **`trusted_redirects.classify_redirect_trust` initially treated `file://` as an unsafe
   scheme.** This project's entire local browser-fixture convention is `file://`-based; a real
   live-Chromium run of the Phase 11 regression suite caught every apply-entry fixture failing
   immediately after this module was wired in. Fixed by adding the same `file://` carve-out
   `app.applications.domain_allowlist` already established.
2. **A field discovered inside an allowed-host iframe could not actually be filled.** The fill
   path always targeted the main page, never the iframe's own `Frame` object. Fixed by tagging
   each iframe-sourced field with its source frame and filling against it directly.
3. **The submit/next-button scan never looked inside an iframe either**, so a form correctly
   discovered and filled inside an iframe still landed on `ACTIVE` instead of
   `READY_FOR_FINAL_SUBMIT`. Fixed by having the iframe scan also locate the submit/next control
   within the same allowed frame.

### Known Phase 12 limitations

See `docs/phase12-spa-ats-hardening.md`'s "Recommended Phase 13" section for the full list. In
short: SmartRecruiters' newer `oneclick-ui` SPA posting shape is protected by an active DataDome
bot-detection CAPTCHA on the one real posting reached this phase — conclusively characterized
(never bypassed), not resolved; whether this CAPTCHA challenge is present on every posting of this
shape or only some remains unknown (a larger, still-bounded sample would be needed); the Walmart
Workday tenant's apply-entry classification remains genuinely `VARIABLE` across 3 repeated
observations this phase — the root cause (A/B page variation vs. hydration timing vs. session
state) is still undetermined; job-identity verification is limited to a confidently-extractable
requisition/posting-id token and reports `UNVERIFIABLE` (not a guess) when no such token exists on
either URL; cross-process browser reattachment remains unimplemented and unclaimed (unchanged from
Phase 10/11).
