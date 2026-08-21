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
