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
  "What remains" for the full list.
