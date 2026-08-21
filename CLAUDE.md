# Sponsor Job Agent — Master Build Specification

Build a local AI-assisted U.S. job application system for a software engineer with about 3 years of backend/software engineering experience.

Main goals:

- Find fresh U.S. technical jobs
- Prioritize Remote > Hybrid > Onsite
- Process only CS/STEM-related jobs
- Require H-1B sponsorship compatibility
- Prefer explicit sponsorship
- Allow likely sponsors only for review
- Hard-skip jobs that say no sponsorship
- Analyze every JD
- Generate a new truthful resume for each JD
- Use only verified candidate experience and skills
- Never invent skills, employers, metrics, years of experience, certifications, or immigration details
- Generate DOCX and PDF resumes
- Generate screener-answer suggestions
- Track applications
- Build a local FastAPI dashboard
- Default mode should be ASSIST, not blind auto-apply

## Sponsorship Rules

CONFIRMED_SPONSOR:
The job or employer explicitly says sponsorship is available.

LIKELY_SPONSOR:
Employer has recent H-1B history but the specific job does not explicitly confirm it.

UNKNOWN:
Not enough evidence.

NO_SPONSORSHIP:
The job says no sponsorship, unable to sponsor, or must not require sponsorship now or in the future.

Rules:

- CONFIRMED_SPONSOR -> eligible
- LIKELY_SPONSOR -> review only
- UNKNOWN -> do not apply
- NO_SPONSORSHIP -> hard skip

Historical sponsorship alone is not proof that a specific role sponsors.

## Job Priority

Highest priority:

Remote + confirmed sponsor + strong technical match

Then:

Remote + likely sponsor
Hybrid + confirmed sponsor
Hybrid + likely sponsor
Onsite + confirmed sponsor
Onsite + likely sponsor

A remote job with no sponsorship must still be skipped.

## Target Roles

Primary:

- Software Engineer
- Software Engineer II
- Backend Engineer
- Backend Software Engineer
- Python Engineer
- Python Developer
- API Engineer
- Platform Engineer
- Cloud Software Engineer
- Application Engineer

Secondary if strongly related to the candidate's background:

- DevOps Engineer
- Cloud Engineer
- Infrastructure Engineer
- SDET
- QA Automation Engineer
- Systems Engineer
- Data Platform Engineer

Do not process unrelated non-STEM jobs.

## Resume Rules

For each strong job:

JD
-> extract requirements
-> match verified candidate evidence
-> select strongest relevant experience
-> select strongest projects
-> reorder skills
-> rewrite truthful bullets for the JD
-> check all claims
-> generate DOCX/PDF/text resume

Never fabricate anything.

Every material resume claim must be supported by the verified candidate profile.

If a skill is not verified, mark it as a gap instead of claiming it.

## Freshness

Track:

- published_at when reliable
- first_seen_at always

Priority:

- 0–60 minutes: maximum
- 1–3 hours: very high
- 3–12 hours: high
- 12–24 hours: moderate
- older: lower

## Candidate Data

Create private candidate files for:

- contact information
- employment history
- skills
- projects
- education
- work authorization
- sponsorship requirement
- relocation preference
- salary preference
- standard application answers

Missing personal facts must become:

NEEDS_USER_INPUT

Do not guess them.

## Application Modes

ANALYZE:
analyze only

ASSIST:
generate everything and mark READY_TO_APPLY

AUTO:
future use only for interfaces where automation is explicitly permitted

Default mode:

ASSIST

Do not automate LinkedIn or Indeed submissions.

Do not bypass CAPTCHA, MFA, authentication, rate limits, or anti-bot protections.

## Tech Stack

Use:

- Python 3.12
- FastAPI
- Pydantic
- SQLite
- Jinja
- httpx
- python-docx
- ReportLab or equivalent
- pytest

Use a modular monolith.

Do not add React, Kafka, Redis, Kubernetes, or microservices for the MVP.

## Dashboard

Show:

- company
- role
- location
- remote/hybrid/onsite
- freshness
- sponsorship status
- technical match
- overall priority
- application state

Filters:

- Remote
- Hybrid
- Onsite
- Confirmed Sponsor
- Likely Sponsor
- Fresh < 1 hour
- High Priority
- Ready To Apply
- Applied
- Interview

## Output Per Job

Create:

- resume.docx
- resume.pdf
- resume.txt
- job_analysis.json
- application_answers.json
- cover_letter.txt when useful

Track exactly which resume belongs to which application.

## Acceptance Criteria

Do not claim the project is complete unless:

- app starts
- dashboard loads
- manual JD ingestion works
- sponsorship classification works
- no-sponsorship jobs are skipped
- work arrangement classification works
- freshness tracking works
- scoring works
- high-priority jobs are identified
- tailored resume generation works
- DOCX works
- PDF works
- unsupported claims are blocked
- application answers work
- unknown factual answers become NEEDS_USER_INPUT
- tracking works
- tests pass
- ./start.sh works
- no secrets are committed

## Build Behavior

First create planning documents under docs/.

Then immediately continue into implementation.

Do not stop after planning.

Create files, install normal dependencies, run tests, fix failures, and continue until the MVP works.

Do not ask routine coding questions.

Only stop if a genuinely personal factual answer is required.

Never report a feature as working unless it was actually tested.

## Provider Architecture Rules (recorded after Phase 3, apply to all future phases)

These are durable rules the provider/discovery-infrastructure layer must keep obeying as
coverage scales toward Phase 4's 10,000–100,000+ tenant registry:

- Every provider connector declares a `ProviderCapabilities` (`app/providers/capabilities.py`)
  that must match what the code actually does. A provider is FULL/PARTIAL/EXPERIMENTAL only
  if its discovery has been implemented and tested; otherwise it is UNSUPPORTED with a
  documented reason. Never inflate a support level to look more complete than it is.
  `submission_supported` is always `False` — this layer discovers jobs, it never submits.
- Only public, unauthenticated, ToS-respecting interfaces are implemented. No CAPTCHA
  bypass, no anti-bot circumvention, no credential theft, no stealth browsing, no
  auth/rate-limit evasion, no falsified headers/tokens — for any provider, at any scale.
- Cross-provider dedup checks stable provider ID first, then canonical URL, and falls back
  to a company/title/location fingerprint **only when a job has no URL at all** — a
  fingerprint match must never override a mismatched stable ID or canonical URL, since two
  genuinely different requisitions can share identical title/company/location text.
- A provider/tenant must never fabricate a field it doesn't actually expose (e.g. a relative
  "Posted 3 days ago" string is not a timestamp) — leave it null and let downstream logic
  (freshness fallback to `first_seen_at`, sponsorship `UNKNOWN`) handle the gap safely.
- One failing tenant/provider must never abort discovery for any other tenant/provider in
  the same cycle.

## Registry Architecture Rules (recorded after Phase 4, apply to all future phases)

- A `registry_portals` row only becomes `VERIFIED`/`ACTIVE` — and only then gets mirrored into
  the operational `company_registry` table that the discovery cycle actually polls — after the
  live verification pipeline (`app/registry/verification.py`) confirms it. Bulk import
  (`app/registry/importers.py`) and page discovery (`app/registry/page_discovery.py`) may only
  ever produce `DISCOVERED`/`CANDIDATE` rows. Never skip verification to inflate registry counts.
- Verification's structural probe (`app/registry/probe.py`) must raise on failure rather than
  swallow it, unlike `JobProvider.fetch_jobs()` (which deliberately isolates per-tenant errors
  for the discovery cycle's sake) — these are different concerns and must stay separate
  mechanisms; do not collapse them back into one.
- Permanent failures (400/401/403/404/410) and temporary ones (429/5xx/timeout/connection) are
  classified separately. Only permanent failures, repeated past
  `REGISTRY_STALE_AFTER_PERMANENT_FAILURES`, ever demote a portal (`STALE`/`QUARANTINED`).
  Temporary failures are recorded but never count toward that threshold — never permanently
  discard a portal over one bad network moment.
- A company-identity mismatch observed during verification quarantines a portal
  (`AMBIGUOUS` → `QUARANTINED`); it never gets silently ACTIVE-ed anyway.
- An ATS-migration record (`registry_migrations`) is only created when an existing portal is
  already `STALE` on a different provider than a newly `VERIFIED`/`ACTIVE` one for the same
  company — a company legitimately running two ATSes at once (both healthy) must never produce a
  false migration record.
- Every registry list/query is bounded (`LIMIT` + keyset pagination) — never `SELECT *` over the
  whole table, regardless of registry size. Synthetic benchmark data (`scripts/
  registry_benchmark.py`) must only ever be written to an isolated temp DB, never the real
  registry, and must use a provider name (`benchmark-fixture`) that can never collide with a
  real one.
- Company/portal identity dedup always requires domain (or an explicit provider+tenant pair) —
  normalized *name* alone is never sufficient to merge two registry companies.

## Distributed Polling / Worker Fleet Rules (recorded after Phase 5, apply to all future phases)

These are durable rules the Phase 5 distributed polling execution layer (`app/workers/`) and its
future replacements/extensions must keep obeying:

- Never claim "monitoring N portals" anywhere (docs, dashboard, reports) unless real, timestamped
  attempt history proves those N portals were actually polled within their expected schedule.
  Allowed wording: "registry contains N portals" / "N verified" / "N active" / "N successfully
  polled in the last 24h" / "monitoring coverage: X%". Storing a row is never the same as
  monitoring a company. See `docs/scaling-claims.md`.
- A portal/verification-queue item is claimed via a single atomic `UPDATE ... WHERE
  (unleased OR lease-expired)` — never a read-then-write pattern from application code, and never
  an in-memory-only lock. Correctness comes from SQLite's own single-writer serialization (WAL
  mode + `busy_timeout`, both must stay configured on every connection); do not reintroduce a
  separate application-level locking mechanism on top of it.
- A worker crash must never require crash-detection logic. The only mechanism that recovers a
  crashed worker's held-but-abandoned lease is the lease's own `lease_expires_at` passing —
  do not add a heartbeat-based "is this worker alive" check as an alternative path.
- The network call (the actual HTTP request to a provider) must always happen OUTSIDE any
  `db_session()` transaction — never hold a DB write transaction open across a network call.
- Sharding (`REGISTRY_SHARD_COUNT`/`REGISTRY_SHARD_INDEX`) is applied by filtering candidates in
  Python before the claim `UPDATE`, using the existing deterministic
  `app.registry.sharding.shard_for_portal` hash — never add a second, different sharding scheme.
- A claimed work item that is skipped without ever being attempted (circuit open, provider at its
  concurrency limit) must get a short cooldown (lease extended), never an outright lease release —
  releasing outright causes a busy-spin of claim/cancel/reclaim across multiple workers/threads
  sharing one provider's tight concurrency budget. This was a real bug caught during Phase 5's own
  local multi-worker acceptance testing; do not revert to bare release-on-cancel.
- Every poll/verification attempt must result in exactly one recorded `poll_attempts` row and the
  lease being released (or intentionally extended, never left indeterminate) — including on a
  wholly unanticipated exception. Any new code path added to `app/workers/runner.py` must be
  wrapped so this remains true; a stranded lease with no attempt record is exactly the failure
  mode the outer safety-net `except Exception` blocks exist to prevent (a real one was caught live
  during this phase's own validation: `ResponseTooLargeError` escaping `GreenhouseProvider.
  fetch_jobs()`'s own internal error isolation for an unusually large real board).
- The circuit breaker must never permanently disable a provider — it always eventually returns to
  allowing a HALF_OPEN probe attempt after `CIRCUIT_BREAKER_COOLDOWN_SECONDS`, regardless of how
  many times it has tripped before.
- Dead-lettering only ever triggers on CONSECUTIVE PERMANENT failures reaching
  `DEAD_LETTER_MAX_ATTEMPTS` — transient failures (retryable) never count toward that threshold,
  matching the same permanent-vs-temporary distinction Phase 4's registry lifecycle already
  established. Never auto-requeue a dead-lettered item; requeuing is always an explicit operator
  action.
- Schema-drift detection (a structurally wrong/missing response field) must remain distinct from
  an empty board (a structurally valid, empty response) in both attempt history and portal
  health — never conflate the two, and never quarantine/disable a portal for schema drift alone.
- `app/workers/queue.py::WorkQueue` (`claim_due_work`/`ack`/`retry`/`fail`/`extend_lease`) is the
  only interface the worker runner and pipeline code may depend on for queue operations — never
  let `app/workers/runner.py` or any provider/pipeline code reference SQLite-specific locking
  details directly; that keeps a future PostgreSQL/Redis/SQS swap possible without touching them.
- Synthetic benchmark data (`scripts/worker_benchmark.py`) must only ever be written to an
  isolated temp DB, never the real registry, and must use the provider name `benchmark-fixture`
  (matching `scripts/registry_benchmark.py`'s existing convention) that can never collide with a
  real provider.