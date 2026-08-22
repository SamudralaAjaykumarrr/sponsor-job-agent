# Real ATS Validation (Phase 10)

Bounded, read-only, low-volume: **one real posting opened per provider**, via
`scripts/phase10_live_validation.py`, on 2026-08-22. No submission was ever attempted. Every
provider's section either reports what was genuinely observed, or honestly `NOT RUN` with the
reason — never a fabricated result.

## Method

For each provider: fetch one real posting via its public discovery API (already used,
unauthenticated, by the Phase 3 discovery connectors), open its real candidate-facing
application URL in headless Chromium, and run the exact
`app.applications.browser_runtime._detect_fields()`/`_detect_button()` code the product uses —
never a second, separate probing heuristic.

## Results

| Provider | Company/tenant | Fields detected | Upload | Submit button | Login wall | CAPTCHA | Result |
|---|---|---|---|---|---|---|---|
| Greenhouse | GitLab (`gitlab` board) | 24 | yes | yes (not clicked) | no | **yes, real** | LIVE_FORM_VERIFIED |
| Lever | Lever's own demo (`leverdemo`) | 22 | yes | yes (not clicked) | no | **yes, real** | LIVE_FORM_VERIFIED |
| Ashby | Ashby's own board (`ashby`) | 28 | yes | yes (not clicked) | no | **yes, real** | LIVE_FORM_VERIFIED |
| SmartRecruiters | SmartRecruiters' own board | 1 (share-link widget only) | no | no | no | no | NOT_TESTED (see below) |
| Workday | Workday's own dogfood tenant | — | — | — | — | — | NOT RUN (tenant offline) |
| Workable | (none known) | — | — | — | — | — | NOT RUN (no known real tenant) |

### Greenhouse — full detail

Real posting on GitLab's public Greenhouse board. 24 fields including First/Last Name, Email,
Phone, Country, LinkedIn, two attachment/resume upload controls, a free-response question, and
a genuine `"Will you now or in the future require sponsorship for a visa to remain in your
current location?*"` question — exactly the kind of field this project's field-mapping engine
(`app.applications.mapping`) is built to recognize. A real CAPTCHA widget was present and
correctly detected; a real submit button was located and never clicked. Final URL stayed on
`job-boards.greenhouse.io` (domain allowlist matched).

### Lever — full detail

Real posting on Lever's own public demo account. 22 fields including a resume upload, full
name/email/phone, LinkedIn/GitHub/portfolio/video-link URLs, and EEOC-style gender/race
demographic questions with a "Decline to self-identify" option each. A real CAPTCHA widget was
present and correctly detected. This is the same real form the Phase 8
`providers_lever.py` adapter documented as having *no* structured public API schema — the
browser engine reaches it anyway by reading the rendered DOM directly, which is exactly the gap
browser-assist exists to close for a provider whose network API doesn't expose one.

### Ashby — full detail

Real posting on Ashby's own public careers board (Ashby uses its own product to hire). 28
fields including free-response interview-style questions and demographic self-identification
choices (age range, gender, race/ethnicity options). A real CAPTCHA widget was present and
correctly detected.

### SmartRecruiters — full detail, honestly incomplete

The postings-list API's candidate-facing URL (`jobs.smartrecruiters.com/SmartRecruiters/<id>`,
confirmed live at HTTP 200) resolved to a **job-description landing page**, not the application
form directly — only a "copy link to share (WeChat)" control was detected. SmartRecruiters
apparently gates the real form behind a further "Apply"/"Apply Now" click this phase's bounded,
one-page-per-provider validation policy did not follow. Marked `NOT_TESTED` at the form level
rather than inflated to `LIVE_FORM_VERIFIED` — the landing page itself was safely opened and its
navigation/domain behavior observed, but the actual form was not reached. See "Recommended Phase
11" in `docs/phase10-real-ats-assist.md`.

### Workday — offline this session

The Phase 3 dogfood tenant (`workday.wd5.myworkdayjobs.com/Workday`) now redirects its CXS API
to `community.workday.com/maintenance-page` — genuinely unavailable as of this check, not a code
defect. No substitute tenant was guessed. See `docs/workday-application-assist.md`.

### Workable — no known real tenant

Phase 3's own dogfooding attempt against guessed tenant names never resolved to a real account
(documented in `docs/acceptance_verification.md`). Not fabricated here either.

## Rate/volume safety

Six total network requests plus six total browser page-opens across the entire validation run —
no crawling, no pagination beyond the first page of results, no repeated polling. Every provider
section either ran exactly once or reported `NOT RUN` immediately.

## Updating this file

Re-run `python scripts/phase10_live_validation.py` and update this file (and
`app/applications/browser_capability_matrix.py`'s corresponding row) only from a fresh,
genuinely-observed result — never bump a verification level from memory or assumption.

## Phase 11 update (2026-08-22): apply-first-click validation

`scripts/phase11_live_validation.py` re-ran all six providers and additionally attempted to
follow each real posting's apply-entry control (see `docs/apply-entry-navigation.md`). Full
per-provider findings live in `docs/phase11-ats-flow-hardening.md`; summary:

| Provider | Apply-entry control found | Classification | Followed | Verification (form level) |
|---|---|---|---|---|
| Greenhouse | yes | `NAVIGATION_SAFE` | **yes** | `LIVE_FORM_VERIFIED` (unchanged) |
| Lever | yes | `EXTERNAL_REDIRECT` | no (correct) | `LIVE_FORM_VERIFIED` (unchanged; page was already the form) |
| Ashby | yes | `EXTERNAL_REDIRECT` | no (correct) | `LIVE_FORM_VERIFIED` (unchanged; page was already the form) |
| SmartRecruiters | yes | `EXTERNAL_REDIRECT` | no (correct) | still `NOT_TESTED` -- see `docs/smartrecruiters-application-assist.md` |
| Workday | inconsistent across 2 runs | `NAVIGATION_SAFE` once, `LOGIN_TRIGGER` once | no (neither run) | still `NOT_TESTED` -- see `docs/workday-tenant-validation.md` (new, genuinely live, tenant: Walmart) |
| Workable | yes | `EXTERNAL_REDIRECT` | no (correct; `application_url` was already the form) | upgraded to `LIVE_FORM_VERIFIED` (new, genuinely live tenant: 'flosum') |

A real bug this run caught and fixed: an early, ungated step-progress regex misread an unrelated
on-page date ("7/31") on the real Greenhouse posting as "step 7 of 31" -- see
`app/applications/apply_entry.py::parse_step_progress`'s docstring and
`tests/test_apply_entry.py::test_unrelated_date_like_ratio_never_treated_as_step_progress`.

Genuine, dated evidence from this run is recorded in `capability_evidence_records` (query via
`python -m app.applications.cli capability-evidence`) and, for Workday,
`workday_tenant_observations` (`python -m app.applications.cli workday-tenants`) -- see
`docs/ats-capability-evidence.md`.

## Phase 12 update (2026-08-22): SPA hardening, repeated Workday, trusted redirects

`scripts/phase12_live_validation.py` re-ran the full provider set with the SPA-hardened DOM-
scanning stack (bounded stabilization wait, iframe/shadow-DOM discovery, trusted-redirect-aware
apply-entry classification), added a genuinely NEW SmartRecruiters posting shape, repeated the
Workday check 3 more times, and added a real, non-ATS-domain trusted-redirect check. Full detail
in `docs/phase12-spa-ats-hardening.md` and the per-topic docs it links; summary:

| Provider | Regression result | Notes |
|---|---|---|
| Greenhouse | `LIVE_FORM_VERIFIED` (regression-confirmed) | GitLab's board organically migrated host (`boards.greenhouse.io` -> `job-boards.greenhouse.io`) between phases -- zero code changes needed, domain-allowlist suffix match and apply-first-click both kept working |
| Lever | `LIVE_FORM_VERIFIED` (regression-confirmed) | apply-entry control confirmed `EXTERNAL_REDIRECT` with `redirect_trust=UNTRUSTED` -- the trusted-redirect model correctly did NOT reclassify it |
| Ashby | `LIVE_FORM_VERIFIED` (regression-confirmed) | this run's posting had no apply-entry control at all (API URL already the form) |
| Workable | `LIVE_FORM_VERIFIED` (regression-confirmed) | identical 14-field result |
| SmartRecruiters (classic shape) | `NOT RUN` (honest) | the tried company's public postings API returned zero results this run |
| SmartRecruiters (new `oneclick-ui` SPA shape) | still `NOT_TESTED`, conclusively characterized | a real DataDome bot-detection CAPTCHA blocked all content before it rendered -- see `docs/smartrecruiters-spa-validation.md` |
| Workday | still `NOT_TESTED`, `VARIABLE` stability confirmed | same real Walmart tenant reloaded 3 more times: `LOGIN_TRIGGER, LOGIN_TRIGGER, NAVIGATION_SAFE` -- see `docs/workday-observation-model.md` |
| Trusted redirects | proven live | GitLab's own corporate careers page (`about.gitlab.com`) links to 10 real `job-boards.greenhouse.io` postings; all 10 classify `TRUSTED_ATS_REDIRECT` -- see `docs/trusted-ats-redirects.md` |

Three real bugs this run's own live/E2E testing caught and fixed (a `file://` scheme
misclassification, and iframe-sourced fields being discovered but not fillable) -- see
`docs/phase12-spa-ats-hardening.md`'s bug list.

Capability evidence this run was recorded with the new `REAL_BROWSER`/`REAL_BROWSER_REPEATED`
verification types (CLAUDE.md Phase 12 section 41) rather than `LIVE_PUBLIC` -- several providers'
`field_discovery`/`captcha_handoff`/`resume_upload` capabilities were genuinely re-observed across
Phase 11 and Phase 12 and are now `REAL_BROWSER_REPEATED` with `repeat_count=2`.

## Phase 13: bounded canary re-validation (`scripts/phase13_live_validation.py`)

| Provider | Posting | Result |
|---|---|---|
| Greenhouse | GitLab | Form + upload control + final-submit control found, no CAPTCHA/login |
| Workable | Flosum | Form + upload control + final-submit control found, no CAPTCHA/login |
| SmartRecruiters | SmartRecruiters' own board | Form found, no CAPTCHA (a different posting shape than Phase 12's `oneclick-ui` DataDome finding) |
| Lever | `leverdemo` | Genuine, visible hCaptcha widget (`class="h-captcha"`, confirmed `visible=True`) -- a true positive |
| Ashby | Ashby's own board | reCAPTCHA v3 telemetry elements present -- ambiguous, conservatively still pauses |
| Workday | Walmart | Two bounded repeated observations disagreed -- `VARIABLE`, consistent with Phase 12 |

This run caught a real bug: a bare `"captcha" in content_lower` substring check (present since
Phase 10) was matching a defensively-loaded reCAPTCHA v3 script tag on Greenhouse/Lever/Ashby/
Workable's CURRENT real pages even when no challenge was ever rendered. Fixed by narrowing to the
three DOM-element-based checks alone -- see `docs/ats-canary-validation.md` for the full writeup
and verification that the real end-to-end CAPTCHA fixture still triggers correctly.
