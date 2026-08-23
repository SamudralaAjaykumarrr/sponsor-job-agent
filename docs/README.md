# Documentation Index

Start with the root `README.md` for the product overview and quick start. This index
groups every doc in this directory by topic so a future developer (or you, six months
from now) can find the right one without a search.

## Architecture & setup

- `architecture.md` -- module map, data flow, storage layout (start here)
- `provider-development.md` -- how to add a new discovery connector
- `provider-error-contract.md` -- the typed `ProviderFetchResult` error contract
- `postgres-backend.md`, `deployment-postgres.md` -- SQLite vs PostgreSQL, running Postgres locally/in production
- `database-migration.md` -- schema versioning framework, one-time SQLite→Postgres data migration tool

## Job discovery & registry

- `autonomous-agent.md` -- the background discovery loop
- `distributed-workers.md`, `worker-architecture.md`, `polling-leases.md` -- the leased, crash-recoverable worker fleet
- `company-registry.md`, `registry-operations.md`, `registry-acquisition.md`, `registry-verification.md`, `registry-import.md`, `registry-scaling.md` -- the company/career-portal registry lifecycle
- `provider-capabilities.md`, `provider-capability-matrix.md` -- what each ATS connector actually supports, discovery + application + browser-assist merged into one authoritative view
- `scaling-claims.md` -- the honest-vocabulary policy for any "N portals" claim

## Sponsorship

- `sponsorship_rules.md` -- the core CONFIRMED/LIKELY/UNKNOWN/NO_SPONSORSHIP rules
- `sponsorship-decision-engine.md` -- how historical evidence may (narrowly) upgrade UNKNOWN→LIKELY, never more
- `sponsorship-evidence-model.md`, `sponsorship-data-import.md` -- the evidence table and government-dataset importers
- `employer-identity-resolution.md` -- company/alias/relationship resolution, never name-similarity-only merging
- `sponsorship-review-operations.md` -- the manual review queue

## Resume generation & optimization

- `jd-analysis-model.md` -- JD requirement extraction
- `resume-evidence-matching.md` -- matching JD requirements against verified candidate evidence
- `resume-quality-diagnostics.md` -- internal alignment scoring (never a universal "ATS score")
- `ats-parse-validation.md` -- text-extraction parseability checks (DOCX/PDF/TXT)
- `phase14-resume-optimization-dashboard.md` -- the optimizer pipeline + unified dashboard build

## Application preparation & execution

- `application-state-machine.md`, `application-safety.md` -- the executor's gates and state machine
- `application-provider-interface.md`, `application-provider-capabilities.md` -- the `ApplicationProvider` interface and per-provider truth
- `application-field-mapping.md` -- form field discovery/mapping
- `application-worker-architecture.md` -- the distributed application worker fleet
- `application-operations.md` -- day-to-day application-layer operations
- `application-reconciliation.md`, `confirmation-evidence.md` -- resolving `SUBMISSION_STATUS_UNKNOWN`, what counts as real confirmation

## Real ATS browser assist

- `application-browser-assist.md`, `browser-assist-sessions.md`, `browser-session-reconstruction.md` -- session lifecycle, crash recovery
- `apply-entry-navigation.md`, `spa-application-navigation.md` -- safe apply-button/SPA navigation classification
- `application-checkpoints.md` -- best-effort observability log over the session lifecycle
- `application-job-identity.md`, `trusted-ats-redirects.md` -- the job-identity verification gate and redirect trust model
- `ats-capability-evidence.md`, `ats-canary-validation.md`, `provider-assist-health.md` -- live-verification evidence, canaries, health signal (never auto-disabling)
- Per-provider: `greenhouse-application-assist.md`, `smartrecruiters-application-assist.md`, `smartrecruiters-spa-validation.md`, `workday-application-assist.md`, `workday-observation-model.md`, `workday-tenant-validation.md`
- `real-ats-validation.md` -- bounded, read-only live validation runs against real public postings

## One-click agent

- `one-click-agent.md` -- the single START/STOP AGENT control, what it turns on, TEST MODE
- `autonomous-orchestration.md` -- why the orchestrator coordinates rather than duplicates
- `one-page-resume-contract.md` -- the one-page hard output contract and bounded compression ladder

## Dashboard & observability

- `unified-dashboard.md` -- the single-page primary workflow
- `production-observability.md` -- structured logging, metrics, correlation IDs

## Operations & deployment

- `operations-runbook.md` -- day-to-day operator reference (start here for running this in anger)
- `fleet-operations.md` -- discovery/application worker fleet operations
- `backup-restore.md` -- SQLite/PostgreSQL backup and restore procedures
- `data-retention.md` -- what's persisted, what deliberately isn't
- `troubleshooting.md` -- observed issues and factual fixes (Chromium libs, Postgres, ports, CAPTCHA, Workday, schema drift, stale resume, unknown submission)

## Acceptance & release history

- `acceptance_verification.md` -- the running, phase-by-phase acceptance log since Phase 1
- `phase2-plan.md` through `phase14-resume-optimization-dashboard.md` -- each phase's own build brief/detail doc
- `release-candidate-audit.md` -- **Phase 15**: architecture/dead-code audit, the
  resume-optimization-worker scope decision, the state-consistency-check ownership map,
  the release-candidate performance benchmark and large-state dashboard validation
  (including a genuine N+1 query fix and a dashboard result-set cap), final
  release-candidate limitations, and the final acceptance results
- `scripts/phase15_release_benchmark.py` -- the release-candidate performance benchmark
  itself (registry/dashboard/resume/queue-claim timing against synthetic data)
