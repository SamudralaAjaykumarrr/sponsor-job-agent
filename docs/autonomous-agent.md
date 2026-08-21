# Autonomous Discovery Agent

Phase 2 turns the dashboard from manual-JD-paste-only into an agent that continuously
discovers, analyzes, and prepares applications for fresh U.S. technical jobs, while keeping
every submission decision in the user's hands (ASSIST, never auto-apply).

## Starting the agent

```
./start.sh
```

The server always starts with the background scheduler running, but the discovery loop
itself is **off by default** (`AGENT_ENABLED=false`). Turn it on either:

- via `.env` (copy `.env.example` to `.env`, set `AGENT_ENABLED=true`), or
- from the dashboard: the status bar at the top has a Turn ON / Turn OFF button
  (`POST /agent/toggle`), which flips the in-memory flag immediately without a restart.

Poll `GET /agent/status` for a JSON view of: enabled/running, last/next cycle time, the last
cycle's summary counters, current config, and the last 10 discovery-cycle log rows.

## How discovery works

Each cycle (`app/agent/cycle.py::run_discovery_cycle`) does, per job:

1. **Fetch** raw postings from every enabled provider (`app/providers/`).
2. **Pre-filter** (discovery-time only, before the job is even stored): full-time only
   (`app/matching/employment_type.py`), US-only location (`app/matching/geography.py`), and
   a freshness cutoff (`FRESHNESS_MAX_DAYS`). These are conservative -- they only reject on an
   *explicit* negative signal (e.g. "Internship", "London, UK"); ambiguous/missing data is
   allowed through, matching the "never reject for missing info" rule used for salary.
3. **Dedupe** (`app/discovery/dedup.py`): first by stable ID (provider + external_job_id),
   then by a company/title/location fingerprint to catch the same role cross-posted to
   multiple sources. A job already analyzed on a prior cycle is only `last_seen_at`-touched,
   never re-analyzed or re-packaged -- no duplicate application packages, ever.
4. **Classify + score** via the same pipeline manual ingestion uses
   (`app/pipeline.py::analyze_job`): work arrangement, sponsorship, seniority, compensation,
   technical match, freshness, priority tier/score, and a machine-readable score breakdown
   (`app/scoring/scorer.py::build_score_breakdown`) -- never a fabricated "chance of an
   interview."
5. **Gate**, in order: not-a-target-role -> `SKIPPED`; `NO_SPONSORSHIP` -> hard skip
   (`SKIPPED_NO_SPONSORSHIP`); incompatible seniority -> `SKIPPED_SENIORITY`; published max
   salary under `$MIN_SALARY_USD` -> `SKIPPED_COMPENSATION`; match score under
   `MIN_MATCH_SCORE` -> `SKIPPED_POOR_MATCH`; `UNKNOWN` sponsorship -> stays `ANALYZED`, not
   progressed ("do not apply"); `CONFIRMED`/`LIKELY` sponsor -> proceeds.
6. **Generate** the application package for `CONFIRMED`/`LIKELY` jobs that pass every gate
   (`app/pipeline.py::generate_assist_outputs`), running the claim checker first --
   violations land on `CLAIM_VALIDATION_FAILED` instead of ever reaching output files.
   `CONFIRMED_SPONSOR` -> `READY_TO_APPLY`. `LIKELY_SPONSOR` -> `REVIEW_REQUIRED` (package is
   still generated so the user has something to review, but it is explicitly flagged
   "verify sponsorship before applying" and is never auto-submitted).
7. **Persist + log**: every state change is written to `application_state_history`, and the
   whole cycle's counters (fetched/new/deduplicated/analyzed/confirmed/likely/hard-skips/
   packages/errors/duration) are written to `discovery_cycles` and the app log.

Per-provider and per-job errors are caught and recorded in the cycle's `errors` list; one bad
board or malformed posting never aborts the rest of the cycle.

## Providers

`app/providers/base.py` defines `RawJobPosting` + the `JobProvider` interface. Implemented:

- **Greenhouse** (`app/providers/greenhouse.py`) -- public `boards-api.greenhouse.io` job
  board API, keyed by board token(s) in `GREENHOUSE_BOARD_TOKENS`.
- **Lever** (`app/providers/lever.py`) -- public `api.lever.co/v0/postings` API, keyed by
  company slug(s) in `LEVER_COMPANY_SLUGS`.

Both are public, unauthenticated, read-only APIs -- no login, no CAPTCHA, no anti-bot bypass.
Default example tokens (`gitlab` for Greenhouse, `leverdemo` -- Lever's own public demo
account -- for Lever) are illustrative starting points; verify they're still valid public
boards and edit `.env` to target the companies you actually want to track.

**Limitations**: neither ATS reliably exposes structured salary or explicit sponsorship
fields, so compensation/sponsorship are inferred from free JD text where possible (salary
regex fallback in `app/matching/compensation.py`). LinkedIn/Indeed and any source requiring
login, CAPTCHA-bypass, or anti-bot evasion are explicitly out of scope and will not be added.

### Adding a new provider

See `docs/provider-development.md` for the full Phase 3 checklist (capability model,
hardened HTTP client, pagination safety, required tests). Short version:

1. Subclass `JobProvider` in a new `app/providers/<name>.py`, implement
   `fetch_jobs(self, max_jobs) -> list[RawJobPosting]`, isolate your own per-source errors
   internally (try/except per board/company, log + continue), and declare a
   `ProviderCapabilities` class attribute that matches what the code actually does.
2. Register a factory in `app/providers/registry.py::_PROVIDER_FACTORIES` (and the class in
   `_PROVIDER_CLASSES`).
3. Add the name to `ENABLED_PROVIDERS` and its tenant-list variable in `.env`.

## Phase 3 — expanded provider coverage

Phase 2 shipped Greenhouse + Lever. Phase 3 (`docs/phase3-ats-coverage.md`) added Ashby,
Workable, SmartRecruiters, BambooHR, Recruitee, Breezy HR, Comeet, and Workday (varying
support levels — see `docs/provider-capabilities.md`), plus a provider-detector, a
SQLite-backed company/tenant registry with adaptive per-tenant polling
(`docs/company-registry.md`), and cross-provider dedup with provenance tracking. The
discovery cycle unchanged for the static `ENABLED_PROVIDERS` path described above; it
additionally now polls any `company_registry` tenants that are due, in the same cycle,
with per-tenant failure isolation and health tracking. None of this touches sponsorship
semantics, ASSIST-only behavior, or the hard gates described below.

## Sponsorship semantics (unchanged, strengthened)

See `docs/sponsorship_rules.md`. Historical H-1B filing data is still never proof a specific
role sponsors -- it only ever produces `LIKELY_SPONSOR` (review-only), same as before.

## ASSIST vs AUTO

Default and only implemented mode is **ASSIST**: the agent prepares everything (resume,
job analysis, screener answers, cover letter) and surfaces the exact external application
`url`, but nothing is ever submitted automatically. `AUTO` remains a stub in
`app/models.py::ApplicationMode` for a future `ApplicationProvider` submission interface, to
be built only for platforms whose terms explicitly permit automated submission -- never for
LinkedIn/Indeed or anything requiring CAPTCHA/MFA/anti-bot bypass. Opening the external
application link never marks a job Applied by itself; that's always an explicit manual action.

## Privacy

`candidate_data/`, `data/app.db`, and `.env` are all gitignored. The discovery cycle logs
counts/timings/provider names/error strings only -- never JD text, resume content, or
candidate profile fields.

## What still requires human review

- Every `REVIEW_REQUIRED` job (LIKELY_SPONSOR): verify sponsorship before applying.
- Every `CLAIM_VALIDATION_FAILED` job: a resume claim didn't trace back to the verified
  profile -- check `candidate_data/profile.json` coverage, then use "Regenerate Resume".
- All actual submission: the agent prepares, the user applies.
- Provider/company selection: `GREENHOUSE_BOARD_TOKENS` / `LEVER_COMPANY_SLUGS` are a manual
  choice, not something the agent infers.
