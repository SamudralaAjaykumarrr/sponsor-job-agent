# Architecture

Modular monolith, Python 3.12 + FastAPI. No React/Kafka/Redis/Kubernetes/microservices.

## Modules (app/)

- `config.py` — paths, constants.
- `db.py` — SQLite connection + schema migration (raw sqlite3, no ORM needed for MVP).
- `models.py` — Pydantic models: Job, JobAnalysis, ApplicationAnswers, CandidateProfile.
- `candidate/profile.py` — loads private candidate JSON files from `candidate_data/`, resolves missing facts to `NEEDS_USER_INPUT`.
- `sponsorship/classifier.py` — rule-based classifier: CONFIRMED_SPONSOR / LIKELY_SPONSOR / UNKNOWN / NO_SPONSORSHIP. NO_SPONSORSHIP keywords checked first (hard skip). Likely list is a bundled local reference (`data/known_h1b_sponsors.json`) of employer names — historical sponsorship is NOT proof, so this only ever produces LIKELY_SPONSOR (review-only), never CONFIRMED.
- `workarrangement/classifier.py` — REMOTE / HYBRID / ONSITE from location + JD text.
- `freshness/tracker.py` — computes freshness tier from `published_at` (if reliable) else `first_seen_at`.
- `matching/skills.py` — extracts JD requirement keywords, matches against verified candidate skills/projects/experience, produces match score + gap list.
- `scoring/scorer.py` — combines remote/sponsorship/match/freshness into priority tier per CLAUDE.md ordering. Enforces hard gates (NO_SPONSORSHIP -> SKIPPED, UNKNOWN -> do not apply).
- `resume/generator.py` — builds resume content strictly from verified profile facts, selecting/reordering evidence relevant to the JD. Never invents. Unverified skills become gaps, not claims.
- `resume/claim_checker.py` — validates every generated resume line against the verified profile; blocks unsupported claims.
- `resume/docx_writer.py`, `resume/pdf_writer.py` — render resume.docx / resume.pdf / resume.txt.
- `applications/answers.py` — generates screener answers from verified profile fields; unknown factual answers -> `NEEDS_USER_INPUT`.
- `applications/tracker.py` — persists application state transitions (NEW -> ANALYZED -> READY_TO_APPLY -> APPLIED -> INTERVIEW / REJECTED / SKIPPED).
- `pipeline.py` — orchestrates ANALYZE / ASSIST modes end to end for one ingested JD.
- `main.py` — FastAPI app: manual JD ingestion endpoint, dashboard page (Jinja), filters, job detail, file downloads, state updates.

## Data flow

1. Manual JD ingestion (paste title/company/location/JD text/url/published_at) -> stored as `jobs` row, `first_seen_at = now`.
2. Pipeline runs: work-arrangement classification, sponsorship classification (hard skip if NO_SPONSORSHIP), freshness tier, skills match, priority scoring.
3. If sponsorship in {CONFIRMED_SPONSOR, LIKELY_SPONSOR} and mode is ASSIST: generate resume (docx/pdf/txt), job_analysis.json, application_answers.json, cover_letter.txt, mark READY_TO_APPLY (LIKELY_SPONSOR jobs are still flagged review-only in the UI even though files are prepared, per "review only" rule).
   - UNKNOWN sponsorship -> analyzed but NOT progressed to resume generation ("do not apply").
   - NO_SPONSORSHIP -> SKIPPED immediately, no further processing.
4. Dashboard lists/filters jobs; lets user change application_state (APPLIED/INTERVIEW/REJECTED) manually — no auto-submission anywhere.

## Storage layout

- `data/app.db` — SQLite.
- `data/known_h1b_sponsors.json` — bundled reference list (small, illustrative; not authoritative).
- `candidate_data/` — private candidate facts (gitignored). Missing fields = `"NEEDS_USER_INPUT"`.
- `output/<job_id>/` — resume.docx, resume.pdf, resume.txt, job_analysis.json, application_answers.json, cover_letter.txt (when useful).

## Explicitly out of scope

No LinkedIn/Indeed automation, no CAPTCHA/MFA/rate-limit/anti-bot bypass, no auto-apply (AUTO mode is a stub only).

## Phase 2 — autonomous discovery agent

See `docs/autonomous-agent.md` for full detail. Summary of what was added on top of the
MVP above, without breaking it (all 45 original tests still pass unmodified in behavior,
except two intentional state-machine renames noted below):

- `app/providers/` — `JobProvider` interface + `RawJobPosting`, plus `greenhouse.py` and
  `lever.py` connectors (public, unauthenticated ATS job-board APIs) and `registry.py`
  (enabled-provider factory driven by `ENABLED_PROVIDERS` config).
- `app/discovery/dedup.py` — stable-ID (provider + external_job_id) dedup with a
  company/title/location fingerprint fallback across providers.
- `app/matching/seniority.py`, `compensation.py`, `employment_type.py`, `geography.py` —
  new gates. Seniority/compensation/match-score run inside `pipeline.analyze_job` (apply to
  both manual and autonomous ingestion). Employment-type/geography are discovery-time
  pre-filters only (`app/agent/cycle.py`), applied before a job is even stored, so a
  manually-pasted job — a deliberate user action — is never silently skipped by a heuristic.
- `app/agent/state.py`, `cycle.py`, `scheduler.py` — in-memory agent status, the 15-step
  discovery cycle, and an asyncio background loop (runs the sync cycle via
  `asyncio.to_thread`, started/stopped from FastAPI's `lifespan`).
- `jobs` table gained additive columns (provider, external_job_id, employment_type,
  salary_min/max, last_seen_at, freshness_minutes, dedup_fingerprint, score_breakdown JSON) —
  applied via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`, never destroying existing rows.
  New tables: `discovery_cycles` (per-cycle log), `application_state_history` (every state
  transition, manual or automatic).
- `ApplicationState` gained `DISCOVERED, REVIEW_REQUIRED, CLAIM_VALIDATION_FAILED,
  SKIPPED_NO_SPONSORSHIP, SKIPPED_SENIORITY, SKIPPED_COMPENSATION, SKIPPED_POOR_MATCH`,
  keeping `NEW/ANALYZED/SKIPPED/READY_TO_APPLY/APPLIED/INTERVIEW/REJECTED` for backward
  compatibility. **Two intentional behavior changes** (explicitly required by the Phase 2
  spec, existing tests updated to match): `NO_SPONSORSHIP` now lands on the specific
  `SKIPPED_NO_SPONSORSHIP` instead of generic `SKIPPED`; `LIKELY_SPONSOR` now lands on
  `REVIEW_REQUIRED` instead of `READY_TO_APPLY` (the package is still generated, just never
  presented as ready-to-submit — matches "LIKELY_SPONSOR -> review only, do not auto-submit").
- `app/scoring/scorer.py::build_score_breakdown` — machine-readable per-component reasons
  (never a fabricated "probability of interview"), stored as JSON on the job row and shown
  on the job detail page.
- Dashboard: agent ON/OFF status bar with last/next cycle + counters, `Fresh <6h` filter,
  `Review Required` filter, job-detail score breakdown table, pipeline history, and
  Regenerate Resume / Open Application actions.

## Phase 3 — ATS coverage expansion + provider infrastructure

See `docs/phase3-ats-coverage.md` for the full writeup. Summary of what was added on top
of Phase 2, without weakening any existing safety rule (all 87 Phase 2 tests still pass
unmodified):

- `app/providers/capabilities.py` — `ProviderCapabilities`/`SupportLevel`, the single
  source of truth every connector declares (`docs/provider-capabilities.md`).
- `app/providers/http_client.py`, `concurrency.py` — centralized bounded
  timeout/retry/backoff/response-size-cap HTTP behavior and bounded per-provider
  concurrency, used by every connector.
- 11 new connectors (`ashby.py`, `workable.py`, `smartrecruiters.py`, `bamboohr.py`,
  `recruitee.py`, `breezy.py`, `comeet.py`, `workday.py`, `unsupported.py` for
  Teamtailor/Jobvite/Pinpoint/JazzHR/iCIMS/Oracle).
- `app/providers/detector.py` — URL → likely-ATS detection with confidence + tenant
  extraction, never overclaiming certainty.
- `app/registry/` (`models.py`, `repo.py`, `scheduling.py`) — SQLite-backed company/tenant
  registry with deterministic adaptive polling and health computation
  (`docs/company-registry.md`); foundation for Phase 4's mass importer.
- `app/discovery/dedup.py::canonicalize_url` — URL canonicalization (strips tracking
  params, normalizes host/trailing-slash) for cross-provider dedup; fingerprint fallback
  now only applies when a job has no URL at all, to avoid wrongly merging two distinct
  requisitions that happen to share title/company/location text.
- `app/jobs_repo.py::record_provenance`/`list_provenance` — every source a job was
  discovered from is retained (new `job_provenance` table), even after dedup.
- New tables (all additive): `company_registry`, `job_provenance`, `discovery_log`.
  `jobs` gained additive columns for the expanded normalized model (`company_identifier,
  city, state, country, remote_status, department, team, office, source_url,
  canonical_url, salary_currency, salary_period, provider_metadata, freshness_source`).
  `discovery_cycles` is now allocated at cycle start and finalized at the end
  (`start_discovery_cycle`/`finalize_discovery_cycle`) so per-tenant provenance/log rows
  can reference the in-progress cycle.
- `app/agent/cycle.py::run_discovery_cycle` now runs two phases per cycle: the unchanged
  Phase 2 static-config path, then a registry-driven adaptive-polling path
  (`_discover_from_registry`) that processes every due `company_registry` tenant, isolates
  per-tenant failures (one failing tenant never blocks another), and records
  latency/yield/health per tenant.
- Dashboard: `/providers` (capability matrix + live tenant health), `/registry`
  (per-tenant table + add-entry form + provider filter), `/discovery-log` (JSON), and a
  "Source provenance" section on the job detail page.
