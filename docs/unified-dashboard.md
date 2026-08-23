# Unified One-Page Dashboard

`GET /` (`app/main.py::dashboard`, `app/templates/dashboard.html`)

## What one page shows

- **Agent Status hero** (see `docs/one-click-agent.md`): the current orchestrator state
  (`STOPPED`/`STARTING`/`RUNNING`/`PAUSED`/`STOPPING`/`ERROR`), START AGENT / STOP AGENT / START
  AGENT (TEST MODE) buttons, last/next cycle, and per-cycle counters. The older Phase 2
  discovery-only Turn ON/OFF toggle still exists, collapsed under a `<details>` disclosure, for
  backward compatibility.
- **Summary cards** (superseding the older Phase 14 wording): Jobs found, Full-time eligible,
  Confirmed sponsor, Strong matches, One-page resumes ready, Applications prepared, Applying,
  Needs your action, Applied today, Skipped -- computed by
  `app/pipeline_dashboard.py::compute_pipeline_summary()`, a handful of small grouped `COUNT(*)`
  queries (never a per-job loop, never a full-table scan beyond what the pipeline table itself
  already fetches).
- **Needs Your Action queue** (`build_needs_action_queue`): centralizes every blocker across
  applications, browser-assist sessions, `LIKELY_SPONSOR` reviews, and resume one-page overflow
  into one list -- company, role, reason, current stage, what the agent already completed, the
  exact action required, and a Continue link to the job detail page.
- **Live Activity feed** (`build_recent_activity`): a rolling feed of state transitions and
  application events, company/title only -- never JD text, resume content, or profile fields.
- **Pipeline table**: Company, Role, Age (freshness), Arrangement, Employment, Sponsorship, JD
  coverage, Resume (state + a `1 PAGE ✓`/`REVIEW_REQUIRED` badge), ATS/provider, Application
  status, User action, Priority, Action -- one row per job, sourced from already-persisted/cached
  tables (`resume_quality_reports`, `resume_variants`, `application_executions`), never a live
  JD/resume recomputation on page load (section 55).
- **Filters**: work arrangement, sponsorship status, freshness, high priority, resume state,
  needs-user-action-only, and a "Include non-full-time (audit)" toggle.

## Job-detail panel (section 51, extends the existing per-job page rather than adding a modal)

`GET /jobs/{job_id}` now includes a "JD coverage & resume diagnostics" card: Analyze JD /
Generate-Regenerate Resume / Force Regenerate actions, the current variant's status, the full
itemized quality report (required/preferred coverage, responsibility/domain/title alignment,
keyword coverage, ATS parseability, missing/unsupported items, claim-check result), a
"Claim provenance" `<details>` panel (per-requirement evidence, section 60), and a "Match
priority components" `<details>` panel (section 41's documented-weight ranking, section 60).
Existing sponsorship-explanation, application-execution, and browser-assist cards are unchanged.

## FULL_TIME default actionability (sections 53-54, 84)

`pipeline_dashboard.is_actionable(job)` hides a job from the default pipeline view only when
`app.matching.employment_type.classify_employment_type()` POSITIVELY returns one of
`CONTRACT`/`C2C`/`PART_TIME`/`INTERNSHIP`/`TEMPORARY`/`SEASONAL`/`FREELANCE` -- matching the
application executor's own hard gate (CLAUDE.md Phase 8 section 1). `UNKNOWN` (the common case
for manually-ingested or legacy jobs with no explicit employment-type signal) stays visible by
default, the same "UNKNOWN is not itself a hard-skip" pattern this project already uses for
sponsorship. This was a deliberate correction during this phase's own test-writing: an earlier
version required a POSITIVE `FULL_TIME` classification to show a job at all, which silently hid
almost every pre-Phase-14 test fixture (none of which set an explicit `employment_type`) from
the dashboard -- a real regression caught by `tests/test_resume_optimizer_dashboard.py` before
it could ship. Include-non-full-time toggle (`full_time_only=false`) shows everything, including
confirmed CONTRACT/etc jobs, for audit/discovery-metrics visibility (section 54) -- never as an
"actionable" implication.

## API endpoints (section 64)

| Endpoint | Purpose |
|---|---|
| `GET /api/jobs/{job_id}/jd-analysis` | Cached JD requirements model |
| `GET /api/jobs/{job_id}/resume-quality` | Current variant's full quality report |
| `GET /api/jobs/{job_id}/resume-evidence` | Per-requirement evidence links for the current variant |
| `GET /api/pipeline/summary` | The same summary-card counts the dashboard renders |
| `GET /api/resume-optimizer/metrics` | `resume_optimizations_total`/`_failed`, `resume_variants_ready`/`_stale`, `resume_claim_failures`, `resume_parse_failures`, `jobs_low_alignment`, coverage distribution |
| `POST /jobs/{job_id}/resume/analyze` | Analyze JD only (no resume generated) |
| `POST /jobs/{job_id}/resume/optimize` | Generate/regenerate (`force=true` to bypass the cached-READY shortcut) |
| `GET /jobs/{job_id}/resume/download/{docx\|pdf\|txt}` | Current optimized-variant artifact download |
| `GET /resume-optimizer/doctor` | Doctor report page |
| `GET /agent/status` | Legacy discovery-scheduler status plus the full one-click-agent orchestrator state |
| `POST /agent/start` | Starts the one-click agent (`test_mode: bool = false`) -- see `docs/one-click-agent.md` |
| `POST /agent/stop` | Stops it cleanly |

None of these expose raw candidate PII beyond what the existing job-detail page already shows
(name/contact fields the candidate themselves entered); evidence links reference verified
bullets already visible on the generated resume, not private candidate_data internals.

## Specialist pages remain (section 79)

`/applications`, `/fleet`, `/registry`, `/companies`, `/sponsorship/*`, `/applications/*`
(browser sessions, capability matrices, provider health) are all unchanged and still linked from
the unified dashboard's nav bar, plus the new `/resume-optimizer/doctor` link -- they remain the
admin/debugging depth views; the unified dashboard is the one page a user actually works from
day to day.

## Safe defaults unchanged (section 82)

`AUTO_SUBMIT_ENABLED` stays `false` by default. `RESUME_OPTIMIZATION_ENABLED` (background
optimization scheduling) also defaults `false` -- the dashboard's "Generate/Regenerate Resume"
button and the CLI always work regardless of this flag; it only controls whether jobs get
optimized automatically in the background.
