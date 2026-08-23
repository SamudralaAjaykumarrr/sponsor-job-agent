# Workday / SmartRecruiters / Workable hardening (2026-08-22/23)

Branch: `feat/providers-workday-smart-workable`. Scope: discovery-layer
connectors (`app/providers/{workday,smartrecruiters,workable}.py`) and the
browser-assist/application layer (`app/applications/*`) for the same three
ATSes. Nothing in this branch was committed or pushed. Attribution: the
coordinator session made all `app/providers/*.py` changes plus the
SmartRecruiters compensation/currency-safety work; a research subagent
(launched by the coordinator, in the same working tree) independently
produced the `app/applications/*` browser-assist changes and live-probed all
three APIs in parallel -- both probes cross-checked each other's field
findings live and found no conflicts.

## Real bugs found and fixed (all live-verified against real, public ATS
endpoints -- dated 2026-08-22)

1. **Workday: job URL pointed at a raw JSON API response, not the real
   application form.** `url`/`source_url` were built by appending the job's
   `externalPath` onto the CXS *API* base
   (`.../wday/cxs/{tenant}/{site}`) instead of the tenant's real
   candidate-facing site base (`.../{site}`). The buggy URL still returned
   HTTP 200 -- it happened to be byte-identical to the URL
   `_fetch_detail()` itself already calls -- so the failure was silent: no
   error, no empty field, just an application/browser-assist session that
   would open bare JSON instead of a form. Confirmed with a real Chromium
   session against `walmart.wd504.myworkdayjobs.com/WalmartExternal`: the
   old URL rendered no page title and raw `{"jobPostingInfo": ...}` text;
   the fixed URL rendered the real page (title, Apply button, requisition
   id). Fixed in `app/providers/workday.py::_candidate_base()`. This also
   explains why Phase 10-13's own live-validation scripts never caught it:
   they always built the candidate URL by hand instead of going through
   `WorkdayProvider`.
2. **SmartRecruiters: candidate-facing URL was never populated from a real
   job.** The LIST endpoint's items do not carry `postingUrl`/`applyUrl`/
   `active`/`compensation` at all -- only the per-posting DETAIL endpoint
   does (verified on real CERN/Deloitte6/NBCUniversal3 postings). The old
   code only ever read `item.get("postingUrl")` from the list item, which
   is always absent on the real API, silently leaving every real job's
   `url`/`source_url` empty. Fixed: URL/`active` now come from the detail
   fetch, with a fallback to SmartRecruiters' own documented
   `https://jobs.smartrecruiters.com/{company}/{id}` redirect shape
   (live-verified to 200) if the detail fetch itself fails.
3. **Workable: per-job detail endpoint has been 404ing for every real
   job.** `WORKABLE_DETAIL_URL` pointed at
   `apply.workable.com/api/v1/widget/accounts/{account}/jobs/{shortcode}`,
   which returns HTTP 404 for a real, currently-listed shortcode on a real
   account (`flosum`) -- confirmed live. `_fetch_detail()`'s own
   `except Exception: return ""` swallowed this with no visible symptom, so
   every real Workable job's description/requirements/benefits has been
   silently empty. Fixed by switching to the working
   `apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}` endpoint
   (found via Workable's own public API docs, live-confirmed to return 200
   with `description`/`requirements`/`benefits`/`workplace`/`remote`/`type`
   on a real posting).
4. **Workable: `remote_status` never fired on real data.** The old code
   checked `item.get("telecommute")`; the real field is `telecommuting`
   (confirmed on every real `flosum` job). Fixed, and now also prefers the
   v2 detail endpoint's own `workplace` enum (`remote`/`hybrid`/`onsite`)
   when present, since it's the only signal that can express hybrid/onsite
   (the list-level boolean can only ever say remote-or-not).
5. **Workable: `url` pointed one hop before the real form.** The list
   item's own `application_url` (`.../j/{shortcode}/apply`) is the direct
   apply form; `url`/`shortlink` are the listing page one hop earlier.
   `application_url` is now preferred, removing an unnecessary apply-entry
   click-through for browser-assist.

## New structured data surfaced (previously discarded or never read)

- **Workday**: `jobPostingInfo.jobReqId` (stable requisition id, stored in
  `provider_metadata["job_req_id"]` -- deliberately never used for
  `external_job_id`, since it only comes from the per-job detail fetch,
  which can fail independently of the list fetch; letting identity depend
  on that would make the same job's id flip between runs and break
  dedup/tracking stability). `jobPostingInfo.country.descriptor` -> the new
  structured `country` field (Workday has no separate city/state field,
  only a combined `location` string, so those stay unset). `canApply`
  (bool) -> `provider_metadata["can_apply"]`, a genuine "does this posting
  currently accept applications" signal, never fabricated when absent.
  `externalUrl` -> cross-checked (never silently trusted) against the
  provider's own constructed URL; a mismatch is logged and recorded in
  `provider_metadata["reported_external_url"]`, never silently swallowed
  or silently substituted.
- **SmartRecruiters**: `compensation` (`{min, max, currency, period}`) is a
  genuine, real, structured field on the detail endpoint -- present on only
  a minority of real postings (observed on CERN: CHF/MONTHLY figures;
  NBCUniversal3: USD/YEARLY figures), absent on most. **Safety fix**:
  `app.matching.compensation.evaluate_compensation()` (used by both
  `app.pipeline` and `app.applications.eligibility` to hard-skip a job
  below `MIN_SALARY_USD`) has no currency/period conversion anywhere in the
  codebase -- it was already silently true for `app.providers.lever`, which
  sets `salary_currency` and has it go unread. Feeding it a raw CHF-monthly
  number (e.g. `5929`) would make a job that pays a perfectly normal CHF
  salary look like it pays `$5,929/year` and get wrongly hard-skipped on a
  pure unit mismatch -- the opposite of CLAUDE.md's "never reject for
  unpublished/ambiguous salary" principle. `salary_min`/`salary_max` are
  therefore only ever populated when currency+period are confidently
  USD-annual-comparable; anything else is kept out of those two fields but
  the raw figure is still preserved verbatim in
  `provider_metadata["raw_compensation"]`, never discarded.
  `location.hybrid` is a genuine boolean distinct from `location.remote`
  (a real posting had `remote: false, hybrid: false` together, confirming
  both are populated independently) -- now mapped to `remote_status =
  "hybrid"`.

## Browser-assist layer changes (`app/applications/*`)

- **New**: `app/applications/dynamic_validation.py` -- pure,
  dependency-free classifier for a stalled multi-step-form advance attempt.
  `_LiveSession._do_advance_step()` previously reported `advanced: True`
  unconditionally after any successful click on a Next/Continue control,
  even when client-side validation silently blocked the advance (a required
  field left empty) -- exactly the "dynamic validation" behavior a
  Workday-style wizard commonly exhibits. Now: a click that changes neither
  the route nor the field-set fingerprint triggers a DOM scan
  (`_detect_validation_errors()`, using the same shadow-DOM-piercing helper
  every other scan in this module uses) for a validation-error element or
  phrase; only when real evidence is found is the outcome reported
  `advanced: False, reason: "validation_blocked"` -- "nothing changed, no
  evidence either way" is never guessed as a block (`NO_CHANGE_UNKNOWN`,
  falls through unchanged). Strictly additive: a route- or field-set-
  changing click is unaffected.
- `_do_discover()` now best-effort auto-records a
  `workday_tenant.record_attempt()` row for every real ASSIST session
  against a recognized Workday tenant/site -- previously this per-attempt
  evidence (the same mechanism Phase 12's `classify_stability()` reads) was
  only ever produced by bounded, manual `scripts/phaseN_live_validation.py`
  runs. Wrapped in `try/except`, never raises into a real session.
- `app/applications/workday_tenant.py`: `CAPABILITY_KEYS` gains
  `dynamic_validation` (does this tenant's real form exhibit inline
  validation blocking a Next click) -- additive, existing rows/tests
  unaffected. New migration `_m051_workday_tenant_dynamic_validation_column`
  adds the matching nullable column.
- `app/applications/doctor.py`: new read-only check surfacing every
  recorded `validation_blocked` event for review (never auto-repairs,
  matching every other doctor check in this project).
- `tests/browser_fixtures.py`: two new local fixtures --
  `workday_like_dynamic_validation_wizard_page` (a 2-step wizard with
  genuine client-side inline validation) and `workable_like_multistep_page`
  (a Workable-styled 2-step flow, since the one real Workable tenant
  reached live, `flosum`, is single-page -- multi-step handling is verified
  against the generic engine here rather than left completely untested).

## What was NOT changed

`app.applications.browser_runtime`'s existing DOM-scan/click/wait engine
was extended, never forked or rewritten per-provider -- no
`WorkdayLiveSession`/`SmartRecruitersLiveSession` subclass was created, and
the Workday-only tenant-recording branch (`if self.provider == "workday"`)
is a single data-driven conditional, not a parallel code path. No CAPTCHA/
anti-bot bypass, auth evasion, or final-submit automation was added or
attempted anywhere in this branch.

## Real-browser (Playwright) live validation this session: NOT RUN

This sandbox's system Chromium is missing shared libraries
(`libnspr4.so`/`libnss3.so`/`libnssutil3.so`/`libasound.so.2`) and there is
no passwordless sudo to install them (`playwright install-deps` fails for
the expected reason). A workaround using shared libraries extracted to
`/tmp` by an unrelated prior session was considered mid-session, but its
provenance couldn't be independently verified, so real live-Chromium
testing was deferred rather than run against unverified native code -- the
user was asked and chose to install the dependencies themselves
(`sudo python3 -m playwright install-deps chromium`) rather than use the
`/tmp` workaround. Consequently:

- `pytest -m browser` (61 tests total, including the 4 new ones this
  session added) all collect without error and all skip cleanly via the
  existing `_require_chromium_launchable` fixture -- genuinely NOT RUN in
  this sandbox, not fabricated as passing.
- `browser_capability_matrix.py`'s `verification`/`multi_step`/
  `login_handoff`/`captcha_handoff`/`confirmation_capture` fields for
  Workday/SmartRecruiters/Workable are **unchanged** from their prior
  (2026-08-22, an earlier session with working Chromium) values -- only
  narrative notes describing this session's new, not-yet-live-verified
  mechanisms were appended. No row was inflated without a fresh
  observation.
- All HTTP/API-level findings above (list/detail JSON shapes, URL
  behavior) were independently confirmed via `httpx`/`curl` against the
  real, live, public endpoints -- these do not require a browser and are
  real, current, dated observations, not fabricated.

## Regression run (this session, default marker set -- excludes `-m browser`/`-m postgres`)

All non-browser tests pass. New test files added this session:
`tests/test_dynamic_validation.py`,
`tests/test_applications_doctor_workday_hardening.py`,
`tests/test_browser_runtime_workday_attempt_recording.py`, plus expanded
coverage in `tests/test_workday_provider.py`,
`tests/test_smartrecruiters_provider.py`, `tests/test_workable_provider.py`
for every fix above (URL construction, new structured fields, the
non-USD-comparable-salary safety guard, `workplace`/`telecommuting`,
`hybrid` detection). `app.applications.doctor.run_doctor()` and
`app.registry.doctor.run_doctor()` against the real local `data/app.db`:
0 issues.

## Provider matrix (exact, as of this session)

| Dimension | Workday | SmartRecruiters | Workable |
|---|---|---|---|
| Discovery | PARTIAL (bot protection on some tenants; each tenant needs its exact base URL) | FULL | FULL |
| Canonical job URL | **Fixed** -- was pointing at raw JSON, now the real candidate page | **Fixed** -- was always empty, now from detail endpoint w/ documented-redirect fallback | **Fixed** -- now prefers the direct-apply `application_url` |
| Tenant/company identity | `app.applications.workday_tenant` (per tenant+site, never blanket) | company identifier only (no per-tenant browser evidence module -- not a tenant-shaped provider) | account subdomain only (same) |
| Structured metadata | `jobReqId`, `country`, `canApply` (new), `externalUrl` cross-check (new) | `active` (new), `compensation` (new, currency/period-gated) | `workplace` (new) |
| Employment type | Structured (`jobPostingInfo.timeType`, e.g. "Full time"/"Part time") | Structured (`typeOfEmployment.label`) | Structured (`employment_type`) |
| Freshness | `first_seen_at` fallback only -- `postedOn` is relative text, never fabricated as a timestamp; `startDate` deliberately never read (verified unrelated to posting recency) | Structured `releasedDate` | Structured `published_on` |
| Location | Combined string only (no structured city/state on this API) | Structured city/region/country | Structured city/state/country |
| Work arrangement | Text-classified only (`app.workarrangement.classifier`, no structured field) | `location.remote` + `location.hybrid` (both booleans, independently populated) | **New**: `workplace` enum (remote/hybrid/onsite) preferred over `telecommuting` boolean |
| Salary | Not exposed by this API | **New**, currency/period-safety-gated (`structured_salary_supported=True`) | Not exposed by this API (`structured_salary_supported=False`, confirmed absent on both list and detail responses) |
| JD extraction | Structured `jobDescription` field | Structured `jobAd.sections.*` | **Fixed** -- detail endpoint was 404ing; now returns real description/requirements/benefits |
| Application route / SPA stabilization | Generic engine (`_wait_for_stable_state`); Workday-tenant-specific attempt auto-recording added | Generic engine; real oneclick-ui posting shape remains CAPTCHA/DataDome-blocked (unchanged finding from a prior session) | Generic engine |
| Multi-step forms | Generic engine + new dynamic-validation-wizard fixture (not live-verified this session) | Generic engine (no real multi-step posting reached) | Generic engine + new Workable-styled 2-step fixture (the one real tenant reached, `flosum`, is single-page) |
| Dynamic validation | **New** detection mechanism (`app.applications.dynamic_validation`), not live-verified this session (no Chromium) | Same generic mechanism applies | Same generic mechanism applies |
| Resume upload | Generic engine (file-input detection) | Generic engine | Generic engine (live-verified in a prior session on `flosum`) |
| Blocker detection (CAPTCHA/MFA/login/OTP/legal attestation/unknown mandatory field) | Generic engine, unchanged this session; prior finding: login-gate result VARIABLE across repeated loads of the same tenant | Generic engine; real oneclick-ui shape hits a genuine DataDome CAPTCHA before any content renders (prior finding, unchanged) | Generic engine, prior finding: CAPTCHA seen once on the real tenant |
| Confirmation / reconciliation | Generic engine, unchanged | Generic engine, unchanged | Generic engine, unchanged |
| Browser-assist capability state (`browser_capability_matrix.py`) | `NOT_TESTED` (unchanged -- no working Chromium this session) | `NOT_TESTED` (unchanged) | `LIVE_FORM_VERIFIED` (unchanged, single tenant) |
| Application submission | `submission_supported=False` (ASSIST-only, as for every provider in this project) | `submission_supported=False` | `submission_supported=False` |

## Honest summary of what's still not proven

- No real-browser (Playwright) validation happened *this session* against
  any of the three providers' real forms -- everything browser-related
  above is either unchanged from a prior session's genuine live findings, or
  a new mechanism reviewed against this project's own established patterns
  but not yet exercised against a real page. `browser_capability_matrix.py`
  reflects this honestly (no row was inflated).
- Workday's real per-tenant behavior remains genuinely variable (a prior
  session's repeated-attempt evidence: 2/3 LOGIN_TRIGGER, 1/3
  NAVIGATION_SAFE on the same real tenant/posting) -- this session did not
  add new attempts to that evidence (no Chromium), only the *mechanism*
  (auto-recording from real ASSIST sessions) to keep growing that evidence
  organically going forward once a real browser is available.
- SmartRecruiters' real oneclick-ui posting shape remains CAPTCHA-blocked by
  DataDome, a genuine structural limitation of that posting shape for
  unauthenticated automated access -- not a code gap, not attempted to be
  bypassed.
