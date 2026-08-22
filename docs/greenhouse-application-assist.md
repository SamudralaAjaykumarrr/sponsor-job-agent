# Greenhouse Application Assist

Greenhouse has two genuinely separate paths in this project, both real, neither fabricated:

## 1. Network-API discovery (Phase 8, unchanged)

`app/applications/providers_greenhouse.py::GreenhouseApplicationProvider` calls the public,
documented `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true` endpoint —
the same officially documented Job Board API the Phase 3 discovery connector already uses, just
with `questions=true`. This returns real structured field metadata (name/label/type/required/
choices) without ever opening a browser. `submission_supported=False`: Greenhouse's actual
"Apply" action goes through the site's own embedded, CSRF-protected form flow, not this
documented API, so automating a submission through it would mean reverse-engineering an
undocumented interface — explicitly out of scope per CLAUDE.md.

## 2. Real-browser assist (Phase 10, this doc)

`app.applications.browser_assist`/`browser_runtime` open the job's real, public candidate-facing
application page in a visible (or headless, if configured) browser and run the same generic
DOM-scan/fill engine every other provider uses.

### Live validation (this phase, `scripts/phase10_live_validation.py`)

A real posting on GitLab's own public Greenhouse board (`boards-api.greenhouse.io` token
`gitlab`) was opened live:

- **24 real fields detected**, including First/Last Name, Email, Phone, LinkedIn, two resume/
  attachment upload controls, a free-text question, and — genuinely present on this posting —
  `"Will you now or in the future require sponsorship for a visa to remain in your current
  location?*"`.
- A real **submit button** was detected (never clicked).
- A real **CAPTCHA widget** was present on this posting and correctly detected
  (`captcha_observed: true`) — proof the detection logic works against an actual production
  CAPTCHA, not just the local sandbox fixture.
- No login wall was present on this specific posting.
- The final URL stayed on `job-boards.greenhouse.io`, matching the domain allowlist.

### What this proves vs. what it doesn't

- Proves: the generic engine correctly discovers a real, production Greenhouse application
  form's fields, upload control, submit control, and CAPTCHA presence.
- Does **not** prove: multi-step navigation on a real Greenhouse posting (the one checked was
  single-page) — verified instead on the local sandbox fixture
  (`tests/test_browser_assist_e2e.py::test_multi_step_form_advance_reaches_step_two`).
- Does **not** attempt, and will never attempt, an actual submission against a real Greenhouse
  posting.

### Capability declaration

See `app.applications.browser_capability_matrix` (`provider="greenhouse"`,
`verification="LIVE_FORM_VERIFIED"`) and `/applications/browser-capability-matrix`. Both the
network-API path and the browser-assist path independently declare
`submission_supported`/`final_submit_automation` as `False`.
