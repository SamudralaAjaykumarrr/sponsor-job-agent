# UI button matrix

CLAUDE.md production-v2 section 91. Scope: the primary one-click dashboard
(`/`) and the job detail page (`/jobs/{id}`) -- the two pages the "OPEN
WEBSITE -> START AGENT -> LEAVE IT RUNNING" flow actually depends on. This is
**not** a full inventory of every specialist/diagnostic page in the app
(`/applications`, `/fleet`, `/registry`, `/companies`, the various `*doctor`
and `*review-queue` pages) -- those existed before this build and are covered
by their own dashboards' own tests (see `tests/test_applications_dashboard.py`,
`tests/test_browser_sessions_dashboard.py`, `tests/test_resume_optimizer_dashboard.py`,
`tests/test_registry_dashboard_phase4.py`, `tests/test_fleet_dashboard.py`,
`tests/test_sponsorship_dashboard.py`). Extend this file if a future change
also needs to guarantee those buttons are non-dead.

| Page | Button/control | Endpoint | Method | Precondition | Expected state change | Idempotent? | Test |
|---|---|---|---|---|---|---|---|
| `/` | START AGENT | `/agent/start` | POST | none (safe no-op if already running) | `desired_state=RUNNING`; first cycle begins within seconds | Yes -- repeated clicks return "already running", never a second orchestrator | `test_agent_start_stop_routes.py`, `test_agent_orchestrator.py` |
| `/` | START AGENT (TEST MODE) | `/agent/start` (`test_mode=true`) | POST | none | Same as above + seeds the idempotent `mock_ats` fixture job | Yes | `test_agent_orchestrator.py`, live Playwright smoke (this session) |
| `/` | STOP AGENT | `/agent/stop` | POST | none (safe no-op if already stopped) | `desired_state=STOPPED`; loop drains and stops | Yes | `test_agent_start_stop_routes.py`, live Playwright smoke |
| `/` | Legacy scheduler Turn ON/OFF | `/agent/toggle` | POST | none | `agent_state.enabled` flips | Yes | `test_agent_scheduler.py` |
| `/` | Ingest & Process | `/jobs/ingest` | POST | title/company/description required | New `jobs` row, analyzed synchronously | No (each submit creates a new job by design -- not meant to be a repeat-safe upsert) | `test_dashboard_phase3.py` and others exercising `ingest_and_process` |
| `/` | filter links (Remote/Hybrid/.../Needs action) | `/` with query params | GET | none | none (read-only) | Yes (GET) | `test_dashboard_row_cap.py`, `test_dashboard_batched_execution_lookup.py` |
| `/` | "view test job" link | `/?include_test_data=true` | GET | none | none (read-only) | Yes | live Playwright smoke (this session) |
| `/jobs/{id}` | Analyze JD | `/jobs/{id}/resume/analyze` | POST | job exists | writes `jd_analyses` row | Yes -- re-running with an unchanged JD fingerprint is a no-op read | covered by `test_resume_optimizer_dashboard.py` |
| `/jobs/{id}` | Generate/Regenerate Resume | `/jobs/{id}/resume/optimize` | POST | sponsorship-eligible job | new/updated `resume_variants` row | Yes -- `(job_id, jd_fingerprint, profile_version, optimizer_version)` unique index (CLAUDE.md Phase 14 section 46) | `test_resume_optimizer_dashboard.py` |
| `/jobs/{id}` | Regenerate Resume (legacy assist path) | `/jobs/{id}/regenerate` | POST | `sponsorship_status in (CONFIRMED, LIKELY)` | regenerates docx/pdf/txt via `generate_assist_outputs` | Yes | pre-existing dashboard tests |
| `/jobs/{id}` | Prepare Application | `/jobs/{id}/applications/prepare` | POST | executor enabled, job eligible | new `application_executions` row (`PREPARING`) | Guarded by `application_executions(job_id) WHERE active=1` partial unique index -- a second click while one is active is rejected, never a duplicate | `test_applications_dashboard.py` |
| `/jobs/{id}` | Queue Application | `/jobs/{id}/applications/queue` | POST | executor enabled | queues for the application worker fleet | Same partial-unique-index guard | `test_applications_dashboard.py` |
| `/jobs/{id}` | Retry | `/jobs/{id}/applications/retry` | POST | a non-active/failed execution exists | new execution attempt | Same guard | `test_applications_dashboard.py` |
| `/jobs/{id}` | Continue (reconcile) | `/executions/{execution_id}/reconcile` | POST | execution exists | resolves `SUBMISSION_STATUS_UNKNOWN`/`NEEDS_USER_ACTION` via real evidence only | Yes -- re-running with no new evidence is a no-op | `test_applications_dashboard.py` |
| `/jobs/{id}` | Start Browser Assist | `/jobs/{id}/browser-assist/start` | POST | `BROWSER_ASSIST_ENABLED=true` | new `browser_assist_sessions` row | Same `(job_id) WHERE active=1` partial-unique-index pattern | `test_browser_sessions_dashboard.py` |
| `/jobs/{id}` | State update (manual) | `/jobs/{id}/state` | POST | valid `can_transition()` target | `jobs.application_state` changes, `application_state_history` row appended | Not idempotent by design (recorded as a distinct transition each time) | pre-existing dashboard tests |
| `/jobs/{id}` | Download DOCX/PDF/TXT/analysis/answers/cover letter | `/jobs/{id}/download/{file_key}` | GET | file generated | none (read-only file response) | Yes (GET) | existing dashboard tests; not re-verified live this session |
| `/jobs/{id}` | Open Application | external `job.url` | GET (external) | `job.url` set | none (leaves the app) | N/A | n/a |

## This session's additions

No new dashboard buttons were added in this build -- the existing route set
above already covered every stage of the pipeline. What changed:

- The **primary control is now unambiguous**: `/agent/start` / `/agent/stop`
  are the only buttons in the un-collapsed hero section; the legacy
  `/agent/toggle` scheduler moved behind a `<details>` "Advanced / legacy
  diagnostics" disclosure and now shows an explicit warning if left ON at the
  same time as the one-click agent.
- `/` gained one new GET-only link, `?include_test_data=true` ("view test
  job"), for the TEST MODE audit view (CLAUDE.md dashboard defect 6).
- No button became a no-op and no button was removed.
