# Phase 2 Plan — Autonomous Discovery Agent

Builds on the existing modular monolith (see `docs/architecture.md`) without breaking it.

## New modules

- `app/providers/` — `base.py` (RawJobPosting + JobProvider ABC), `greenhouse.py`, `lever.py`,
  `registry.py` (enabled-provider factory from config). Public, unauthenticated ATS job-board
  APIs only. Each provider isolates per-board/company fetch errors.
- `app/discovery/dedup.py` — stable-ID dedup (provider + external_job_id) with a
  company/title/location fingerprint fallback across providers.
- `app/matching/seniority.py` — title + "N years" extraction; gates Staff/Principal/Architect/
  Director/VP and 7+ year requirements unless JD evidence is compatible with ~3 YOE.
- `app/matching/compensation.py` — gates only on a clearly published max salary < $80k; never
  rejects for unpublished salary. Also extracts salary ranges from free-text JD as a fallback.
- `app/matching/employment_type.py`, `app/matching/geography.py` — discovery-time pre-filters
  (full-time only, US-only), applied before a job is even stored, so manual JD paste (a
  deliberate user action) is never silently skipped by a heuristic.
- `app/agent/state.py` — in-memory agent ON/OFF + last/next cycle status.
- `app/agent/cycle.py` — the 15-step discovery cycle (fetch → normalize → dedupe → classify →
  score → gate → generate → persist → log), isolating per-job errors.
- `app/agent/scheduler.py` — asyncio background loop (`asyncio.to_thread` for the sync cycle),
  started/stopped from FastAPI lifespan, polling `DISCOVERY_INTERVAL_MINUTES`.

## Schema (additive only, via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`)

`jobs` gains: provider, external_job_id, employment_type, salary_min, salary_max, last_seen_at,
freshness_minutes, dedup_fingerprint, score_breakdown (JSON text).
New tables: `discovery_cycles`, `application_state_history`.

## State machine

Add `DISCOVERED, REVIEW_REQUIRED, CLAIM_VALIDATION_FAILED, SKIPPED_NO_SPONSORSHIP,
SKIPPED_SENIORITY, SKIPPED_COMPENSATION, SKIPPED_POOR_MATCH` to `ApplicationState`, keeping
`NEW/ANALYZED/SKIPPED/READY_TO_APPLY/APPLIED/INTERVIEW/REJECTED` for backward compatibility.
Behavior change (explicitly required by this phase's spec, so existing tests are updated to
match): NO_SPONSORSHIP now lands on `SKIPPED_NO_SPONSORSHIP` instead of generic `SKIPPED`;
LIKELY_SPONSOR now lands on `REVIEW_REQUIRED` instead of `READY_TO_APPLY`.

## Config

`.env` (gitignored) + `.env.example`. New knobs: `AGENT_ENABLED` (default false — safe,
opt-in), `DISCOVERY_INTERVAL_MINUTES`, `MAX_JOBS_PER_CYCLE`, `MIN_MATCH_SCORE`,
`FRESHNESS_MAX_DAYS`, `ENABLED_PROVIDERS`, `GREENHOUSE_BOARD_TOKENS`, `LEVER_COMPANY_SLUGS`,
`MIN_SALARY_USD`.

## Testing strategy

Provider tests use `httpx.MockTransport` (built into httpx, no live network, no new
dependency). Discovery-cycle/e2e tests use in-process fake providers returning fixture
`RawJobPosting` lists — deterministic, matches the 5 required smoke-test scenarios.
