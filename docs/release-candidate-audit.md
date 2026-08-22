# Phase 15 — Release-Candidate Audit

This document records the Phase 15 (final production-readiness) audit: what was reviewed,
what was found, what was changed, and the final acceptance results. Phases 1-14 are not
re-summarized here -- see `docs/acceptance_verification.md` and each phase's own doc.

## 1. Architecture / dead-code audit (CLAUDE.md Phase 15 section 6)

Method: `pyflakes` over `app/` (unused imports/names), plus a cross-reference scan for any
`.py` file under `app/` never imported anywhere else in `app/`/`tests/`.

**Findings:**

- **No dead/orphaned modules.** Every file under `app/` is genuinely imported somewhere
  (the one apparent candidate, `app/workers/retry.py`, turned out to be a false positive
  of the grep pattern used -- it's imported by `app/workers/runner.py` in a
  multi-name `from app.workers import circuit, dead_letter, reaper, retry, schema_check`
  statement).
- **18 files had a genuinely unused import** (leftover from earlier refactors -- e.g. an
  enum imported for a check that was later moved elsewhere). All removed; full test suite
  re-run clean afterward (see §11). List: `app/db_postgres.py`, `app/health.py`,
  `app/observability/metrics.py`, `app/sponsorship/evidence.py`,
  `app/sponsorship/relationships.py`, `app/applications/browser_capability_matrix.py`,
  `app/applications/capability_evidence.py`, `app/applications/canary.py`,
  `app/applications/provider_health.py`, `app/registry/lifecycle.py`,
  `app/registry/store.py`, `app/providers/errors.py`, `app/workers/metrics.py`,
  `app/workers/circuit.py`, `app/workers/queue.py`, `app/workers/models.py`,
  `app/workers/repo.py`, `app/providers/registry.py`.
- **One dead local variable**: `app/providers/registry.py::build_provider_for_tenant()`
  computed `factory = _PROVIDER_FACTORIES.get(...)` and never used it (the function
  actually dispatches on `cls`/`_PROVIDER_CLASSES`). Removed; `_PROVIDER_FACTORIES` is
  still genuinely used by its other caller.
- **One vestigial `nonlocal`**: `app/sponsorship/importers.py::_run_import`'s inner
  `flush()` declared `nonlocal ambiguous, unmatched` but never read or wrote either --
  both are actually mutated directly in the enclosing `for` loop, not inside `flush()`.
  Removed the unnecessary declaration; no behavior change (Python doesn't require
  `nonlocal` for names a function never assigns).
- **No duplicate/contradictory capability truth found.** `app.applications
  .capability_matrix` is confirmed pure presentation over
  `app.applications.provider_registry.all_application_capabilities()` (its own docstring
  states this and the code matches); `app.applications.browser_capability_matrix` is a
  genuinely separate, intentionally-distinct data source (live-browser verification
  evidence, not automation policy) per Phase 11's own design note. See
  `docs/provider-capability-matrix.md` (new this phase) for the single generated merge.
- **No unsafe direct DB access found** outside the sanctioned `db_session()` /
  `WorkQueue` / `app.applications.queue` interfaces the per-phase CLAUDE.md rules already
  require and the existing doctors already check for regressions.
- **`app/providers/base.py:99`** (`fetch_jobs_result`'s return-type annotation
  `"ProviderFetchResult"`) is flagged by `pyflakes` as an undefined name. Verified this is
  a false positive: it's a deliberate forward-reference string annotation, and the actual
  name is imported locally inside the function body
  (`from app.providers.errors import ProviderFetchResult, ...`) specifically to avoid a
  circular import between `base.py` and `errors.py`. No runtime code ever evaluates this
  annotation eagerly (no `typing.get_type_hints()` call site exists for this class), so
  left unchanged -- "fixing" it would mean importing at module level and reintroducing the
  circular-import problem this pattern exists to avoid.

**Conclusion:** the codebase does not need a rewrite or restructuring going into the
release candidate. Cleanup performed was mechanical, low-risk, and fully covered by the
existing test suite (re-run clean before and after, §11).

## 2. State-consistency check ownership (CLAUDE.md Phase 15 section 9)

Every "impossible combination" example named in the Phase 15 build brief was checked
against the existing per-subsystem doctors before writing any new code, per section 10's
"reuse existing doctors" instruction. All four are already covered:

| Impossible combination | Owning check | Location |
|---|---|---|
| APPLIED without confirmation | `_check_applied_without_confirmation` | `app/applications/doctor.py` |
| READY_TO_APPLY / active execution with a stale resume | `_check_stale_resume_jd_mismatch` | `app/applications/doctor.py` |
| NO_SPONSORSHIP with an active/submitted execution | `_check_unknown_sponsorship_submitted` (its `no_sponsorship_submitted` branch) | `app/applications/doctor.py` |
| CONTRACT (non-FULL_TIME) with an active application | `_check_non_full_time_in_submission` / `_check_non_full_time_queued` | `app/applications/doctor.py` |

`app/doctor.py` (new this phase) is the single `python -m app.doctor` entry point that
runs all four subsystem doctors (registry, sponsorship, applications, resume optimizer)
together, plus checks that don't belong to any one subsystem: database/schema
reachability, candidate-profile completeness, configuration validity
(`app/config_doctor.py`, new), basic job-row integrity, and dead-letter backlog. It
deliberately does not re-implement any check already owned by a subsystem doctor.

## 3. Resume-optimization worker capability decision (CLAUDE.md Phase 15 sections 30-31)

**Decision: keep the Phase 14 lightweight asyncio scheduler
(`app.resume_optimizer.scheduler`); do not add a leased `RESUME_OPTIMIZATION`
`WorkerCapability`.**

Reasoning:

- The workload genuinely is low-volume relative to discovery/application work -- one
  optimization pass per job, not a continuously arriving stream.
- `optimize_resume()` already has real, tested, database-level concurrency safety
  independent of which scheduling mechanism calls it: `resume_variants`' two unique
  indexes (`(job_id, jd_fingerprint, profile_version, optimizer_version)` for idempotency,
  and a partial `(job_id) WHERE current = 1` for single-current-variant) are enforced by
  Postgres/SQLite itself via `app.resume_optimizer.repo.claim_variant()`'s
  catch-the-unique-violation-and-refetch pattern -- already proven under concurrent
  Postgres callers in Phase 14's own test suite (`tests/test_resume_optimizer_postgres.py`).
  Adding a second, leased-queue layer on top would duplicate a correctness guarantee that
  already exists at the data layer, not add one.
- The manual Generate/Regenerate dashboard action and the CLI remain synchronous and
  ungated by any scheduler regardless -- a leased worker fleet would only change how the
  *background auto-optimize* path is scheduled, and that path already runs correctly today
  as a single-process loop.
- A future genuine need for multi-machine parallel resume optimization (e.g. very large
  job volume) remains a reasonable thing to add later -- this decision is scoped to
  "not needed for this release candidate's expected scale," not "never valuable."

No code changed for this decision; it's recorded here as the deliberate scope call CLAUDE.md
Phase 15 section 30 asked for ("If the current architecture is sufficient... document why
and do not add complexity merely to satisfy this prompt").

## 4. Manual vs. agent-confirmed application provenance (CLAUDE.md Phase 15 section 74)

**Already fully supported by existing architecture** -- no new code needed.
`app.jobs_repo.record_state_change(job_id, from_state, to_state, actor=...)` has carried an
`actor` column since Phase 2/8, and every real call site already passes a distinct, honest
value:

- `actor="user"` -- a manual dashboard state change (`app/main.py`'s
  `POST /jobs/{job_id}/state` handler)
- `actor="executor"` -- an automated executor-driven transition
  (`app/applications/repo.py`, `app/applications/executor.py`)
- `actor="system"` (default) -- an ordinary pipeline-driven transition

Additionally, `app.applications.reconcile.reconcile_execution()`'s `resolution` parameter
already distinguishes `"confirmed_applied"` (operator found independent evidence the
executor's submission genuinely went through) from `"manual_applied"` (operator applied
entirely outside the executor, e.g. after a CAPTCHA stop) -- both are logged with a
distinct event detail string. The `jobs.state_history` table (queryable per job) is
therefore the honest, queryable AGENT_CONFIRMED-vs-USER_MARKED_APPLIED record CLAUDE.md
section 74 asked for.

## 5. Gap closure: release-candidate performance benchmark (CLAUDE.md Phase 15 sections 44, 65)

`scripts/phase15_release_benchmark.py` (new) reuses existing benchmark infrastructure
rather than duplicating it: it imports and calls `scripts/registry_benchmark.py::run_benchmark()`
directly for registry/due queries, and `scripts/resume_optimizer_benchmark.py::run_benchmark()`
directly for JD analysis, evidence matching, and resume generation (`optimize_resume()`
already runs ATS parse validation internally as part of that call). It adds new
measurements for the three items no prior benchmark script covered: standalone ATS parse
validation timing, unified-dashboard query performance, and application-queue-claim
performance (`app.applications.queue.claim_execution_batch`, mirroring
`scripts/worker_benchmark.py`'s leasing-benchmark style including an 8-worker contention
check).

Everything ran against an isolated temp SQLite database (never `data/app.db`); every
synthetic row used provider name `"benchmark-fixture"`. Run at 1,000 / 10,000 / 50,000 /
**100,000** rows -- all four sizes completed.

**Results** (`python scripts/phase15_release_benchmark.py --sizes 1000,10000,50000` plus a
separate `--sizes 100000` run):

| Operation | 1,000 | 10,000 | 50,000 | 100,000 |
|---|---|---|---|---|
| Registry bulk import | 0.03s | 0.11s | 0.55s | 1.18s |
| Registry due-portal query | 0.003s | 0.003s | 0.003s | 0.003s |
| JD analysis (mean) | 1.0ms | -- | -- | 1.44ms |
| Evidence matching (mean) | 0.05ms | -- | -- | 0.05ms |
| Full resume optimize incl. ATS parse (mean) | 62.7ms | -- | -- | 65.5ms |
| Standalone ATS parse validation (mean, 200 runs) | 9.7ms | -- | -- | 9.6ms |
| Dashboard: `list_jobs({})` (unbounded, summary) | 0.02s | 0.34s | 1.6s | 3.1s |
| Dashboard: full HTTP `GET /` (end to end) | 0.14s | 1.0s | 4.2s* | 8.6s* |
| Dashboard: rendered response size | 492KB | 4.9MB | 334KB* | 332KB* |
| Application queue: single claim (50) | 0.01s | 0.01s | 0.02s | 0.02s |
| Application queue: 8-worker contention drain | 0.22s | 0.59s | 2.8s | 7.1s |
| Application queue: duplicate claims | 0 | 0 | 0 | 0 |

\* After the row-cap fix in §6 below (the pre-fix 50,000-row response was 24MB / 5.7s).

**What this proves:** these specific queries/operations, against synthetic data of this
exact shape, complete in the times shown, on this one development machine, on this date.
Registry/due queries, JD analysis, evidence matching, and application-queue claiming all
stay sub-second (registry due-query stays flat because it's an indexed, already-bounded
`LIMIT` query regardless of table size) at every tested scale including 100,000 rows, with
zero duplicate claims under 8-worker contention at every size. Resume generation and ATS
parse validation are per-job operations with no dependency on total table size, so they're
flat by construction.

**What this does NOT prove:** real network-polling throughput (no HTTP requests were made
to any real provider), real multi-machine/distributed behavior (see
`tests/test_postgres_leasing.py` / `tests/test_multi_machine_simulation.py` for that
instead), interview/application/hiring outcomes for any candidate, or that this exact
timing holds on different hardware. It also does not model realistic data skew (every
synthetic job/execution is structurally uniform; a real dataset's actual query plans could
differ).

## 6. Gap closure: large-state dashboard validation (CLAUDE.md Phase 15 sections 42-44)

Ran the dashboard (`GET /`) against the same isolated synthetic datasets above at
1,000 / 10,000 / 50,000 / **100,000** jobs -- all four sizes completed; no scale had to be
skipped.

**Findings, and what was fixed:**

1. **JD/resume optimization is never recomputed on page load.** Confirmed by inspection
   and by the benchmark: the dashboard route reads `resume_variants`/
   `resume_quality_reports` (already-persisted, `current=1`) via
   `app.resume_optimizer.repo.get_current_variants_for_jobs()`/
   `get_quality_reports_for_jobs()` -- both single batched queries, both sub-20ms even at
   100,000 jobs. No call to `optimize_resume()` or `analyze_jd()` exists anywhere in the
   dashboard request path. **PASS, no change needed.**
2. **A genuine N+1 query pattern was found and fixed.** `app/main.py`'s dashboard route
   was calling `app.applications.repo.get_active_execution_for_job(jid)` once PER JOB in a
   dict comprehension -- one query per row, unlike the two sibling lookups
   (variant/quality) which were already correctly batched. Added
   `get_active_executions_for_jobs(job_ids)` to `app/applications/repo.py`, mirroring the
   exact existing batched pattern, and switched the dashboard route to use it. Verified
   with a regression test (`tests/test_dashboard_batched_execution_lookup.py`) that
   asserts exactly one SQL query fires for N jobs, using `sqlite3.Connection.set_trace_callback`
   to trace every statement actually executed (not a `sqlite3.Connection` method
   monkeypatch, which fails -- it's an immutable C type).
3. **A genuine unbounded-result-set problem was found and fixed.** The rendered pipeline
   table had no row cap: at 50,000 synthetic jobs the dashboard returned a 24MB HTML
   response in 5.7 seconds. Root cause: `app.jobs_repo.list_jobs()` has no `LIMIT`, and
   every actionable/filtered job was rendered as its own table row. Fixed conservatively --
   no change to `list_jobs()`'s signature or behavior (still unbounded by default, still
   used unmodified for the summary-card counts, which correctly need the true global
   counts), no new pagination UI, no filter-behavior change. Only the final,
   already-fully-filtered `jobs` list is now sliced to `config.DASHBOARD_MAX_TABLE_ROWS`
   (new config constant, default 500) immediately before rendering -- applied LAST, after
   `resume_status`/`needs_action_only` have already narrowed the set, so those filters
   still search the complete matching set, never just the first page (verified by
   `tests/test_dashboard_row_cap.py::test_needs_action_filter_searches_beyond_the_cap`,
   which plants the matching job at position 20 of 20 against a cap of 5 and confirms it's
   still found). Result: response size is now flat regardless of table size (492KB at
   1,000 jobs, 332KB at 100,000 jobs) and full end-to-end request time dropped from 5.7s
   to 4.2s at 50,000 jobs. The template shows an honest "Showing top N of M matching jobs"
   note whenever the true count exceeds the cap.
4. **Summary-card counts (`compute_pipeline_summary`) still scan every job.** This was
   NOT changed. `total_discovered`/`full_time_eligible` need the actual `Job` objects
   because full-time eligibility runs `app.matching.employment_type.classify_employment_type()`
   against each job's title/description text, not a single indexed column -- this is a
   documented, deliberate Phase 14 tradeoff (see that function's own docstring: "this
   project is a local, single-user job agent... classifying an already-in-memory page's
   worth of jobs in Python is the right tradeoff over a redundant indexed column").
   Benchmark confirms the real cost: 3.1s at 100,000 jobs. This is honestly reported as a
   **known, accepted scaling limit**, not fixed here -- caching a redundant classified
   column, or reimplementing the classifier in SQL, would be a materially larger and
   riskier change than "fix conservatively" calls for, and 100,000 jobs is far beyond this
   product's realistic single-user scale (the real `data/app.db` has 445 jobs after
   extensive multi-phase development).
5. **Functional correctness at scale**: every tested size returned HTTP 200 with a
   well-formed dashboard page; the 8-worker application-queue-claim contention test showed
   zero duplicate claims at every size up to 100,000.
6. **Empty state**: already covered by the existing default pytest suite
   (`tests/test_agent_dashboard.py`, `tests/test_api.py`) against a freshly-initialized
   empty database -- re-verified passing as part of this gap's regression run (§11).

## 7. New this phase

| File | Purpose |
|---|---|
| `app/doctor.py` | Global doctor -- `python -m app.doctor` |
| `app/config_doctor.py` | Configuration validation |
| `app/acceptance.py`, `scripts/release_acceptance.sh` | Release-candidate acceptance runner |
| `app/resume_optimizer/visual_regression.py` | Resume DOCX/PDF layout regression checks (page count, blank pages, headings, bullets, contact info) beyond text extraction alone |
| `scripts/secret_scan.py` | Deterministic local secret/private-artifact scanner |
| `scripts/generate_provider_matrix.py` | Live-generated authoritative provider capability matrix |
| `scripts/export_tracking.py` | Safe job/application tracking export (CSV/JSON), never the candidate profile |
| `/version` endpoint (`app/main.py`) | Release/schema/optimizer/classifier/capability version identifiers |
| `README.md` (root) | Product README |
| `docs/README.md` | Documentation index |
| `docs/operations-runbook.md`, `docs/backup-restore.md`, `docs/data-retention.md`, `docs/troubleshooting.md`, `docs/provider-capability-matrix.md`, this file | New operational/reference docs |
| `start.sh` | Extended startup summary: schema version, registry portal count (honest wording), resume-optimization status |
| `scripts/phase15_release_benchmark.py` | Final release-candidate performance benchmark (§5); reuses `registry_benchmark.py`/`resume_optimizer_benchmark.py` |
| `app/applications/repo.py::get_active_executions_for_jobs()` | Batched execution lookup, fixing a genuine N+1 query pattern (§6) |
| `app/config.py::DASHBOARD_MAX_TABLE_ROWS` + `app/main.py` dashboard route + `app/templates/dashboard.html` | Bounds the rendered pipeline table to fix a genuine unbounded-response-size problem (§6) |
| Tests | `tests/test_global_doctor.py`, `tests/test_config_doctor.py`, `tests/test_secret_scan.py`, `tests/test_release_acceptance.py`, `tests/test_resume_visual_regression.py`, `tests/test_generate_provider_matrix.py`, `tests/test_export_tracking.py`, `tests/test_dashboard_batched_execution_lookup.py`, `tests/test_dashboard_row_cap.py` (50 new tests total) |

## 8. Provider capability truth (CLAUDE.md Phase 15 sections 71-72)

Live-generated via `python scripts/generate_provider_matrix.py` during this phase's
acceptance run:

```
Providers with auto-submit=True: ['mock_ats']
```

Confirmed: **every real ATS provider connector remains ASSIST_ONLY.** `mock_ats` (an
in-process deterministic test fixture) is the only provider with
`submission_supported=True`. No real production application was submitted during this
phase's development or validation.

## 9. Release-candidate limitations (CLAUDE.md Phase 15 section 92)

- Real-ATS automatic final submission remains unsupported unless explicitly proven
  otherwise, per-provider, with genuine live evidence (see
  `app.applications.capability_evidence`) -- there is no generic "just enable it" switch.
- CAPTCHA, MFA/login, and unresolved legal/attestation questions always require a human;
  none of these are ever bypassed or guessed.
- Workday behavior is tenant/site-specific and tracked per-`(tenant, site)`
  (`app.applications.workday_tenant`) -- never generalized from one tenant's observed
  behavior to "Workday works."
- Historical sponsorship evidence is prioritization/review signal only; it can promote
  `UNKNOWN → LIKELY_SPONSOR` and never more (never `CONFIRMED_SPONSOR`, never overrides
  `NO_SPONSORSHIP`).
- Internal JD/ATS-alignment diagnostics (required/preferred-skill coverage, internal
  alignment score) are transparency tooling over this project's own matching logic, not a
  proprietary ATS's real scoring engine, and carry no claim about interview or hire
  probability.
- No guarantee of interview, job offer, or any hiring outcome.
- Live provider interfaces (ATS job boards and application forms) can and do change
  without notice; provider health/circuit-breaker/schema-drift infrastructure detects and
  isolates this per-provider but cannot prevent it.
- Real public ATS validation in this project is always bounded, read-only, and
  opportunistic (whatever real postings happen to be live/reachable at run time) --
  never a claim of exhaustive or permanent coverage.

## 10. Ongoing work classification (CLAUDE.md Phase 15 section 93)

No further large numbered phase is planned. Future work falls into:

- **Normal maintenance**: dependency updates, provider connector adjustments as real ATS
  markup/APIs change, documentation upkeep.
- **Bug fixes**: as discovered through normal use or a future doctor/acceptance run.
- **Optional future features** (not committed, not scoped): a leased
  `RESUME_OPTIMIZATION` worker capability if volume genuinely grows (§3 above); expanding
  `browser_capability_matrix`/`workday_tenant` coverage as more real tenants are
  genuinely, live-verified; a formal data-retention/expiry policy at the deployment layer
  (`docs/data-retention.md`) if a specific deployment needs one.

## 11. Final acceptance results

See the root `README.md` and the output of `./scripts/release_acceptance.sh` /
`python -m app.acceptance` for the authoritative, reproducible final numbers (test counts,
doctor results, migration check, secret scan, gitignore audit). Final numbers, after
closing the two gaps in §5/§6:

- Default pytest: **1125 passed**, 0 failed (Phase 14 baseline 1075 + 50 new Phase 15 tests).
- PostgreSQL suite (`pytest -m postgres`, via bundled `pgserver`): **44 passed**, 0 failed
  (re-run after the dashboard/repo changes in §6 since those touch DB query code).
- Real-browser suite (`pytest -m browser`, real Chromium): **57 passed**, 0 failed.
- `python -m app.doctor`: **0 serious issues**, 2 informational warnings against the real
  local database (both re-verified this pass, read-only, real DB never modified):
  - `DATABASE_URL` unset -- expected/correct for the safe local-SQLite default; the
    message itself says "Fine for LOCAL_DEVELOPMENT; set a postgres:// URL for
    PRODUCTION." Confirmed informational only, not release-blocking.
  - One pre-existing job row (id 445, company "Acme Corp", blank title, created
    2026-08-22T17:10:34Z -- predates this session's own work) with an empty title.
    Confirmed via a read-only `SELECT` (no write ever issued against the real DB) that
    this is exactly one historical/data-hygiene row from earlier interactive testing, not
    a defect the pipeline can currently produce, and the doctor already correctly reports
    it as `severity="warning"` (not `"serious"`) -- it does not fail the doctor, the
    acceptance runner, or any test.
- `python -m app.resume_optimizer.cli doctor`: 0 serious, 0 warnings.
- `python -m app.applications.cli doctor`: 0 serious, 0 warnings.
- `python scripts/secret_scan.py` / `--all`: no findings against tracked files (the 4
  `--all` findings are deliberate fake-secret fixtures inside `tests/test_secret_scan.py`/
  `tests/test_config_doctor.py`, used to test the scanner itself).
- Fresh-SQLite migration check (throwaway temp DB, never the real `data/app.db`): schema
  reaches the expected current version cleanly.
- Startup smoke test: `./start.sh` launched (twice -- once before, once after the §6
  fixes), `/health`, `/readiness`, `/version`, `/metrics`, `/`, `/fleet`, `/applications`,
  `/registry` all returned successfully, dashboard confirmed rendering correctly against
  the real 445-row database (well under the new 500-row cap, so no cap note shown, exactly
  as expected), server stopped cleanly both times.
- Release-candidate performance benchmark (§5) and large-state dashboard validation (§6):
  both completed at all four required scales -- 1,000 / 10,000 / 50,000 / **100,000** --
  nothing had to be skipped for this environment.
