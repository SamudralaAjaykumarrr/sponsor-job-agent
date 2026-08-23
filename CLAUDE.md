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

## Real ATS Browser Assist Rules (recorded after Phase 10, apply to all future phases)

- `app.applications.browser_runtime` is the ONLY module that ever imports `playwright` —
  `app.applications.browser_assist` (session orchestration) never does. This module must never
  grow a function that clicks a final submit/apply action for ANY real provider, under any
  condition — enforced not just by review but by a static doctor check
  (`app.applications.doctor._check_no_browser_auto_submit_capability`) that scans the module's
  own public API for a forbidden name pattern on every doctor run. Every browser context is a
  fresh, ephemeral `browser.new_context()` — never `launch_persistent_context()`, never a saved
  `storage_state` — and no password/MFA-code/cookie/token is ever a column in
  `browser_assist_sessions` or written to disk.
- `app.applications.browser_assist.start_session()` re-derives
  `app.applications.eligibility.evaluate_executor_eligibility()` independently every time, never
  trusting a stale `application_state` or a previously-passing check — a hard-skip employment
  type or non-eligible sponsorship status must NEVER get a browser session, full stop, matching
  the same defense-in-depth principle Phase 8's executor already established.
- Every browser navigation is checked with `app.applications.domain_allowlist
  .is_allowed_host_for_session()` immediately after each page load; an unexpected host pauses the
  session (`PAUSED_PLATFORM_RESTRICTED`) rather than continuing to interact with an unverified
  page. An EMPTY current-URL string is the correct rejection condition — an empty HOSTNAME is
  not (a `file://` URL, and any two same-host pages with no netloc, legitimately have one); a
  real live-Chromium test caught this distinction being conflated.
- `browser_assist_sessions` follows the exact same "one active thing per job" pattern
  `application_executions` already uses: `active=1` while non-terminal, a partial unique index
  on `(job_id) WHERE active=1` is the actual atomic distributed-ownership guard (verified live
  under real PostgreSQL with 8 concurrent claimers — exactly one ever wins), and
  `SUBMISSION_STATUS_UNKNOWN` deliberately stays `active=1` so a second concurrent attempt is
  blocked until a human reconciles it, exactly like Phase 8's execution model.
- A session's form-fingerprint drift check (`PAUSED_FORM_CHANGED`) applies on
  `resume_session()`/`mark_user_action_complete()` — an unexpected change while paused must
  never be silently remapped — but NEVER on `advance_step()`, where a completely different
  field set on the next page of a genuinely multi-step form is expected, not drift. Do not merge
  these two checks back into one; a real E2E test caught exactly this conflation making every
  multi-step form unusable.
- `resume_session()`'s crash-recovery split is deliberate and must not be collapsed: if the
  browser/process is gone and the session's last known status was pre-submission (any
  `PAUSED_*`, `ACTIVE`, `READY_FOR_FINAL_SUBMIT`, `STARTING`), a fresh browser safely reopens at
  the same `application_url` and rediscovers from scratch; if the last known status was
  `AWAITING_USER_SUBMIT`, the outcome is NEVER guessed — the session becomes
  `SUBMISSION_STATUS_UNKNOWN` for explicit human reconciliation. This module makes no claim of,
  and must not attempt, cross-process browser reattachment as a tested guarantee.
- `app.applications.browser_assist.attempt_user_submit_reconciliation()` is the ONLY code path
  that may mark a browser-assist-linked execution `APPLIED` from browser-observed evidence, and
  only when the browser is genuinely still live AND the current page's text contains a real
  success-phrase match — never merely because the user claims they submitted. It funnels
  through the same `app.applications.repo.update_execution()` mirror path every other APPLIED
  transition uses; it must never write `jobs.application_state` directly.
- `app.applications.browser_capability_matrix` is deliberately DATA, not a `RealATSAssistProvider`
  class hierarchy — `browser_runtime`'s DOM engine is genuinely provider-agnostic by
  construction (live-proven this phase against real, unrelated Greenhouse/Lever/Ashby forms with
  zero per-provider code), so per-provider variation is only ever "has this been genuinely opened
  and inspected live" (`LIVE_FORM_VERIFIED`/`FIXTURE_ONLY`/`NOT_TESTED`), tracked as a dated,
  update-from-a-genuine-observation-only table — never inflate a row without a fresh check, and
  never build a parallel class hierarchy that would reintroduce the per-provider branching the
  generic engine exists to avoid.
- `app.applications.background_scheduler` is the only code path that runs
  `app.applications.reconcile_worker.run_pass()` or
  `app.applications.browser_assist.expire_stale_sessions()` on a schedule; both remain
  independently gated by their own existing flags (`RECONCILE_WORKER_ENABLED`,
  `BROWSER_ASSIST_ENABLED`) and a failure in one must never stop the other or the loop itself —
  matching every other background loop's "one thing failing never takes down the rest"
  principle in this project.
- A radio/checkbox GROUP's detected label must come from its fieldset legend (the actual
  question) in preference to any single option's own per-choice label text — the opposite
  priority from a normal single-value field, where the field's own label IS the question. A real
  E2E test caught the old uniform-priority code silently mislabeling every sponsorship-style
  radio question as its first choice's text ("Yes"), which meant the field was never recognized
  or filled at all.
- `app.applications.schema.DECLINE_TO_SELF_IDENTIFY_PHRASES` must keep BOTH the
  apostrophe-deleted and apostrophe-replaced-with-space forms of every contraction (`"i dont"`
  and `"i don t"`) — every caller normalizes via `.replace("'", "")`, which deletes the
  apostrophe rather than inserting a space, so only the deleted form actually matches; a real
  bug (present since Phase 8, only surfaced by Phase 10's live/E2E testing) had this list only
  containing the never-actually-produced space form.

## Real ATS Flow Hardening Rules (recorded after Phase 11, apply to all future phases)

- `app.applications.apply_entry` is the ONLY source of apply-entry/final-submit/step-progress
  classification logic — `app.applications.browser_runtime` calls into it rather than
  maintaining a second, parallel phrase table. `NAVIGATION_SAFE_PHRASES`, `FINAL_SUBMIT_PHRASES`,
  and `LOGIN_TRIGGER_PHRASES` must remain mutually disjoint (no phrase in two tables), so
  classification is always a single unambiguous lookup, never a priority tie-break. A real Phase
  10 bug (`"apply now"` was listed as a FINAL-submit phrase) is exactly the failure mode this
  separation prevents; never reintroduce it.
- Only a control FRESHLY classified `NAVIGATION_SAFE` in the SAME call may ever be clicked by
  `browser_runtime.advance_apply_entry()` (or any future apply-entry-clicking function) — never a
  cached/stale classification from an earlier discovery pass. This function, and any future one
  like it, must never be named with a `click_apply`/`auto_submit`/`click_submit`/
  `submit_application` fragment, since `app.applications.doctor.
  _check_no_browser_auto_submit_capability` statically scans `browser_runtime`'s public API for
  exactly those patterns — a new apply-entry-clicking capability must stay visible to that check
  by naming convention, not evade it.
- Apply-entry click-through is always bounded (`_MAX_APPLY_ENTRY_HOPS` in
  `app.applications.browser_assist`) and re-validates the domain allowlist and CAPTCHA/login
  state after EVERY hop via the normal `rediscover()` path — an apply-entry click is never exempt
  from any existing safety check, and a misbehaving page can never trap this in an unbounded
  click loop.
- `apply_entry.parse_step_progress()`'s slash-ratio pattern (`N / M`) must always require a
  `step`/`progress` keyword within a short window before the numbers — a real live run against a
  genuine Greenhouse posting caught an ungated version of this regex misreading an unrelated
  on-page date ("7/31") as "step 7 of 31". `total_steps_if_known` must never be persisted without
  `step_confidence == EXACT` alongside it (enforced by
  `app.applications.doctor._check_invalid_step_progress`'s `invented_total_steps` check) — a bare
  "Step N" with no genuinely parsed total stays `INFERRED` with `total_steps=None`, never guessed.
- Every browser-touching function in `app.applications.browser_assist` (any present or future
  one that calls into `app.applications.browser_runtime`) must claim the session's lease via
  `browser_session.claim_session()` at entry and release it via `release_session_lease()` in a
  `finally` at exit, REGARDLESS of the resulting status — a `PAUSED_*`/terminal outcome must
  never leave the lease held, or no other worker could ever resume that session
  (`app.applications.doctor._check_paused_session_holding_lease` catches a regression here).
  `claim_session()`'s re-entrant-for-the-same-`worker_id` clause (`OR lease_owner = ?`) must
  never be removed — an orchestration function that internally delegates to another one (e.g. a
  future refactor mirroring `mark_user_action_complete` -> `resume_session`) depends on it.
- "You already applied"/duplicate-application evidence (`ConfirmationOutcome.already_applied`,
  `BrowserSessionStatus.DUPLICATE_APPLICATION_DETECTED`) must never be folded into a fresh
  `CONFIRMED`/`APPLIED` transition — checked BEFORE the ordinary success-phrase match in
  `browser_runtime._do_capture_confirmation()`, and always routed through a status distinct from
  `CONFIRMED` so a human reconciles which is true. Never add a code path that marks an execution
  `APPLIED` from duplicate-application evidence alone.
- `app.applications.workday_tenant` (or any future per-provider-variant tracker) records
  observations keyed by `(tenant, site)`, never as a single collapsed capability claim for the
  whole provider — a NULL capability column means "not observed", distinct from `0` ("observed
  absent"), and must never be conflated. Never generalize one tenant's observed behavior (or one
  run's result, if results are inconsistent across runs of the same URL — report the
  inconsistency honestly rather than keeping only the more favorable run) to "provider X is
  supported."
- `app.applications.capability_evidence` records `LIVE_PUBLIC` for a capability like
  `apply_first_click` ONLY when the capability was genuinely proven working end-to-end (a control
  was both classified `NAVIGATION_SAFE` AND successfully clicked/navigated) — a control that was
  found but correctly left unclicked (any other classification, the safety mechanism working as
  intended) is `NOT_TESTED` with a note, never inflated to look like a proven capability.
  Staleness (`is_stale()`, `CAPABILITY_EVIDENCE_MAX_AGE_DAYS`) only ever prompts revalidation
  (doctor warning, dashboard badge) — it must never automatically disable a capability.
- Real, live tenant/posting URLs used by any validation script (`scripts/
  phase11_live_validation.py` and any successor) must always be genuinely discovered (a public
  API response, or a plain web search for publicly documented career-board URLs) — never guessed
  or fabricated. A tenant that turns out to be offline/unreachable is reported `NOT RUN` with the
  real reason; no substitute is silently invented in its place.

## SPA/Dynamic ATS Flow Hardening Rules (recorded after Phase 12, apply to all future phases)

- `app.applications.trusted_redirects.classify_redirect_trust()` may only ever trust a
  cross-host destination whose hostname matches one of `app.applications.domain_allowlist.
  PROVIDER_DOMAINS`'s existing per-provider suffixes (excluding `mock_ats`'s local/test hosts,
  which must never be a real trust signal) — this is the SAME evidence table already used for
  post-navigation host checks, never a second, broader, or provider-unaware allowlist. A `file://`
  URL is always `SAME_HOST`-trusted (this project's entire local test-fixture mechanism, mirroring
  `domain_allowlist.is_allowed_domain`'s own carve-out — a real live-Chromium run caught an
  earlier version treating it as unsafe, breaking every apply-entry fixture). `javascript:`/
  `data:`/`vbscript:` schemes are always `UNSAFE_SCHEME`, regardless of visible text.
  `app.applications.doctor._check_unsafe_redirect_allowlist` statically enforces that every
  trusted suffix is a real, specific domain, never a bare/near-empty or generic-TLD entry.
- Trust from `classify_redirect_trust` only ever unlocks the ordinary TEXT-classification path in
  `apply_entry.classify_apply_control_detailed()` — a `TRUSTED_ATS_REDIRECT` destination whose
  text reads as a final submission (`FINAL_SUBMIT_PHRASES`) still classifies `FINAL_SUBMIT`, never
  `NAVIGATION_SAFE`. Trust must never, by itself, mark anything safe to click.
- `apply_entry.select_apply_control()` is the only sanctioned way to resolve multiple apply-entry
  candidates on one page. Multiple `NAVIGATION_SAFE` candidates sharing the identical destination
  (the ordinary top/bottom/sticky-Apply-button pattern) are not ambiguous; multiple
  `NAVIGATION_SAFE` candidates with genuinely DIFFERENT destinations must never be resolved by
  picking one — always `(None, reason)`, routed to `PAUSED_AMBIGUOUS_APPLY_CONTROL`.
- `app.applications.job_identity.verify_job_identity()` must only ever report `MISMATCH` when a
  confidently-shaped requisition/posting-id token was extracted from BOTH the session's own
  recorded `application_url` and the current page URL and they genuinely differ. When no such
  token exists on one or both sides, the result is `UNVERIFIABLE` — never treated as a match or a
  mismatch, and `browser_runtime._do_discover()` only ever pauses `JOB_IDENTITY_MISMATCH` on a
  confirmed `MISMATCH`, never on `UNVERIFIABLE`.
- `browser_runtime._wait_for_stable_state()` is the only sanctioned DOM-readiness wait for any
  future navigation/click-driven code path in this module — never reintroduce a bare
  `wait_for_load_state("networkidle")` as the sole readiness signal (a genuinely SPA-rendered page
  may never reach it) and never an unbounded/arbitrary-length sleep. Every poll interval and the
  overall timeout must remain configured (`BROWSER_DOM_STABILIZATION_*`), never hardcoded inline.
- `browser_runtime._scan_iframes()` only ever reads frames Playwright can normally read (the same
  access a browser's own devtools has) — never a cross-origin sandbox bypass of any kind. An
  allowed-host frame's fields must be filled by targeting that frame's own `Frame` object (tagged
  via `rf["_frame"]`), never `self.page` — a real live test caught fields being discovered but
  silently never fillable before this tagging existed; any future field-scanning addition that
  can originate from a non-main-frame source must propagate the same tagging. An unexpected-host
  frame only pauses the session (`PAUSED_IFRAME_UNEXPECTED_HOST`) when it actually contains
  form-shaped content — an ad/analytics/tracking iframe must never by itself trigger a pause.
- Every DOM-scanning `page.evaluate()` call in `browser_runtime` must use the shared
  `_DEEP_QUERY_JS` shadow-piercing helper (never a plain `document.querySelectorAll` for field/
  button/apply-control discovery) so open-shadow-root content is found uniformly. This must never
  be extended to attempt reading a CLOSED shadow root (`el.shadowRoot` is null/undefined for one)
  — that is the correct, honest `UNSUPPORTED` outcome, never a bypass target.
- `app.applications.workday_tenant`'s `workday_tenant_attempts` table is APPEND-ONLY — a new
  observation is always a new row via `record_attempt()`, never an update to a prior attempt (the
  aggregate `workday_tenant_observations` row, maintained separately by `record_observation()`,
  remains the current-capability view). `classify_stability()` must never report `STABLE` from
  fewer than 2 attempts, and any 2+ attempts with disagreeing `result` values are `VARIABLE` —
  reported honestly, never cherry-picked to the more favorable run. `STALE` (most recent attempt
  older than `CAPABILITY_EVIDENCE_MAX_AGE_DAYS`) always overrides whatever the attempts once
  showed. `app.applications.doctor._check_workday_universal_claim_from_one_tenant` statically
  prevents `browser_capability_matrix`'s workday row from claiming `LIVE_FORM_VERIFIED` without at
  least one genuinely `STABLE` tenant/site behind it.
- `capability_evidence.EvidenceVerificationType`'s `STATIC_HTML`/`REAL_BROWSER`/
  `REAL_BROWSER_REPEATED` values (added alongside the unchanged `LIVE_PUBLIC`/`FIXTURE`/
  `NOT_TESTED`) and the `repeat_count` column exist ONLY to record genuine re-observation —
  `record_evidence()`'s repeat-streak logic increments `repeat_count` and promotes to
  `REAL_BROWSER_REPEATED` only when BOTH the prior and the new recording are themselves
  time-sensitive real-observation types; a `FIXTURE`/`NOT_TESTED`/`STATIC_HTML` re-check always
  resets the streak to 1. Never manually set `repeat_count` or the verification type to simulate
  a repeat that didn't genuinely happen.
- `app.applications.spa_events` is an append-only, best-effort (never-raising) structured event
  log — the only source `app.applications.metrics.collect_phase12()` and the
  `stage_transition_invalid`/related doctor checks may query. A write failure here must never
  propagate into or interrupt a real browser discovery/fill pass.
- `apply_entry.is_valid_stage_transition()` is advisory/logged-only (via `spa_events`), never
  blocking — it must never be wired to reject or roll back a real session's stage update. It must
  always return `True` when `after_reconstruction=True` is passed (the sanctioned Phase 11
  reconstruct-and-resume path can legitimately re-land on an earlier stage after a fresh browser
  reopens and rediscovers from scratch — this is expected, not anomalous) and must always treat
  `CONFIRMATION` as terminal (any different stage observed afterward is always flagged).
- Real, live tenant/posting URLs used by `scripts/phase12_live_validation.py` and any successor
  follow the same discovery rule established in Phase 11 (public API response or plain web
  search, never guessed) — extended here to also cover a genuinely NEW posting/URL SHAPE for an
  already-covered provider (e.g. this phase's SmartRecruiters `oneclick-ui` shape): finding one
  requires the same real discovery, never fabrication, and an encountered CAPTCHA/anti-bot
  challenge is always reported as the honest result (a conclusive characterization, CLAUDE.md
  Phase 12 section 76 criterion B), never worked around to force a fake "form reached" result.

## Provider Resilience Rules (recorded after Phase 13, apply to all future phases)

- `app.applications.job_identity.verify_job_identity()` (the Phase 12 single-signal URL-
  requisition-token comparison) stays wired unchanged into `browser_runtime._do_discover()`'s
  per-navigation `MISMATCH` gate. `verify_job_identity_full()` (the Phase 13 multi-signal
  `JobIdentityVerification`: company/title/requisition-id/tenant-site/location) is a SEPARATE,
  additional check run only at the two highest-stakes moments — immediately before a resume-upload
  field is filled, and immediately before `READY_FOR_FINAL_SUBMIT`. Provider name is never a
  compared signal in `verify_job_identity_full()` — both "stored" and "observed" would trivially
  be the same in-process value, never independent evidence from the page. `location` is a WEAK,
  corroborating-only signal (two different requisitions commonly share a location string) — it
  may only ever produce AMBIGUOUS on its own, never PROBABLE/VERIFIED, and a location mismatch is
  never counted toward MISMATCH. **Only a `VERIFIED` verdict may continue unattended past this
  gate, by default** (`app.applications.job_identity.meets_min_confidence`,
  `config.APPLICATION_IDENTITY_MIN_CONFIDENCE` default `"VERIFIED"`) — `PROBABLE`/`AMBIGUOUS`/
  `INSUFFICIENT` all pause `PAUSED_JOB_IDENTITY_UNVERIFIED`, distinct from a confirmed `MISMATCH`'s
  `PAUSED_JOB_IDENTITY_MISMATCH`. `MISMATCH` is NEVER affected by
  `APPLICATION_IDENTITY_MIN_CONFIDENCE` — a confirmed contradiction always pauses unconditionally,
  regardless of configuration. `browser_assist.start_session()`/`resume_session()` are the ONLY
  two call sites of `browser_runtime.open_session()` in the codebase, so this gate is centralized;
  no worker/scheduler/dashboard path may bypass it by calling `browser_runtime` directly.
- `app.applications.title_normalization.titles_equivalent()` is order/punctuation/seniority-
  marker-set equivalence ONLY — it must never be loosened into fuzzy/similarity-based matching,
  and title equivalence alone must never be treated as identity proof.
- `app.applications.provider_health` (real-browser ASSIST flow health) is permanently separate
  from `app.workers.circuit` (discovery poll) and `app.applications.circuit` (submission) — none
  of the three may gate the others. Recording evidence here must never auto-disable a provider;
  a `DEGRADED`/`STALE`/`SCHEMA_DRIFT`/`CAPTCHA_BLOCKED`/`AUTH_GATED` health only ever surfaces for
  review. `compute_health()` must remain a pure, live-recomputed function over the stored row —
  never cached — so a "healthy" label can never silently outlive the evidence behind it.
- `app.applications.confirmation_evidence.ConfirmationGrade.confirms()` (STRONG/MODERATE only)
  is the only gate that may set a browser-assist session's execution `APPLIED` from captured
  confirmation text. WEAK/NONE must never confirm, even if a future provider-specific pattern
  supplies a confirmation id or confirmation-shaped URL without a trusted phrase match.
- `app.applications.checkpoints` is an append-only, best-effort (never-raising) OBSERVABILITY
  log layered on top of the existing reconstruct-and-resume mechanism — it must never itself
  perform recovery, and `find_ordering_anomalies()` stays advisory/logged-only, never blocking,
  mirroring `apply_entry.is_valid_stage_transition`'s own design.
- `app.applications.canary` must never import `app.applications.mapping` or receive an
  `ApplicationField` list, must never call an upload function, and must never click a control
  classified anything other than `NAVIGATION_SAFE` for its single bounded apply-entry hop —
  these are structural invariants (no upload/submit code path exists in the module at all), not
  merely runtime checks. `REAL_ATS_CANARY_ENABLED` stays `false` by default and
  `run_scheduled_canaries()` is the only function that may run canaries on a schedule.
- `app.applications.resume_integrity.verify_resume_freshness()` only ever reports `fresh=False`
  on a CONFIRMED divergence: both `resume_jd_fingerprint` and the job's current
  `jd_sponsorship_fingerprint` non-empty AND different. Missing/unset data on either side is
  always reported fresh — never a guessed staleness from absent evidence.
- CAPTCHA detection (`browser_runtime._do_discover`, `canary._observe`) is DOM-element-based only
  (`iframe[src*='captcha']`, `[class*='captcha']`, `[id*='captcha']`) — never a raw whole-page-
  text substring scan, which a real Phase 13 live-validation run proved matches a merely-
  referenced (never rendered) reCAPTCHA script tag on multiple real providers' current pages. Any
  future refinement of this heuristic must be verified against both the real end-to-end fixture
  (`tests/browser_fixtures.py`'s `captcha_page`) and, where possible, a real live provider page
  before landing — never loosened in a way that could miss a genuinely rendered challenge.

## Resume Optimizer / Dashboard Architecture Rules (recorded after Phase 14, apply to all future phases)

- `app.resume.claim_checker.check_resume_claims()` remains the single, unmodified truthfulness
  firewall for every generated resume, including every optimizer-produced variant. No new code
  path may bypass it or check a looser/parallel notion of "supported claim" — `app.resume_
  optimizer.optimizer.optimize_resume()` calls it exactly once, unconditionally, on every
  generated `ResumeContent`, and a violation always yields `CLAIM_CHECK_FAILED`, never a partial
  or best-effort `READY`.
- No universal ATS-match score is ever computed or displayed. `app.resume_optimizer.quality
  .QualityReport.internal_alignment_score` is the only composite number produced anywhere in
  this layer, and every surface that renders it (dashboard, job-detail page, JSON API) must pair
  it with an explicit "not an ATS score / not an interview-hire probability" label — never a
  bare percentage.
- `app.resume_optimizer.models.TRANSFERABLE_ELIGIBLE_CATEGORIES` deliberately excludes
  `LANGUAGE`, `ARCHITECTURE`, and `SECURITY` — a missing skill in one of those categories is
  always `MISSING`, never `TRANSFERABLE`, regardless of what else the candidate has verified in
  the same category. Widening this set requires the same bar as any other truthfulness change:
  the analogy must be honestly defensible on a resume, not merely thematically related.
- `resume_variants` is the only table a resume artifact's "current" status may live on. Its two
  unique indexes — `(job_id, jd_fingerprint, profile_version, optimizer_version)` for
  idempotency and a partial `(job_id) WHERE current = 1` for the single-current-variant guarantee
  — are the actual concurrency mechanism (`app.resume_optimizer.repo.claim_variant()`'s atomic
  INSERT, never a read-then-write check). Any future write path against `jd_analyses`,
  `resume_variants`, or a table keyed off either must follow the same catch-the-unique-violation-
  and-refetch pattern `save_jd_analysis()` uses — a real UniqueViolation race under 8 concurrent
  Postgres callers was caught live by this phase's own concurrency test before this pattern was
  applied consistently.
- A JD or candidate-profile change never silently leaves a stale resume looking current.
  `app.pipeline.reanalyze_job()` calls `app.resume_optimizer.repo.mark_stale()` on any title/
  description change; a profile-content hash change (`fingerprint.compute_profile_version()`)
  changes the identity itself, so the next `optimize_resume()` call naturally creates a new
  variant rather than reusing a stale one. Neither path may be removed or made silent.
- `app.pipeline_dashboard.is_actionable()` hides a job from the default dashboard view only on a
  POSITIVE `CONTRACT`/`C2C`/`PART_TIME`/`INTERNSHIP`/`TEMPORARY`/`SEASONAL`/`FREELANCE`
  classification (`app.matching.employment_type.classify_employment_type()`) — `UNKNOWN` always
  stays visible by default, matching this project's existing "UNKNOWN is not itself a hard-skip"
  pattern for sponsorship. Never require a POSITIVE `FULL_TIME` classification to show a job —
  an earlier version of this exact check did that and silently hid every job with no explicit
  employment-type signal (the common case) from the unified dashboard; a doctor-style regression
  test now guards this.
- `app.resume_optimizer.scheduler` is a lightweight single-process asyncio background loop
  (mirroring `app.applications.background_scheduler`'s structure), NOT a leased distributed
  worker capability — this was a deliberate Phase 14 scope decision given the workload's low
  volume and `optimize_resume()`'s own database-level idempotency/concurrency guarantees. A
  future `RESUME_OPTIMIZATION` `WorkerCapability` remains a reasonable addition if true
  multi-machine parallelism for this specific workload is ever needed, but must not be added
  merely to mirror the discovery/application worker fleets' shape.
- `RESUME_OPTIMIZATION_ENABLED` (background scheduling) and the dashboard's manual Generate/
  Regenerate action are independent — the manual action and the CLI must never be gated by this
  flag, matching `APPLICATION_AUTO_PREPARE_ENABLED`'s existing "never gate manual work" contract.
- `app.resume_optimizer` must never import from `app.applications.browser_*`, `app.applications
  .circuit`, or `app.workers.circuit`, and none of those modules may import from
  `app.resume_optimizer` — resume optimization, browser-assist execution, and the discovery/
  submission circuit breakers remain three independent concerns, matching this project's
  existing separation-of-concerns rules for adjacent subsystems.
- Synthetic benchmark data (`scripts/resume_optimizer_benchmark.py`) follows the same isolated-
  temp-DB-only, never-collide-with-a-real-name convention as every prior phase's benchmark
  (`benchmark-fixture` provider name) — never write synthetic rows into a real registry or a
  developer's real `data/app.db`, and never claim the benchmark predicts interview/hiring
  outcomes.

## One-Click Autonomous Agent Rules (recorded after the one-click-autonomous-agent build,
apply to all future phases)

These are durable rules the orchestration layer (`app/agent/orchestrator.py`,
`app/agent/run_state.py`) and the one-page resume contract (`app/resume_optimizer/one_page.py`)
must keep obeying as this project evolves further:

- `AgentOrchestrator` coordinates existing stages by calling their unmodified public entry points
  (`app.agent.cycle.run_discovery_cycle`, `app.resume_optimizer.optimizer.optimize_resume`,
  `app.applications.scheduler.run_cycle`, `app.applications.worker.ApplicationWorker.run`) — it
  must never reimplement, fork, or partially duplicate any stage's own logic. A new pipeline stage
  added in the future is wired into the orchestrator by calling its own entry point the same way,
  never by copying its internals into `app/agent/orchestrator.py`.
- The orchestrator raises `config.APPLICATION_EXECUTOR_ENABLED`/`APPLICATION_AUTO_PREPARE_ENABLED`
  for the duration it is `RUNNING` and always restores the operator's actual pre-start value on
  stop (`_apply_config_overrides`/`_restore_config_overrides`) — it must never leave either flag
  permanently altered, and it must never touch `AUTO_SUBMIT_ENABLED` in a normal (non-test-mode)
  run. `AUTO_SUBMIT_ENABLED` may only ever be temporarily raised for TEST MODE, and only alongside
  the deterministic `mock_ats` fixture — never for a real provider, never in a normal run, no
  matter how the orchestrator's scope grows.
- `agent_run_state` (single-row, durable desired/actual state) and `agent_cycle_log` (append-only
  per-cycle counters) remain the only source of truth for agent status and the `agent_*`/
  `one_page_resume_*` metrics — never an in-process-only counter that a restart would silently
  reset, matching this project's existing "never an in-process counter, always a live query over
  persisted state" metrics convention.
- Restart recovery (`app/main.py`'s `lifespan` re-starting the orchestrator when
  `desired_state == RUNNING`) must remain safe with zero new duplicate-prevention logic of its
  own — it works only because every stage the orchestrator drives already has its own idempotent/
  leased claim mechanism (partial unique indexes, lease-expiry-only recovery). A future stage added
  to the orchestrator must have the same property before restart recovery can safely include it.
- `app.resume_optimizer.one_page.enforce_one_page()` is the only place PDF page count is measured
  and compression is applied — it must never rewrite a verified bullet/skill's text character-by-
  character (that would produce a string absent from `verified_bullets`, which
  `app.resume.claim_checker.check_resume_claims()` — unmodified, per the existing Phase 14 rule
  above — would correctly reject). Every compression step must stay removal-only (whole
  bullets/skills/projects) or apply to genuinely free, non-claim-checked text (the summary) or pure
  rendering (font/spacing via `compression_level`). `ONE_PAGE_MIN_FONT_SIZE` is never bypassed
  regardless of how many compression steps have been applied, and a resume that cannot safely reach
  one page becomes `ResumeVariantStatus.REVIEW_REQUIRED` — never a fabricated tiny/unreadable
  render, and never silently left multi-page while still marked `READY`.
- `app.agent.orchestrator._run_resume_stage()` promotes a resume_optimizer variant onto
  `jobs.resume_docx_path`/`resume_pdf_path`/`resume_txt_path`/`resume_jd_fingerprint`/
  `promoted_resume_variant_id` only when that variant is `READY` with `page_count == 1` — a
  `REVIEW_REQUIRED` overflow result must never be promoted, so the application executor can never
  pick up a multi-page resume through the automatic pipeline.
- `app.applications.executor._verify_resume_artifact()`'s path-ownership check recognizes any
  path containing the `/<job_id>/` segment (matching `app.applications.doctor.
  _check_wrong_resume_job_mapping` and `app.resume_optimizer.doctor.
  _check_resume_linked_to_wrong_job`'s existing convention) — never re-narrow this back to an
  exact-immediate-parent-directory-name match, which breaks the resume_optimizer's nested
  `output/<job_id>/optimized/<variant_id>/` layout (a real integration bug this feature's own live
  testing caught).
- Any new mutating FastAPI route that itself calls `asyncio.create_task()` (or otherwise needs to
  run on the actual event loop thread) must be declared `async def` — a plain `def` route handler
  runs in FastAPI's worker threadpool, where there is no running event loop
  (`asyncio.get_running_loop()` raises), a real bug this feature's own route-level testing caught
  on `/agent/start`.
- Synthetic/fixture data for TEST MODE (`agent-test-mode-fixture-1`, provider `mock_ats`) follows
  the same never-collide-with-a-real-identifier convention as every other benchmark/fixture in this
  project — idempotent re-seeding (matched by its fixed `external_job_id`), never written to
  anything but the `mock_ats` provider path, and never mistaken for a real job.
