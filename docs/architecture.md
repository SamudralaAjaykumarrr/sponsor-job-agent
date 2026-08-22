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

## Phase 4 — company/career-portal registry (acquisition, verification, lifecycle, scale)

See `docs/phase4-company-registry.md`, `docs/registry-import.md`, `docs/registry-verification.md`,
`docs/registry-operations.md`, `docs/registry-scaling.md` for full detail. Summary of what was
added on top of Phase 3, without rebuilding it (all 205 Phase 3 tests still pass unmodified):

- New, additive tables (`app/db.py`): `registry_companies`, `registry_portals`,
  `registry_provenance`, `registry_portal_health_events`, `registry_migrations`,
  `registry_import_batches`. The Phase 3 `company_registry` operational polling table is
  **unchanged** — it remains the only thing `app/agent/cycle.py` reads from.
- `app/registry/models.py` gained `Company`, `CareerPortal`, `RegistryProvenance` and the
  `PortalStatus`/`DiscoveryStatus`/`VerificationResult`/`IdentityStatus` enums, alongside the
  untouched `CompanyRegistryEntry`.
- `app/registry/normalize.py`, `url_canon.py` — deterministic company-name/domain normalization
  and career-portal URL canonicalization (tenant-path-preserving, unlike the job-posting-URL
  canonicalizer in `app/discovery/dedup.py`).
- `app/registry/store.py` — bounded/keyset-paginated CRUD for the new tables; every list query
  takes a `limit` and never does `SELECT *` over the whole table.
- `app/registry/importers.py` + `app/registry/cli.py` — `RegistrySource` interface, CSV/JSON/JSONL
  bulk import, idempotent upsert engine, `python -m app.registry.cli {import,validate,stats,
  export,doctor,verify}`.
- `app/registry/probe.py` — raw, bounded, per-provider structural probes (reuses each connector's
  own URL templates) that **raise** on failure, unlike `JobProvider.fetch_jobs()`, which
  deliberately swallows per-tenant errors for the discovery cycle's sake. Verification needs the
  un-isolated outcome for exactly one tenant, hence the separate module.
- `app/registry/verification.py`, `lifecycle.py` — the two-step verification pipeline
  (structural probe, then best-effort enrichment) and lifecycle transitions (promotion,
  permanent-vs-temporary-failure-aware demotion, migration detection).
- `app/registry/quality.py` — deterministic, rule-based confidence scoring with human-readable
  reasons (never an opaque probability).
- `app/registry/sharding.py` — deterministic `portal_id -> shard` hashing
  (`REGISTRY_SHARD_COUNT`/`REGISTRY_SHARD_INDEX`, default 1/0), groundwork for a future
  distributed worker (no distributed infrastructure built).
- `app/registry/sync.py` — the single bridge from the Phase 4 layer to the unchanged Phase 3
  `company_registry` table: mirrors a `VERIFIED`/`ACTIVE` portal in (promoting it to `ACTIVE`),
  disables (never deletes) the mirrored row when a portal regresses.
- `app/registry/page_discovery.py` — safe, bounded, robots.txt-respecting, JS-free career-page
  link discovery for a given company domain.
- `app/registry/doctor.py`, `analytics.py`, `export.py` — integrity checker, real DB-derived
  aggregate stats, streaming JSONL/JSON export (no candidate data).
- `app/providers/registry.py::workday_base_url` — small Phase 3 bug fix: Workday tenant
  identifiers coming from `app.providers.detector`'s short form (`tenant/wdHost/site`) are now
  reconstructed into the full CXS base URL `WorkdayProvider` actually needs; previously only a
  literal full base URL (the static-config shape) worked.
- Dashboard: `/registry` gained Phase 4 summary cards, filters, search, and a bounded portal
  table; `/registry/portals/{id}` (detail: provenance, confidence reasons, health, sibling
  portals, migration history, safe POST actions); `/registry/doctor`.
- `scripts/registry_benchmark.py` — synthetic 1k/10k/50k/100k scale benchmark, isolated temp
  SQLite DB only, never the real registry (`docs/registry-scaling.md`).

## Phase 5 — distributed polling execution layer + registry acquisition

See `docs/phase5-distributed-polling.md` (start here), `docs/worker-architecture.md`,
`docs/polling-leases.md`, `docs/registry-acquisition.md`, `docs/fleet-operations.md`,
`docs/scaling-claims.md` for full detail. Summary of what was added on top of Phase 4, without
rebuilding it (all 312 pre-existing tests still pass unmodified):

- `app/workers/` — the entire new package: `identity.py` (PII-free worker id), `models.py`
  (`WorkerStatus`/`AttemptStatus`/`CircuitState`/`LeasedWorkItem`), `repo.py` (CRUD for the new
  execution tables), `leasing.py` (atomic claim/release/extend against `company_registry`/
  `registry_portals`), `queue.py` (`WorkQueue` abstraction), `retry.py` (centralized retry
  classification + backoff), `circuit.py` (per-provider circuit breaker + inflight-concurrency
  slots), `schema_check.py` (schema-drift vs. empty-board detection), `dead_letter.py`,
  `metrics.py` (honest stored-vs-monitored metrics), `runner.py` (`Worker`, the actual execution
  loop, reusing `app.agent.cycle.process_raw_job` and `app.registry.verification.verify_portal`
  entirely unchanged), `supervisor.py` (local dev multi-process spawner, no shell involved),
  `cli.py` (`run`/`status`/`attempts`/`dead-letter`).
- `app/registry/acquisition.py` — resumable acquisition batch executor (checkpointed resume,
  per-record failure isolation, immediate live verification of new candidates), on top of the
  unchanged Phase 4 `importers.py`/`verification.py`/`lifecycle.py`/`sync.py`.
- New, additive tables/columns (`app/db.py`): `poll_attempts`, `workers`, `dead_letters`,
  `provider_circuit_state`, `registry_acquisition_batches`, plus lease columns on
  `company_registry` and `registry_portals`. WAL mode + `busy_timeout` now configured on every
  connection for safe multi-process concurrency.
- Dashboard: `/fleet` (workers, attempts, dead letters, fleet metrics, discovery latency),
  `/acquisition` (batch progress + resume), `/fleet/metrics` (JSON).
- `scripts/worker_benchmark.py` — synthetic 1k/10k/50k/100k `company_registry` leasing/attempt
  benchmark, isolated temp SQLite DB only.

## Phase 6: production-scale distributed architecture

See `docs/phase6-production-scale.md` for the full map. Summary of what
moved:

- `app/db.py` now dispatches to either SQLite (unchanged) or PostgreSQL
  (`app/db_postgres.py`, a thin connection/cursor wrapper, not SQLAlchemy)
  based on `DATABASE_URL`. Same schema (mechanically translated), same
  `get_connection()`/`db_session()`/`init_db()` surface every other module
  already calls.
- `app/migrations.py` — real, versioned, idempotent schema migrations
  (`schema_migrations` table), used for every Phase 6 schema change; Phase
  1-5's schema is the implicit baseline version.
- `app/workers/leasing_postgres.py` — `SELECT ... FOR UPDATE SKIP LOCKED`
  claiming for Postgres, dispatched from the same `app/workers/leasing.py`
  functions every caller already uses.
- `app/workers/reaper.py`, `app/workers/identity.py` (extended) — orphan
  worker detection + multi-machine identity metadata.
- `app/providers/errors.py` — structured `ProviderFetchResult`, wired into
  `app/workers/runner.py`; see `docs/provider-error-contract.md`.
- `app/workers/schema_drift_repo.py` — persistent schema-drift tracking,
  feeding the circuit breaker for provider-wide drift.
- `app/registry/acquisition_records.py`, `app/registry/domain_seed.py`,
  `app/registry/acquisition_priority.py` — distributed acquisition
  checkpointing, the domain-seed pipeline, and priority scoring; see
  `docs/registry-acquisition.md`.
- `app/sponsorship/evidence.py` — sponsorship-evidence storage foundation
  for Phase 7 (never read by the sponsorship classifier).
- `app/observability/` (`metrics.py`, `logging_config.py`), `app/health.py`
  — `/metrics`, `/readiness`, structured logging, correlation ids; see
  `docs/production-observability.md`.
- `app/db_migrate.py` — SQLite → PostgreSQL data migration tool; see
  `docs/database-migration.md`.
- `scripts/multi_machine_simulation.py`, `scripts/phase6_scale_benchmark.py`
  — distributed-coordination acceptance simulation and a separate Phase 6
  synthetic scale benchmark (SQLite and, when available, real Postgres).

## Phase 7: sponsorship intelligence

See `docs/phase7-sponsorship-intelligence.md` for the full map. Summary:

- `app/sponsorship/evidence.py` (extended, additive columns) — the full
  evidence schema (source type/quality, dataset linkage, occupation/
  location detail, idempotency key). Still never imported by
  `app/sponsorship/classifier.py`.
- `app/sponsorship/schema.py` — shared enums (`SourceType`, `SourceQuality`,
  `AliasType`, `RelationshipType`, `HistoricalStrength`,
  `RoleSimilarityTier`, `RecencyBucket`) and the deterministic
  source-quality/recency weight tables.
- `app/sponsorship/datasets.py` — dataset versioning
  (`sponsorship_datasets`).
- `app/sponsorship/importers.py` — streaming/batched/idempotent/resumable
  USCIS H-1B Employer Data Hub + DOL OFLC LCA disclosure CSV importers; see
  `docs/sponsorship-data-import.md`.
- `app/sponsorship/aliases.py`, `app/sponsorship/relationships.py`,
  `app/sponsorship/identity.py` — company alias model, parent/subsidiary
  safety, deterministic employer identity resolution (never merges on name
  similarity alone); see `docs/employer-identity-resolution.md`.
- `app/sponsorship/similarity.py`, `app/sponsorship/profile.py` — role/
  occupation/location similarity and the cached, recomputed-on-import
  employer historical profile + `history_score` (a relative ranking signal,
  never a probability).
- `app/sponsorship/decision.py` — the ONE place current-role evidence
  (`classifier.py`) and historical evidence (`profile.py`) are combined,
  with a persisted, versioned decision audit trail
  (`sponsorship_decisions`); see `docs/sponsorship-decision-engine.md`.
- `app/sponsorship/classifier.py` (extended, still current-role only) —
  added negation-safety, conditional-language, and same-JD conflict
  detection (`classify_sponsorship_detailed`); the original
  `classify_sponsorship()` two-tuple signature and behavior are unchanged.
- `app/sponsorship/review_queue.py`, `app/sponsorship/doctor.py`,
  `app/sponsorship/cli.py`, `app/sponsorship/metrics.py`,
  `app/sponsorship/acquisition_integration.py` — review queue ordering,
  integrity checks, operational CLI, `/metrics` additions, and the (signal-
  only, never priority-score-owning) wire-up of real evidence into
  `app.registry.acquisition_priority`'s existing `has_sponsorship_history_signal`
  input.
- `app/pipeline.py` — `analyze_job()` now calls
  `app.sponsorship.decision.persist_decision()` instead of
  `classify_sponsorship()` directly; new `reanalyze_job()` entry point for
  JD-change detection, safe on terminal (`APPLIED`/`INTERVIEW`/`REJECTED`)
  jobs.
- Dashboard: `/companies`, `/companies/{id}`, `/sponsorship/review-queue`,
  `/sponsorship/doctor`, `/sponsorship/identity-review`, plus a decision
  panel on `/jobs/{id}` and a `historical_strength` dashboard filter; see
  `docs/sponsorship-review-operations.md`.
- `scripts/sponsorship_benchmark.py` — synthetic 10k/100k(+500k/1M optional)
  evidence-import/profile/lookup benchmark, isolated temp SQLite DB only.

## Phase 8: safe ATS application executor

See `docs/phase8-application-executor.md`, `docs/application-provider-interface.md`,
`docs/application-field-mapping.md`, `docs/application-safety.md`,
`docs/application-state-machine.md`, `docs/application-operations.md`.

- `app/applications/models.py` — `ExecutionStatus` (the fine-grained,
  per-attempt state machine, stored on `application_executions.status`),
  `ExecutionMode` (ASSIST/AUTO_PERMITTED), `AutomationPolicy`/`PolicyReason`,
  `FieldCategory`/`FieldConfidence`, `ApplicationField`,
  `ApplicationCapabilities`, `FormSnapshot`/`FormField`, and the
  submit/confirmation result dataclasses.
- `app/applications/eligibility.py` — the pre-execution gate: FULL_TIME hard
  gate (unconditional, first check), US-location, CS/STEM role, seniority,
  compensation, technical-match threshold, sponsorship-status branching,
  resume-artifact presence, answer completeness, terminal-state check.
  Re-derives every check independently of `jobs.application_state`.
- `app/applications/schema.py` — maps `candidate_data/profile.json` (the
  ONLY candidate truth source) into the generic `ApplicationField` schema;
  legal/attestation fields are always `needs_user_input=True` (never present
  in the profile schema by design).
- `app/applications/mapping.py` — deterministic label/alias field-matching
  engine (EXACT/HIGH/MEDIUM); the MEDIUM token-overlap fallback never
  applies to legal/demographic/consent/signature field categories.
- `app/applications/provider.py` + `provider_registry.py` — the
  `ApplicationProvider` interface (separate from `app.providers.base.
  JobProvider`) and its registry. Adapters: `providers_greenhouse.py`
  (form discovery live-verified against the real public Job Board API,
  submission NOT implemented — ASSIST_ONLY), `providers_lever.py` (live-
  checked: no structured question schema available — UNSUPPORTED),
  `providers_generic.py` (fallback for every other known ATS — apply-URL
  only), `mock_ats.py` (deterministic in-process fixture ATS — the only
  provider with `submission_supported=True`, used for executor-mechanics
  testing only).
- `app/applications/executor.py` — `queue_application()` (the safe front
  door: eligibility + duplicate + executor-enabled checks, creates an
  `application_executions` row) and `process_execution()` (prepare → map →
  fill → validate → (submit) → confirm, synchronous).
- `app/applications/repo.py` — persistence for `application_executions`
  (partial-unique-indexed on `job_id WHERE active=1` — the actual
  distributed duplicate-submission guard), `application_answer_snapshots`,
  `application_audit_log`, and `mirror_job_state()` (the two-layer state
  bridge onto `jobs.application_state`).
- `app/applications/queue.py` — atomic lease-claim queue for
  `application_executions`, same `UPDATE ... WHERE` pattern as
  `app.workers.leasing`.
- `app/applications/duplicate.py`, `rate_limit.py`, `reconcile.py`,
  `doctor.py`, `metrics.py`, `cli.py` — duplicate-application protection,
  hourly/daily/per-company rate limits (DB-query-based, already fleet-wide),
  explicit human reconciliation for `SUBMISSION_STATUS_UNKNOWN`, the
  read-only "application doctor" integrity checker, `/metrics` additions,
  and the operational CLI.
- `app/applications/fingerprint.py` — application-form structural
  fingerprinting + `application_form_baselines` drift detection, distinct
  from Phase 6's discovery-payload `provider_schema_drift`.
- `app/matching/employment_type.py` (extended) — `classify_employment_type()`,
  a POSITIVE classifier (`EmploymentType` enum in `app.models`) distinct
  from the existing permissive `is_full_time()` boolean (kept unchanged,
  still used by `app.agent.cycle`'s discovery-time filter).
- Dashboard: `/applications` (bucketed queue view + live metrics + executor
  on/off banner), `/applications/doctor`, a job-detail "Application
  execution" card with Prepare/Queue/Retry/Reconcile actions, and 3 new
  JSON API endpoints (`/api/applications/metrics`, `/api/jobs/{id}/eligibility`,
  `/api/executions/{id}`).
- `APPLICATION_EXECUTOR_ENABLED` / `AUTO_SUBMIT_ENABLED` — both default
  `false`; printed on every startup (never silently enabled).

## Phase 9: production application-worker fleet

See `docs/phase9-production-application-workers.md`,
`docs/application-worker-architecture.md`, `docs/application-reconciliation.md`,
`docs/application-browser-assist.md`, `docs/application-provider-capabilities.md`.

- `app/applications/worker.py` — `ApplicationWorker`, the standalone
  distributed daemon (`python -m app.applications.worker run`) that
  continuously drives Phase 8's `process_execution()` pipeline: claim (via
  Phase 8's already-built `app.applications.queue`) → submission-circuit
  check → execute → attempt-history record → lease release. Declares only
  `APPLICATION_PREPARE`/`APPLICATION_SUBMIT` worker capabilities, logically
  separate from the discovery worker fleet.
- `app/applications/supervisor.py` — local multi-process supervisor,
  mirroring `app.workers.supervisor.Supervisor`.
- `app/applications/circuit.py` — a SEPARATE submission circuit breaker
  (own table `application_provider_circuit_state`) from the discovery
  circuit breaker, tripped more conservatively.
- `app/applications/attempts.py` — per-attempt history (`application_attempts`).
- `app/applications/scheduler.py` — continuous auto-prepare scheduler
  (`APPLICATION_AUTO_PREPARE_ENABLED`, independent of `AUTO_SUBMIT_ENABLED`).
- `app/applications/budget.py` — deterministic daily budget accounting.
- `app/applications/reconcile_worker.py` — automated reconciliation
  *evidence* pass; never itself resolves an execution — always funnels
  through the existing `reconcile_execution()`.
- `app/applications/capability_matrix.py` — the truthful, generated
  provider capability report.
- `app/applications/browser_assist.py` — optional Playwright-based visible-
  browser preparation aid (`BROWSER_ASSIST_ENABLED`, off by default);
  never clicks submit, never persists cookies/sessions/passwords.
- Two real bugs caught and fixed by this phase's own testing: a
  resumed-`SUBMITTING`-row double-submit risk in `executor.process_execution()`
  (now converts to `SUBMISSION_STATUS_UNKNOWN` instead), and a Postgres
  `bool`→`INTEGER` column type mismatch in `app.jobs_repo.insert_job`/
  `update_job` (only ever manifested against real Postgres, never SQLite).
- Dashboard: `/application-workers` (fleet/circuits/attempts),
  `/applications/capability-matrix`, manual scheduler/reconcile-worker
  triggers, daily-budget/fleet summaries folded into `/applications`.
