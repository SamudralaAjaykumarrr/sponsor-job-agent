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

## Production-Scale Distributed Architecture Rules (recorded after Phase 6, apply to all future phases)

- `app/db.py` (SQLite) and `app/db_postgres.py` (PostgreSQL) share ONE schema
  (`app.db.SCHEMA`, SQLite DDL, mechanically translated for Postgres by
  `_translate_schema_for_postgres`) — never hand-maintain two separate schema
  definitions that can drift apart. New tables/columns going forward are added via
  `app/migrations.py`'s versioned migration list, not by editing the Phase 1-5 baseline
  SCHEMA string in place.
- Every schema-DDL sequence against Postgres (`db_postgres.init_db()`, `migrations.run_pending()`
  in Postgres mode) must run inside the session advisory lock
  (`acquire_schema_lock`/`release_schema_lock`) — concurrent, unserialized DDL from multiple
  processes against a shared Postgres database is a real, reproduced deadlock/UniqueViolation
  hazard (caught live during Phase 6's own multi-worker validation), not a hypothetical one.
- Timestamps are stored as ISO-8601 TEXT in both backends, never a native TIMESTAMP/TIMESTAMPTZ
  column, and boolean flags stay INTEGER (0/1) in both backends — this is a deliberate,
  permanent choice to keep the two backends behaviorally identical, not a temporary shortcut.
- `?`-style placeholders are the only paramstyle used in application SQL; `app/db_postgres.py`'s
  translation to `%s` assumes no SQL string anywhere contains a literal `?` character in string
  data. If a future query ever needs a literal `?`, it must not go through the shared
  `conn.execute()` path unexamined.
- `app/workers/leasing.py` remains the only public leasing interface; `app/workers/
  leasing_postgres.py` is an internal dispatch target, never called directly by worker/pipeline
  code. A Postgres SKIP LOCKED claim must never overfetch more candidate rows than it can
  plausibly need (a flat multiplier unrelated to sharding caused a real worker-starvation bug,
  fixed by `_select_limit`) — overfetching under SKIP LOCKED locks rows it never uses, which
  starves other concurrent claimers rather than merely wasting work.
- `app/providers/errors.py`'s `ProviderFetchResult`/`fetch_jobs_result()` is the only sanctioned
  way to get a typed, non-swallowed outcome out of a provider — `fetch_jobs()` itself must never
  be changed to raise or otherwise alter its existing swallow-and-return-`[]` contract, since the
  static multi-tenant discovery path (`app.agent.cycle`) still depends on that isolation.
  A provider's per-tenant fetch `except` block may stash `self._last_error = exc` (already done
  for every FULL/PARTIAL provider) but must never re-raise.
- `provider_schema_drift` never stores raw response payloads — only a structural signature
  (hash of the shape-check's descriptive detail string) plus small bounded text fields. Drift
  affecting many DISTINCT tenants of one provider may feed the circuit breaker; drift for a
  single tenant must never, by itself, disable that portal or trip the breaker.
- `employer_sponsorship_evidence` (`app/sponsorship/evidence.py`) must never be imported by
  `app.sponsorship.classifier` or any code path that sets a job's `sponsorship_status`. It is a
  storage foundation for future sponsorship intelligence and may only ever influence acquisition
  *priority* (`app/registry/acquisition_priority.py`), never a specific job's confirmed/likely/
  unknown sponsorship determination.
- `app/registry/acquisition_priority.py`'s scoring inputs must never include an interview-
  probability-shaped field — acquisition priority answers "worth verifying/polling sooner", not
  "likely to lead to a job offer".
- The orphan worker reaper (`app/workers/reaper.py`) only ever changes a worker's own `status`
  column to `OFFLINE` — it must never touch a lease directly. Lease recovery is, and must remain,
  driven exclusively by `lease_expires_at`/`verify_lease_expires_at` passing, independent of
  whether the reaper ever runs.
- `/health` must never touch the database (liveness only); `/readiness` is the only endpoint that
  checks database reachability/schema compatibility. Never conflate the two.
- Structured logging's correlation fields are an explicit allowlist
  (`app/observability/logging_config.py::_STRUCTURED_FIELDS`) — a field must be added to this
  allowlist deliberately, never passed through implicitly, and no field name resembling
  candidate PII (email/phone/resume/password/ssn/dob) may ever be added to it.
- Synthetic benchmark/simulation data introduced in Phase 6 (`scripts/phase6_scale_benchmark.py`,
  `scripts/multi_machine_simulation.py`) follows the same isolated-temp-DB-only,
  never-collide-with-a-real-name convention as Phase 4/5's benchmarks (`benchmark-fixture`,
  `simulated-provider-fixture`) — never write synthetic rows into a real registry or a
  developer's real `data/app.db`.

## Sponsorship Intelligence Rules (recorded after Phase 7, apply to all future phases)

Core rule, unchanged from the Phase 7 build brief and now durable: historical sponsorship
evidence may only ever answer "is this employer worth prioritizing/reviewing?", never "does
this specific current role sponsor?". A specific job may only become CONFIRMED_SPONSOR from
current-role/current-JD evidence.

- `app/sponsorship/classifier.py` (`classify_sponsorship`/`classify_sponsorship_detailed`)
  remains current-role-only — it must never import `app.sponsorship.evidence`, `.profile`,
  `.identity`, or `.decision`. This narrows and supersedes the blanket Phase 6 wording
  ("historical evidence may only ever influence acquisition priority, never a job's
  sponsorship_status") for exactly one new, deliberately sanctioned integration point:
  `app.sponsorship.decision.decide_sponsorship()`/`persist_decision()`, the only code path
  allowed to blend historical evidence into a job's sponsorship_status, and even there it may
  only ever upgrade UNKNOWN → LIKELY_SPONSOR — never produce CONFIRMED_SPONSOR, never override
  NO_SPONSORSHIP, never downgrade CONFIRMED_SPONSOR. `app/pipeline.py::analyze_job()` calls
  `persist_decision()`, not `classify_sponsorship()`, as its sponsorship step.
- A same-JD conflict (both positive and negative sponsorship language present) always resolves
  to LIKELY_SPONSOR with `conflict=True` and a blocking reason — never a hard skip, never
  CONFIRMED. Conditional/case-by-case language alone also resolves to LIKELY_SPONSOR
  (`conditional=True`), never CONFIRMED.
- `sponsorship_decisions` is append-only and versioned per job — a reclassification always
  inserts a new row with `decision_version` incremented; prior decisions are never overwritten.
  A new version is only written when the JD fingerprint (or classifier version) actually
  changed; re-persisting unchanged input is a no-op read.
- A job in a terminal, human-driven `application_state` (APPLIED/INTERVIEW/REJECTED) must never
  have that state silently changed by a later JD edit — `app/pipeline.py::reanalyze_job()`
  still computes and records the new decision for audit history, but leaves `application_state`
  untouched for terminal jobs.
- Employer identity resolution (`app/sponsorship/identity.py`) never merges two companies on
  name similarity alone — only an exact normalized-name+domain match, a verified alias match,
  or an unambiguous (single-candidate) normalized-name-only match resolves automatically.
  Anything ambiguous is written to `employer_identity_review` for manual resolution, never
  force-matched.
- `company_relationships` (parent/subsidiary/affiliate/acquired) is metadata for display and
  doctor contradiction-checking only — evidence/profile aggregation in
  `app.sponsorship.profile` always scopes strictly to one `company_id`; a relationship never
  transfers sponsorship history between the two companies it links.
- `employer_sponsorship_profile` is a cached, recomputed-on-import aggregate
  (`app.sponsorship.profile.refresh_employer_profile`) — job/company classification must never
  scan raw `employer_sponsorship_evidence` rows on a live request path (see Phase 6's
  performance rule, reaffirmed). `history_score`/`historical_strength` are relative ranking
  signals, never a "probability of sponsorship" — never label them that way anywhere (code,
  docs, UI).
- Any query filtering `employer_sponsorship_evidence` on `(dataset_id, source_record_id)` for
  idempotency must include the literal `AND source_record_id != ''` clause matching the partial
  unique index's own WHERE condition — SQLite cannot prove a bound parameter satisfies a partial
  index's `!=` condition without it, and omitting it silently degrades to a full table scan
  (a real O(n²) bug caught live during this phase's own benchmark; see
  `docs/phase7-sponsorship-intelligence.md`).
- Government dataset importers (`app/sponsorship/importers.py`) only ever read an
  already-downloaded local file — never perform a live network download themselves. Import must
  stay streaming (never load a whole file into memory), batched (one transaction per batch, not
  per row), idempotent (safe to re-run the identical file), and resumable via
  `sponsorship_datasets.resume_cursor`. A dataset never fabricates a field its source format
  doesn't actually provide (e.g. USCIS's public Employer Data Hub has no occupation field —
  `occupation_code`/`occupation_title` stay blank for those rows, never guessed).
- `employer_sponsorship_evidence` must never store beneficiary/worker names or other
  immigration-filing personal data — only employer/role/location/aggregate fields.
- `app.sponsorship.acquisition_integration.sync_acquisition_signal()` may only ever write the
  single `registry_companies.has_sponsorship_history_signal` boolean column — it must never
  recompute or overwrite `priority_score`/`priority_reasons` itself (those need portal-level
  inputs this module doesn't own); a company with no sponsorship history must never be starved
  by this signal, only ever additively boosted when present.
- Synthetic benchmark data (`scripts/sponsorship_benchmark.py`) follows the same isolated-temp-
  DB-only, never-collide-with-a-real-name convention as every prior phase's benchmark
  (`benchmark-fixture` dataset name) — never write synthetic rows into the real registry or a
  developer's real `data/app.db`.

## Application Executor Rules (recorded after Phase 8, apply to all future phases)

**No automated application submission may occur unless the job has been positively classified
as FULL_TIME.** `app.matching.employment_type.classify_employment_type()` must return exactly
`EmploymentType.FULL_TIME` — `UNKNOWN` is never treated as FULL_TIME for submission purposes,
even though it is still allowed to enter the queue for ASSIST-only preparation. This is the
first, unconditional check in `app.applications.eligibility.evaluate_executor_eligibility()`
and must remain first; no downstream executor code path may bypass it.

- `app.applications.provider.ApplicationProvider` (submission/form-filling) and
  `app.providers.base.JobProvider` (discovery) are permanently separate interfaces — a provider
  fully supported for discovery may be `UNSUPPORTED`/`ASSIST_ONLY` for application (this is
  true today for Lever, live-verified: its public API exposes no structured question schema).
  `submission_supported=True` may only ever be set on a provider that has been genuinely tested
  end-to-end; as of Phase 8 that is only the deterministic in-process `MockATSProvider` — no
  real ATS adapter may set it without the same bar of actual, tested, explicitly-permitted
  submission automation.
- `AUTO_PERMITTED` execution mode may only actually submit when ALL of: FULL_TIME (positive),
  `sponsorship_status == CONFIRMED_SPONSOR`, resume claim-check passed, the selected provider's
  `automation_policy == PERMITTED_AUTO` for this specific draft, no CAPTCHA/MFA/auth-required
  flag, no unresolved required field (especially `LEGAL_ATTESTATION`/`DEMOGRAPHICS`, which are
  never guessed — see `app.applications.schema` and `app.applications.mapping`'s
  `_STRICT_FIELD_IDS`), no duplicate, and `AUTO_SUBMIT_ENABLED=true`. Any single failed
  condition falls back to ASSIST/`NEEDS_USER_ACTION` — never a partial or "best effort" submit.
  There is no generic "force submit" flag anywhere in `app/applications/`.
- `application_executions(job_id) WHERE active=1` is a partial UNIQUE index — the actual,
  atomic, cross-worker guard against two concurrent executions for the same job (CLAUDE.md
  section 61). `app.applications.repo.create_execution()` observes this as
  `DuplicateExecutionError`; never replace this with an application-level check-then-insert.
  `active` flips to 0 only when `app.applications.models.TERMINAL_STATUSES` is reached —
  `SUBMISSION_STATUS_UNKNOWN`/`NEEDS_USER_ACTION` deliberately stay `active=1` so a second
  concurrent execution attempt is blocked while a human resolves them too.
- A job is marked `APPLIED` only via `app.applications.repo.update_execution()` reaching
  `ExecutionStatus.APPLIED`, which requires a `ConfirmationResult.confirmed=True` from
  `ApplicationProvider.verify_confirmation()` — a `submit()` success alone is never sufficient
  (see `SUBMITTED` vs `APPLIED` in the state machine). A submission whose outcome could not be
  determined (timeout, dropped connection) becomes `SUBMISSION_STATUS_UNKNOWN` and is never
  auto-retried; `app.applications.reconcile.reconcile_execution()` is the only path that resolves
  it, and it is always an explicit human/operator action.
- `app.applications.eligibility.evaluate_executor_eligibility()` re-derives every check
  independently of whatever `jobs.application_state` the pipeline already computed — this is
  deliberate defense in depth, not redundant code to be simplified away.
- `jobs.application_state` stays the coarse, dashboard-facing summary; the fine-grained,
  versioned execution status machine lives only in `application_executions.status`
  (`app.applications.models.ExecutionStatus`), mirrored onto the job row by
  `app.applications.repo.mirror_job_state()`. Do not repurpose an existing Phase 1-7
  `ApplicationState` value for executor mechanics, and do not write `ExecutionStatus` values
  directly into `jobs.application_state`.
- `APPLICATION_EXECUTOR_ENABLED` and `AUTO_SUBMIT_ENABLED` both default to `false` and are
  independent switches — discovery/analysis/resume generation must never be gated by either one.
  `app.applications.executor.queue_application()` raises rather than silently no-op-ing when the
  relevant flag is off, so a caller can show an honest message instead of jobs quietly never
  progressing.
- Rate limits (`MAX_APPLICATIONS_PER_HOUR`/`_PER_DAY`/`_PER_COMPANY_PER_DAY`) are enforced by
  querying `application_audit_log`'s `submit_attempted` events directly against the shared
  database (`app.applications.rate_limit`) — already fleet-wide the moment `DATABASE_URL` points
  at shared Postgres, matching Phase 6's distributed-rate-limiting principle. Never add a
  separate in-memory/per-process counter for this.
- `app.applications.doctor` (the "application doctor") must never be extended to auto-repair —
  read-only reporting only, same as `app.registry.doctor`/`app.sponsorship.doctor`.
- Synthetic/fixture data for this layer (the mock ATS, `mock_scenario` provider_metadata keys)
  must only ever be exercised via `provider == "mock_ats"` jobs, which can never collide with a
  real provider name — never write a real job's provider/external_job_id through the mock path.

## Application Worker Fleet Rules (recorded after Phase 9, apply to all future phases)

- `app.applications.executor.process_execution()` must never call `provider.submit()` for an
  execution whose stored status is already `SUBMITTING` or `SUBMITTED` — that means a prior
  invocation (this process or another) reached the submit step but never recorded a final
  outcome (a crash), and the request may or may not have reached the provider. Resuming such a
  row must always convert straight to `SUBMISSION_STATUS_UNKNOWN` instead. This guard was
  missing before Phase 9's own crash-recovery testing caught it live; never remove or weaken it,
  and never add a second code path that calls `submit()` without first checking it.
- `app.applications.queue.claim_execution_batch`'s claimable-status set must include every
  status an execution can be left in mid-pipeline (not just `QUEUED`) so that lease expiry
  alone — never a second crash-detection mechanism — is sufficient to recover a crashed
  worker's abandoned execution, matching the discovery fleet's existing guarantee. Any new
  `ExecutionStatus` value that represents "still actively being worked by a worker, not yet
  paused for a human" must be added to this set.
- `app.applications.circuit` (submission circuit breaker, table
  `application_provider_circuit_state`) and `app.workers.circuit` (discovery circuit breaker,
  table `provider_circuit_state`) are permanently separate mechanisms — a provider's discovery
  circuit state must never gate application submission, and vice versa. Both must remain
  self-healing (never permanently disable a provider).
- `app.applications.worker.ApplicationWorker` and `app.workers.runner.Worker` remain logically
  separate worker fleets: an `ApplicationWorker` declares only `APPLICATION_PREPARE`/
  `APPLICATION_SUBMIT` capabilities and must never claim from `app.workers.queue`/
  `app.workers.leasing` (the discovery poll/verification queues), and a discovery worker must
  never claim from `app.applications.queue`.
- A claimed application execution that is skipped without ever being attempted (submission
  circuit open, provider at its submission concurrency limit) gets a short lease-cooldown
  extension, never a bare release — same busy-spin-avoidance rule Phase 5 established for
  discovery, extended here.
- `app.applications.scheduler.run_cycle()` (`APPLICATION_AUTO_PREPARE_ENABLED`) and
  `AUTO_SUBMIT_ENABLED` remain independent switches (CLAUDE.md Phase 9 section 37) — the
  scheduler queuing a job in `ASSIST` mode must never depend on `AUTO_SUBMIT_ENABLED`, and a job
  is only ever queued in `AUTO_PERMITTED` mode when both that flag AND the specific job's own
  `auto_submit_eligible` are true.
- `app.applications.reconcile_worker.run_pass()` must never itself decide a
  `SUBMISSION_STATUS_UNKNOWN` execution's fate — it may only call a provider's optional
  `check_submission_status()` hook (default unsupported/`None` for every real ATS adapter) and,
  when that returns genuine evidence, funnel the result through the existing
  `app.applications.reconcile.reconcile_execution()` — the same function a human operator uses.
  Never add a code path that marks an execution `APPLIED`/`WITHDRAWN` by any other route.
- `ApplicationProvider.check_submission_status()` and `.check_job_still_active()` must only ever
  return genuine evidence obtained from the provider itself (or `None`/`True` meaning "not
  checkable") — never a guess, and `confirmation_recheck_supported` must be set `True` only on a
  provider that genuinely implements the hook with real evidence, matching
  `submission_supported`'s existing "only if genuinely tested" bar.
- `app.applications.executor.process_execution()`'s pre-submission revalidation (fresh
  `get_job()` + fresh `evaluate_executor_eligibility()` immediately before the `SUBMITTING`
  transition) must never be removed or bypassed — a hard-skip result at this point always
  becomes `ExecutionStatus.JOB_NO_LONGER_ACTIVE`, never a submission.
- `app.applications.browser_assist` (optional, `BROWSER_ASSIST_ENABLED`, requires Playwright
  installed separately) must never click a final submit/apply action, must never fill a
  `DEMOGRAPHICS`/`VOLUNTARY_DISCLOSURE`/`LEGAL_ATTESTATION`/`SIGNATURE`-category field, and must
  never use `launch_persistent_context()` or save `storage_state` to disk — every browser
  context is fresh and ephemeral, closed at the end of every call. No stealth/fingerprint-
  spoofing/CAPTCHA-solving/proxy-rotation/anti-bot-bypass/hidden-login/MFA-interception may ever
  be added to it.
- A worker's `DRAINING` status (`app.workers.models.WorkerStatus.DRAINING`) is only ever set by
  an explicit operator action (`app.applications.worker_admin.request_drain`), never
  automatically. A draining `ApplicationWorker` must keep heartbeating, must stop claiming new
  executions, and must call `process_execution(..., allow_submission=False)` for any execution
  it finishes processing — never start a new submission while draining.
- Boolean fields written to any table shared with the Postgres backend must be coerced to `int`
  (`0`/`1`) before being passed to `conn.execute()`, matching the existing "boolean flags stay
  INTEGER in both backends" schema rule — SQLite silently accepts a raw Python `bool`, but
  psycopg maps it to Postgres's native `boolean` type, which conflicts with an `INTEGER` column
  (a real bug Phase 9's own Postgres testing caught in `app.jobs_repo`). Any new table/column
  added in a future phase that stores a Python `bool` must go through the same coercion pattern
  (see `app.jobs_repo._coerce_sql_value`) rather than relying on SQLite's permissiveness.
- Any new table whose primary key is not literally a column named `id` must be added to
  `app.db_postgres._TABLES_WITHOUT_ID_PK`, or its INSERT statements will break under the
  Postgres backend's automatic `RETURNING id` / `.lastrowid` emulation.
- Synthetic benchmark/fixture data for this layer (`mock_ats_server_records`, any future
  application-worker load-test script) follows the same isolated-temp-DB-only,
  never-collide-with-a-real-name convention as every prior phase's benchmarks — never write
  synthetic rows into a real registry or a developer's real `data/app.db`.