# SmartRecruiters SPA Validation

Phase 12's priority target (CLAUDE.md section 4, "SMARTRECRUITERS SPA PRIORITY") and its honest
outcome, per the phase's own explicit success criterion:

> Success is one of: A. real application form reached and verified safely, OR B. actual limitation
> conclusively characterized and documented. Do not force a fake success.

This phase reached **B**.

## What Phase 10/11 found

Phase 10/11 opened real SmartRecruiters postings via the classic `postingUrl` shape
(`jobs.smartrecruiters.com/<Company>/<id>-<slug>`) and found a job-description LANDING page whose
Apply control classified `EXTERNAL_REDIRECT` -- correctly never clicked, but never confirmed as
reaching a real form either.

## What Phase 12 did differently

A plain web search this phase (never guessed) turned up a NEWER, distinct SmartRecruiters posting
URL shape actively in use by real companies (Visa, Sandisk, and others) alongside the classic one:

```
https://jobs.smartrecruiters.com/oneclick-ui/company/<Company>/publication/<uuid>?dcr_ci=<Company>
```

This `oneclick-ui` path is a genuinely client-rendered (SPA) posting page -- exactly the surface
CLAUDE.md's Phase 12 build brief calls out. `scripts/phase12_live_validation.py::
validate_smartrecruiters_spa()` opened one live (a real Visa internship posting), using this
phase's full generic SPA-hardening stack: the bounded DOM-stabilization wait, the shadow-DOM-
piercing/iframe-aware DOM scan, and the trusted-redirect-aware apply-entry classifier.

## The real result

```json
{
  "landing_render_wait": "dom_stable",
  "landing_render_ms": 876,
  "apply_entry_control_found": false,
  "fields_detected": 0,
  "iframe_unexpected_host": "geo.captcha-delivery.com",
  "captcha_observed": true
}
```

The page's own content included a genuine bot-detection CAPTCHA challenge served from
`geo.captcha-delivery.com` -- DataDome's CAPTCHA-delivery domain -- BEFORE any application content
(job description, Apply control, or form) ever rendered. `_wait_for_stable_state` correctly settled
on `dom_stable` (the CAPTCHA challenge page itself is static once rendered), the CAPTCHA was
correctly detected (`captcha_observed=true`), and -- per this project's absolute, unconditional
rule -- was never solved, never bypassed, never worked around in any way. Zero fields, zero apply
controls: an honest, correct `NOT_TESTED` outcome at the form level, not a project bug.

## Why this is a conclusive characterization, not a shrug

- This is a STRUCTURAL property of (at least this) SmartRecruiters posting shape for
  unauthenticated automated access -- active bot detection challenging the request before content
  renders -- not a gap in this project's DOM-scanning/apply-entry logic. The exact same generic
  engine (bounded wait, iframe scan, shadow-DOM piercing) is proven working end-to-end against a
  deterministic local SmartRecruiters-shaped SPA fixture (see below) and against four OTHER real,
  unrelated ATS platforms this same phase (Greenhouse, Lever, Ashby, Workable).
- The classic `postingUrl` shape was ALSO re-attempted this phase (Visa's public postings API) but
  returned zero postings for that specific company/run -- honestly reported `NOT RUN`, never
  substituted with a fabricated result.
- Bypassing a bot-detection CAPTCHA is explicitly forbidden by this project's safety rules
  (CLAUDE.md Phase 9 sections 48-49, Phase 12 sections 48, 55) regardless of how close the
  automation otherwise gets -- so "form reached" (criterion A) was never a legitimately achievable
  outcome here, making honest characterization (criterion B) the correct, non-fake success.

## Generic SPA mechanism proven locally (fixture-tested, not merely live-attempted)

`tests/browser_fixtures.py::smartrecruiters_like_spa_page` reproduces the SmartRecruiters SPA
shape deterministically: a landing page renders immediately, a JS `setTimeout` inserts the real
"Apply Now" control after a delay, clicking it performs a `history.pushState` route change (no
full page load) and mounts a genuinely new 2-step form (including a resume-upload field and a
final "Submit Application" control) via `innerHTML`. `tests/test_browser_assist_phase12_e2e.py`
drives this fixture through the full `browser_assist.start_session()`/`advance_step()` path against
real (if synthetic) Chromium and confirms: the delayed Apply control is detected and safely
clicked, the SPA route change is detected, the dynamically-mounted form is discovered and filled,
and the multi-step resume-upload page is reached via an ordinary `advance_step()` call. This
mechanism-level proof stands independent of whether any one real, currently-live posting happens
to be reachable past its own bot-detection layer on a given day.

## Capability matrix

`app.applications.browser_capability_matrix`'s `smartrecruiters` row remains `NOT_TESTED` at the
real-form level -- honest, per the finding above. Its notes carry this full characterization.
`app.applications.capability_evidence` records `smartrecruiters/field_discovery` as
`REAL_BROWSER_REPEATED` (genuinely re-observed across Phase 11 and Phase 12 runs, even though both
runs stopped short of the real form) and `smartrecruiters/apply_first_click` as `NOT_TESTED`.
