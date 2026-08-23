# Greenhouse / Lever / Ashby Hardening Pass (2026-08-22, browser-validated 2026-08-23)

Branch: `feat/providers-greenhouse-lever-ashby`. Scope: deep verification and hardening of
these three providers only, without touching shared browser-assist infrastructure (that
layer is provider-agnostic by design -- see `app.applications.browser_capability_matrix`'s
own module docstring -- and was independently re-verified in this same time window on a
sibling worktree covering Workday/SmartRecruiters/Workable; the two efforts coordinated to
avoid overlapping edits).

This is not a rebuild. All three providers already carried `FULL` discovery support and
`LIVE_FORM_VERIFIED` browser-assist evidence from prior phases. The work here was: fetch
each provider's real public API live, diff the actual response schema against what the
normalize layer captures, and fix what was genuinely wrong or missing -- never guessed.

## Real bugs found and fixed (all live-verified against the real public APIs)

1. **Ashby salary was frequently silently wrong.** `compensation.summaryComponents` is a
   list of DIFFERENT component types (Salary/Bonus/EquityPercentage/...) in no guaranteed
   order. The old code read `summaryComponents[0]` unconditionally -- on a real posting
   fetched live (`api.ashbyhq.com/posting-api/job-board/ashby`), index 0 was an
   `EquityPercentage` component with `minValue: null`, so a job with a genuine
   €110K-€185K salary reported `salary_min=None`. `salary_max` was never read at all, and
   `currencyCode`/`interval` (→ `salary_period`) were dropped entirely. Fixed:
   `_extract_salary()` now filters for a component whose `compensationType` genuinely says
   "Salary" and extracts all four fields.
2. **Greenhouse freshness used the wrong timestamp.** `published_at` was set from
   `updated_at`, which changes on every edit (a typo fix would make a months-old posting
   look brand new to the freshness ranking). The real API also exposes `first_published`,
   confirmed live (`boards-api.greenhouse.io/v1/boards/gitlab/jobs`) as the genuine original
   publish date. Now preferred, with `updated_at` as fallback only when a tenant doesn't
   expose `first_published`.
3. **Lever dropped roughly half of a posting's real JD content.** The real API
   (`api.lever.co/v0/postings/leverdemo`) exposes structured `lists` sections (e.g.
   "Qualifications", "Duties" -- exactly the requirement-bearing content JD analysis and
   resume tailoring need) and an `additionalPlain` closing section (EEO/extra-requirements
   text) as fields separate from `descriptionPlain`. Neither was captured. Both are now
   concatenated into `description`.
4. **Lever/Ashby requisition-id identity verification silently never worked.**
   `app.applications.job_identity.extract_requisition_token()`'s only pattern matched
   numeric or `R-`-prefixed ids (Greenhouse/Workday's shape). Both Lever
   (`jobs.lever.co/<site>/<uuid>`) and Ashby (`jobs.ashbyhq.com/<board>/<uuid>`) identify a
   posting by a UUID path segment, confirmed live against both APIs -- so the strongest
   identity signal (`verify_job_identity_full`'s `requisition_id` match, the only signal
   that alone reaches `VERIFIED`) was structurally unavailable for 2 of this project's 3
   FULL-support providers. Added a UUID path-segment pattern, bounded to a standalone
   `/`-delimited segment so it can't fire on an unrelated hex substring.

## Smaller, verified additions

- Greenhouse: uses the API's real `company_name` field when present (previously always
  guessed from the board token, e.g. `"acme-corp"` -> `"Acme Corp"`, even when the real
  company name differs); captures `requisition_id`/`internal_job_id` into
  `provider_metadata`; captures `offices[0].name` as `office`; `employment_type_raw` is a
  best-effort, honestly-labeled scan of the tenant's own freeform `metadata` custom fields
  for one named like an employment-type question (no fixed schema across tenants, so
  `structured_employment_type_supported` deliberately stays `False` -- this is a heuristic,
  not a structural guarantee, and was empty on every field checked against the real GitLab
  board, which uses `metadata` for an unrelated "Quota Coverage Type" field).
- Lever: `salary_period` now derived from the real `salaryRange.interval` field
  (`"per-year-salary"` -> `"year"`, etc).

## What was investigated and deliberately left unchanged

- **Lever multi-location jobs** (`categories.allLocations`): a real posting can list
  several countries. Joining them into a single `location` string was considered and
  rejected -- `app.matching.geography.is_us_location()` does simple substring scanning, so
  a joined string like `"Amsterdam, Netherlands; Arlington, TX"` would hard-fail the
  US-location gate on the `"netherlands"` substring even though the same posting also lists
  a genuine US location. Left as the existing single primary-location string.
- **Ashby's `applyUrl` vs `jobUrl` as canonical `url`**: confirmed live that `applyUrl`
  resolves directly to the `/application` form page. This is deliberate existing design
  (matches `browser_capability_matrix`'s own Ashby note) so apply-first-click isn't needed
  for tenants where the discovery URL already IS the form -- left unchanged.
  `job_identity.py`'s UUID fix (above) already makes the `/application` suffix a
  non-issue for identity verification, since the path segment match is bounded by `/` on
  both sides.
- **Ashby per-job application-form-schema endpoint**: checked live for a public,
  unauthenticated per-job detail endpoint that might expose a structured question schema
  (mirroring what Greenhouse's `?questions=true` provides) -- returned `401 Unauthorized`.
  Confirms the existing design (no dedicated `providers_ashby.py` API-based application
  adapter; ASSIST_ONLY via the generic browser-assist DOM engine) is honestly correct, not
  a gap to fill.
- **Greenhouse application-form field types**: re-fetched a real `?questions=true` payload
  live; every field type present (`input_text`, `input_file`, `textarea`,
  `multi_value_single_select`) is already handled by `providers_greenhouse.py`'s
  `_extract_fields()`. No gap found.
- **CAPTCHA/MFA/login/legal-question detection, SPA stabilization, field discovery/mapping,
  blocker/confirmation detection for the browser-assist path**: these live in
  `app.applications.browser_runtime`/`apply_entry`/`schema` -- provider-agnostic by
  construction, already `LIVE_FORM_VERIFIED` for all three providers with detailed dated
  findings in `app.applications.browser_capability_matrix`. This hardening pass did not
  touch that layer (per-provider discovery/normalize-layer bugs were the actual gap found;
  a sibling effort was independently re-verifying that shared layer in the same window).
  This sandbox also has no working Chromium (binary present, missing system shared
  libraries, no sudo available to install them), so no new live-browser E2E validation was
  possible from here regardless.

## Exact capability matrix

| | **Greenhouse** | **Lever** | **Ashby** |
|---|---|---|---|
| **Discovery** | FULL. Unauthenticated `boards-api.greenhouse.io/v1/boards/{token}/jobs`, single request per tenant, full job list, no pagination. | FULL. Unauthenticated `api.lever.co/v0/postings/{site}`, single request per tenant. | FULL. Unauthenticated `api.ashbyhq.com/posting-api/job-board/{name}`, single request per tenant. |
| **Employment** | No dedicated API field. `employment_type_raw` from a best-effort scan of tenant-defined `metadata` custom fields (heuristic, honestly `structured_employment_type_supported=False`); `classify_employment_type()` falls back to title/description text, `UNKNOWN` on silence -- positive-evidence-only, never guessed to FULL_TIME. | Structural: `categories.commitment` (e.g. "Regular Full Time (Salary)"). `structured_employment_type_supported=True`. | Structural: `employmentType` enum ("FullTime"/"PartTime"/"Contract"/...). `structured_employment_type_supported=True`. |
| **JD** | Full HTML `content` field, stripped to plain text. Single field, always complete for what the tenant provides. | `descriptionPlain` + structured `lists` sections (Qualifications/Duties/etc, real requirement content) + `additionalPlain` closing section -- all three now concatenated (fixed this pass; previously ~half the real content was dropped). | `descriptionPlain` (fallback: stripped `descriptionHtml`). Already complete on the postings checked. |
| **Fill (form field discovery, API-based)** | PARTIAL. `?questions=true` on the same public boards-api returns real structured fields (name/label/type/required/choices), live-verified including a genuine sponsorship question on a real GitLab posting. | UNSUPPORTED via API -- no documented public endpoint exposes a question schema (live-checked, confirmed again this pass). Handled entirely via browser-assist instead. | UNSUPPORTED via API -- no public per-job detail/schema endpoint (confirmed live this pass: `401 Unauthorized`). Handled entirely via browser-assist instead. |
| **Fill (browser-assist, DOM-based)** | LIVE_FORM_VERIFIED. Provider-agnostic engine reaches 23-24 real fields on a live GitLab posting. | LIVE_FORM_VERIFIED. 22 real fields on Lever's own public demo posting. | LIVE_FORM_VERIFIED. 27-28 real fields on Ashby's own public careers board. |
| **Upload** | `input_file` fields (resume/cover letter) supported by both the API-based adapter and browser-assist; live-verified resume upload field present. | Not applicable via API (no schema); browser-assist resume upload live-verified. | Not applicable via API; browser-assist resume upload live-verified. |
| **Blockers** | CAPTCHA live-verified on a real posting (GitLab), correctly detected and paused, never bypassed. Login-handoff verified on local fixture only (no live posting required login). | CAPTCHA live-verified on the real Lever demo posting (hCaptcha), correctly detected and paused. | CAPTCHA live-verified on a real posting, correctly detected and paused. |
| **Confirmation** | Success-page/confirmation-id detection verified on local sandbox fixture; never exercised against a real submission (no real application was ever submitted). | Verified on local sandbox fixture only. | Verified on local sandbox fixture only. |
| **Final submission capability** | **ASSIST_ONLY.** No auto-submit path exists or is enabled for any real posting. | **ASSIST_ONLY.** Same. | **ASSIST_ONLY** (via generic adapter -- no dedicated API adapter exists, browser-assist never clicks final submit for any real provider by structural/doctor-enforced design). |
| **Evidence** | `app.providers.capabilities` (discovery), `app.applications.capability_matrix` (form/assist), `app.applications.browser_capability_matrix` (browser), plus this pass's live curl checks against the real public API (see bugs 2 above). | Same three sources, plus this pass's live checks (bugs 1, 3, 4 above). | Same three sources, plus this pass's live checks (bug 1, and the `401` per-job-endpoint check above). |
| **Known limitation** | `employment_type_raw` from `metadata` is heuristic and tenant-dependent (empty on tenants that don't name a matching custom field, e.g. GitLab's real board). Multi-page/multi-step behavior only verified on a local fixture, not a real multi-page Greenhouse posting. | No API-based form discovery exists or ever will without a documented public schema endpoint (never reverse-engineered). Multi-step behavior genuinely unknown -- the live posting checked was single-page. | No API-based form discovery exists (confirmed `401` on the only plausible endpoint). Multi-step behavior genuinely unknown -- the live posting checked was single-page. `organizationName` can be `null` on some boards, in which case `company` falls back to a title-cased board-name guess (never fabricated as something more specific). |

## 2026-08-23: real-Chromium live-validation closure

Real Chromium (system-installed via `playwright install-deps chromium`, no `/tmp`
LD_LIBRARY_PATH workaround) was verified launchable and used to run:

- The full `@pytest.mark.browser` suite (deterministic local HTML fixtures,
  `tests/browser_fixtures.py`): **57/57 passed**, 0 skipped, 0 errors -- previously
  environment-blocked (missing `libnspr4.so` etc, no sudo), now genuinely executed.
- A new bounded, read-only, no-submit live-validation script,
  `scripts/gh_lever_ashby_live_validation.py`, following the exact safety model
  `scripts/phase12_live_validation.py`/`phase13_live_validation.py` already established:
  at most one real posting per provider (the same already-vetted public postings those
  scripts use -- GitLab's public Greenhouse board, Lever's own demo account, Ashby's own
  careers board), at most one safe `NAVIGATION_SAFE` apply-entry hop, never fills a
  candidate field, never uploads a resume, never clicks final submit, never submits.

**Per-provider live result** (today's specific postings; a job board's "first" posting
can differ day to day):

| | Greenhouse (GitLab, req `8503792002`) | Lever (demo, `33538a2f-...`) | Ashby (own board, `7458d4e9-...`) |
|---|---|---|---|
| Candidate-facing URL correctness | Correct -- `job.url` from discovery (`job-boards.greenhouse.io/gitlab/jobs/8503792002`) opened directly to the real application page. | Correct -- `job.url` opened to the real posting; one safe apply-entry hop landed on `.../apply` with the real embedded form. | Correct -- `job.url` (`applyUrl`) opened directly to the `/application` form page (no hop needed, matching the existing documented finding that this is posting-dependent). |
| SPA/page loading | `content_ready` in 47ms. | `dom_stable` in 818ms (genuinely dynamic). | `content_ready` in ~1s. |
| Field detection | 23 real fields (name/email/phone/LinkedIn/employment-agreement question/accessibility question/...). | 22 real fields (name/email/phone/current company/LinkedIn/GitHub/pronouns/...). | 27 real fields (name/email/EMEA-timezone question, 3 free-response questions, self-ID demographics/...). |
| Resume-upload detection | Detected (`input_file` "Attach" control). | Detected ("ATTACH RESUME/CV"). | Detected ("Resume"). |
| Job identity (`verify_job_identity_full`, using the SAME schema.org JSON-LD extraction `browser_runtime` itself uses before a resume upload/final submit, not a raw `<title>` tag) | **VERIFIED** via matching requisition token (no JSON-LD on this posting; URL-path numeric id `8503792002` matched on both sides). | **VERIFIED** via matching requisition token -- **the first live confirmation that this pass's UUID-path-token fix (`job_identity.py`) actually works on a real Lever page**; before that fix this would have been `UNVERIFIABLE`. | **VERIFIED**, and independently corroborated four ways: JSON-LD `title`/`company`/`identifier` all matched AND the UUID path token matched -- also the first live confirmation of the UUID fix working on a real Ashby page. |
| Blocker detection -- CAPTCHA (production DOM-element check, `app.applications.canary`) | Not present on today's specific posting/session (`canary_captcha_detected=False`; form and final-submit control both still found and correctly never clicked). | **Present** -- correctly detected before any field-filling; canary's own safety gate (never follow an apply-entry hop past a detected CAPTCHA) correctly stopped further navigation. | **Present** -- same correct detection and stop. |
| Blocker detection -- login/MFA | Not present. | Not present. | Not present. |
| Confirmation detection | Not exercised (never submitted, per instructions) -- final-submit control detected and left unclicked. | Not exercised -- canary halted at the CAPTCHA gate before reaching the form (correct). | Not exercised -- canary halted at the CAPTCHA gate before reaching the form (correct); final-submit control was independently detected (and left unclicked) by the separate field-discovery pass. |

**Honest discrepancy worth recording, not silently reconciled**: `app.applications.
browser_capability_matrix`'s existing Greenhouse row (dated 2026-08-22, owned by the
sibling worktree covering the shared browser-assist layer) states the real GitLab posting
"genuinely presented a CAPTCHA widget." Today's DOM-based canary check found no CAPTCHA on
today's specific GitLab requisition (`8503792002`, "Account Executive - Italy"). Since
GitLab's public board returns 200+ jobs and which one lands as "the first result" can
differ run to run, the most likely explanation is CAPTCHA presence is posting/session-
dependent on this tenant, not a fixed per-provider property -- consistent with how this
project already treats Workday's own CAPTCHA/login variability (`VARIABLE`, never
cherry-picked). Not resolved here since `browser_capability_matrix.py` is the sibling
session's file; flagged to them directly and recorded here for the record. No code
changed as a result -- this is a live-environment observation, not a bug.

**No production bug was found by this live run.** One methodology issue surfaced and was
fixed IN THE VALIDATION SCRIPT ITSELF, not in application code: the script's first draft
compared a real page's raw `<title>` tag against the stored job title for the identity
check, which produced a false `title` mismatch (e.g. `"Job Application for Account
Executive - Italy at GitLab"` vs `"Account Executive - Italy"`) purely from site-chrome
wrapper text -- `app.applications.job_identity.verify_job_identity_full()` correctly
treats ANY strong-signal mismatch as `MISMATCH` even when `requisition_id` also matches
(by design, per its own docstring), so a sloppy "observed title" source alone was enough
to produce a false `MISMATCH` verdict. The script was corrected to call
`browser_runtime._extract_observed_job_meta()` -- the actual schema.org JSON-LD extraction
production code uses -- instead, after which all three providers correctly verified.
This is a real illustration of why the production code intentionally never uses a raw
`<title>` tag for identity comparison; it does not indicate a defect in
`verify_job_identity_full()` or `job_identity.py` itself.

Capability truths were re-generated after this run (`python scripts/
generate_provider_matrix.py`) and are unchanged: Greenhouse stays `PARTIAL`/`ASSIST_ONLY`,
Lever/Ashby stay generic `ASSIST_ONLY`, all three stay `LIVE_FORM_VERIFIED` for browser
evidence, and `auto-submit=False` for every real provider (`mock_ats` remains the only
exception). No provider was promoted to `AUTO_SUBMIT_SUPPORTED` -- a detected submit
button was never treated as evidence of a legitimate, permitted submission path.

## Tests

- `tests/test_providers.py`, `tests/test_ashby_provider.py`: 19 new deterministic unit
  tests covering every fix above, plus schema-drift resilience (missing `lists`/
  `additionalPlain`/`compensation`/`first_published` fields must degrade gracefully, never
  crash normalization -- each already-isolated by the existing per-item/per-tenant
  try/except).
- `tests/test_job_identity.py`: 6 new tests for the UUID requisition-token pattern
  (Lever-shaped, Ashby-shaped, apply-suffix variants, a mismatch case, and a negative case
  confirming it never partial-matches an unrelated short hex fragment).
- Full non-browser suite: **1223 passed**, 57 deselected.
- Full browser-marker suite (real Chromium, 2026-08-23): **57 passed**, 1223 deselected.
- Combined: **1280/1280 tests passing**, 0 skipped, 0 errors.
- `python scripts/generate_provider_matrix.py` re-run after all changes (both the
  discovery-layer fixes and the live-validation pass): capability truths (support levels,
  `auto-submit=False` for every real provider) are unchanged -- this pass improved data
  quality, identity verification, and closed out real-browser validation, not automation
  scope.
