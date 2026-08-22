# Phase 14: JD/Resume Optimization + Unified One-Page Dashboard

## Goal

Build the strongest truthful JD-specific resume optimization system possible, and consolidate
the day-to-day user experience into one primary dashboard, without ever inventing candidate
experience or promising a fake universal ATS score.

## What was built

| Module | Purpose |
|---|---|
| `app/resume_optimizer/models.py` (new) | Typed vocabulary: `RequirementCategory`, `EvidenceLevel`, `MatchStatus`, `ResumeVariantStatus`, `ATSParseStatus`, `AlignmentLabel`, and the `JDRequirementItem`/`JDAnalysisResult`/`SkillEvidence`/`EvidenceGraph`/`RequirementMatch` dataclasses |
| `app/resume_optimizer/jd_analysis.py` (new) | Pure JD-text -> normalized requirements extraction: required/preferred, negation/conditional-aware, years, education, certification, responsibilities, domain, sponsorship-language, salary. Never reads candidate data. |
| `app/resume_optimizer/evidence.py` (new) | Candidate evidence graph built from `CandidateProfile` only: skill -> supporting bullets/sources, evidence level (`DIRECT_VERIFIED`/`TRANSFERABLE_VERIFIED`/`FAMILIAR_ONLY`), responsibility/domain evidence |
| `app/resume_optimizer/matching.py` (new) | JD requirement <-> evidence matching: `MATCHED`/`PARTIAL`/`TRANSFERABLE`/`MISSING`/`UNSUPPORTED` per requirement, with evidence ids and an explanation string |
| `app/resume_optimizer/optimizer.py` (new) | Orchestration: truthful bullet/skill selection + reuses the unmodified `app.resume.claim_checker` + artifact writing + ATS parse validation + quality diagnostics, with idempotent/concurrency-safe variant creation |
| `app/resume_optimizer/ats_parse.py` (new) | DOCX/PDF/TXT parse validation via `python-docx`/`pypdf` -- PASS/WARN/FAIL with reasons, never a proprietary ATS score |
| `app/resume_optimizer/quality.py` (new) | Itemized, transparent coverage diagnostics -- never a universal "98% match" |
| `app/resume_optimizer/priority.py` (new) | Documented-weight match-priority ranking, explicitly not an interview probability |
| `app/resume_optimizer/repo.py` (new) | DB access for the 5 new tables, idempotent + concurrency-safe writes |
| `app/resume_optimizer/fingerprint.py` (new) | JD/profile/artifact fingerprinting |
| `app/resume_optimizer/scheduler.py` (new) | Optional background optimization loop (`RESUME_OPTIMIZATION_ENABLED`, off by default) |
| `app/resume_optimizer/doctor.py` / `cli.py` / `metrics.py` (new) | Integrity checks, `python -m app.resume_optimizer.cli analyze\|generate\|report\|doctor`, live metrics |
| `app/pipeline_dashboard.py` (new) | Cross-cutting aggregation for the unified dashboard (summary cards, FULL_TIME actionability) |
| `app/pipeline.py` (extended) | `reanalyze_job` now marks a job's current resume variant `STALE` on a materially changed title/description |
| `app/main.py` / `app/templates/dashboard.html` / `app/templates/job_detail.html` (extended) | Unified one-page dashboard: summary cards, pipeline table with JD-coverage/resume/application/user-action columns, job-detail diagnostics panel, new safe API endpoints |
| `app/templates/resume_optimizer_doctor.html` (new) | Doctor report page |
| `app/migrations.py` (extended) | 5 new additive migrations: `jd_analyses`, `jd_requirements`, `resume_variants`, `resume_quality_reports`, `resume_evidence_links` |
| `scripts/resume_optimizer_benchmark.py` (new) | Isolated-temp-DB benchmark, engineering latency only |

See `docs/jd-analysis-model.md`, `docs/resume-evidence-matching.md`,
`docs/resume-quality-diagnostics.md`, `docs/ats-parse-validation.md`, `docs/unified-dashboard.md`
for the deep dives.

## Truthfulness architecture

Every code path that writes resume content flows through the SAME, unmodified
`app.resume.claim_checker.check_resume_claims` that Phase 1 already built. The optimizer widens
*selection and ordering* of verified content -- it never adds a second, looser validation path.
A resume whose claims fail this check is marked `CLAIM_CHECK_FAILED` and is never silently
served as if it were `READY`.

Two concrete decisions this phase's own testing forced:

1. **The resume summary never echoes the raw JD title.** An early version wrote "targeting
   {job_title}" into the summary sentence. A low-fit acceptance test (`Java Backend Engineer`
   JD against a Python-only verified profile) caught that this embeds the literal word "Java"
   into the candidate's resume even though nothing about the candidate's Java experience is
   true. The summary now only ever contains verified skill names.
2. **`TRANSFERABLE` framing is restricted to a curated category subset.** `LANGUAGE`,
   `ARCHITECTURE`, and `SECURITY` are excluded from `TRANSFERABLE_ELIGIBLE_CATEGORIES` --
   claiming "Python transfers to Go" (or "Java") is not a defensible truthful claim the way
   "one REST framework is transferable to another" or "one cloud provider's experience is
   transferable to another" is. A missing language is always `MISSING`, never `TRANSFERABLE`
   (see `docs/resume-evidence-matching.md`).

## No fake universal ATS score

`app/resume_optimizer/quality.py` never emits "98%" or an "ATS score." It emits itemized
`REQUIRED_SKILL_COVERAGE` / `PREFERRED_SKILL_COVERAGE` / `RESPONSIBILITY_ALIGNMENT` /
`DOMAIN_ALIGNMENT` / `TITLE_ALIGNMENT` / `KEYWORD_COVERAGE` / `EXPERIENCE_EVIDENCE_COVERAGE` /
`ATS_PARSEABILITY` counts, plus named `MISSING_REQUIRED_ITEMS`/`UNSUPPORTED_JD_ITEMS` lists. The
one composite number it does compute (`internal_alignment_score`) is always paired with the
literal string "Internal alignment score -- NOT an ATS score, NOT an interview/hire probability"
in every API/UI surface that shows it.

## Idempotency and concurrency

`resume_variants` has two unique indexes: `(job_id, jd_fingerprint, profile_version,
optimizer_version)` (idempotency -- the identical input never produces two rows) and a partial
`(job_id) WHERE current = 1` (the same "one active thing per job" pattern Phase 8's
`application_executions` and Phase 10's `browser_assist_sessions` already established).
`repo.claim_variant()` performs a single atomic INSERT and raises `DuplicateVariantError` on a
unique-constraint violation -- never a read-then-write check. A real race was caught live during
this phase's own concurrent-Postgres validation: `repo.save_jd_analysis()`'s initial
"does this already exist" check was not by itself a sufficient guard against 8 threads calling
`optimize_resume()` for the identical job/JD simultaneously; it now catches the resulting
`UniqueViolation`/`IntegrityError` and re-fetches rather than crashing. See
`tests/test_resume_optimizer_postgres.py::test_concurrent_optimization_same_identity_never_duplicates`.

## Scope decision: background optimization is a lightweight scheduler, not a leased worker fleet

CLAUDE.md section 57 sanctions adding a `RESUME_OPTIMIZATION` worker capability "if cleanly
justified." Given the project's existing distributed-worker machinery (`app/workers/`,
`app/applications/queue.py`) is built for cross-machine fleets claiming leased work items, and
resume generation for one user's job pipeline is comparatively low-volume and idempotent by
construction, this phase instead added `app/resume_optimizer/scheduler.py`: a single-process
asyncio background loop (mirroring `app.applications.background_scheduler`'s exact structure),
gated by `RESUME_OPTIMIZATION_ENABLED` (default `false`). It calls the same `optimize_resume()`
any dashboard action or CLI command calls -- concurrency safety comes from the database unique
indexes above, not from a lease. If this project is later run across multiple machines with a
shared Postgres database, `optimize_resume()`'s own idempotency/concurrency guarantees already
hold; a true leased `RESUME_OPTIMIZATION` worker capability remains a reasonable Phase 15
addition if true multi-machine parallelism for this specific workload is ever needed.

## Regression discipline

Nothing in `app/applications/browser_*.py`, `app/applications/circuit.py`,
`app/workers/circuit.py`, `app/sponsorship/*.py`, or `app/matching/employment_type.py` was
modified. The only touch points into pre-Phase-14 code are additive: one `mark_stale()` call in
`app.pipeline.reanalyze_job` (wrapped in a bare `except Exception: pass` so a resume-optimizer
issue can never block the sponsorship/state pipeline), and new imports/routes in `app/main.py`.
See the final-verification results in this doc's companion report for the full default/Postgres/
browser test counts.
