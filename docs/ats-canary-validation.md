# ATS Canary Validation (Phase 13)

CLAUDE.md Phase 13 sections 13-14, 56, 74-77.

`app.applications.canary` is a safe, read-only application-flow canary. It
opens a configured public job/application page and observes provider/job-
identity/apply-entry/form/upload-control/step/login/CAPTCHA/final-submit
state — **without ever**:

- filling any candidate PII into the page (it never imports
  `app.applications.mapping` and never receives an `ApplicationField` list)
- uploading a resume (only *detects* a file-type input)
- clicking a final-submit control (only *detects* it)
- solving or bypassing a CAPTCHA (a sighting stops the canary immediately)

It reuses `app.applications.browser_runtime`'s own detection primitives
(`_wait_for_stable_state`, `_detect_fields`, `_detect_button`) rather than
duplicating DOM-scanning logic, and `app.applications.apply_entry`'s
`classify_apply_control_detailed` for the one bounded, freshly-classified
`NAVIGATION_SAFE` apply-entry hop it is allowed to follow (never a
final-submit click, never a loop — see "Bounded hop" below).

## Result shape

`CanaryResult`: `provider`, `tenant`, `site`, `url`, `ok`,
`captcha_detected`, `login_detected`, `apply_entry_found`,
`apply_entry_followed`, `form_found`, `upload_control_found`,
`final_submit_found`, `step_hint`, `error`.

Persisted append-only to `provider_canary_runs` via
`record_canary_run()`/`run_and_record_canary()`.

## Bounded hop

At most one apply-entry click, and only when:

- the page has not already surfaced a CAPTCHA or login wall, and
- a form was not already found, and
- exactly one control classifies `NAVIGATION_SAFE` with a real `href`.

This mirrors `app.applications.browser_assist._advance_through_apply_entry`'s
own one-hop-at-a-time, freshly-re-validated safety model.

## Scheduling

`run_scheduled_canaries()` is the **only** entry point that runs canaries
on a schedule, gated by `REAL_ATS_CANARY_ENABLED` (default `false`, never
enabled automatically) and intended to be paced no faster than
`REAL_ATS_CANARY_INTERVAL_HOURS`. One target's failure never aborts the
rest — matching this project's standing "one failing tenant/provider never
aborts the cycle" rule.

## CLI

```
python -m app.applications.cli canary <url> [--provider NAME]
```

## Bounded live validation (this phase)

`scripts/phase13_live_validation.py` reuses the exact same real,
previously-discovered public postings `scripts/phase11_live_validation.py`
and `scripts/phase12_live_validation.py` already vetted (their own
API-discovery calls, re-run here) — never a guessed URL. Real findings from
running it in this environment (see `docs/phase13-provider-resilience.md`
for the full table): Greenhouse (GitLab) and Workable (Flosum) reached a
real form with an upload control and a final-submit control, no CAPTCHA;
Lever's demo genuinely embeds a visible hCaptcha widget (a true positive);
Ashby's page carries reCAPTCHA v3 telemetry elements (ambiguous, correctly
conservative); SmartRecruiters' own careers board reached a form with no
CAPTCHA (a *different* posting shape than the `oneclick-ui` CAPTCHA Phase
12 found, consistent with "do not assume every SmartRecruiters posting
behaves identically"); Workday's Walmart tenant showed `VARIABLE` stability
across two bounded repeated observations, consistent with Phase 12's own
finding.

## A real bug this validation caught

The very first live run against Greenhouse/Lever/Ashby/Workable's
**current** pages reported `captcha_detected=True` on all of them, even
though no challenge was ever rendered. Investigation showed these
providers' pages now defensively load a reCAPTCHA v3 library `<script>`
tag on every visit (invisible verification, no widget shown) — the old
CAPTCHA heuristic in `app.applications.browser_runtime._do_discover()`
included a bare `"captcha" in content_lower` check across the ENTIRE page
text, which matched that script's own `src` URL. This has been narrowed to
the three DOM-**element**-based checks alone (an actual captcha-classed/
id'd element, or a captcha-`src` iframe) — verified to still catch the
real end-to-end fixture (`tests/browser_fixtures.py`'s
`<div class="g-recaptcha">`, since `"recaptcha"` contains `"captcha"` as a
substring) while no longer flagging a merely-referenced script tag. This
is a precision improvement, not a loosened safety boundary: a genuinely
rendered challenge (Lever's real hCaptcha widget, confirmed `visible=True`
in this run) still trips the pause, and this project never attempts to
solve or bypass one either way.
