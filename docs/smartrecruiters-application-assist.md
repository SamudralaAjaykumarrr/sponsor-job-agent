# SmartRecruiters Application Assist

## Phase 10's finding

The public postings API's candidate-facing URL
(`jobs.smartrecruiters.com/SmartRecruiters/<id>`) resolved to a job-description LANDING page, not
the application form directly -- only a "copy link to share (WeChat)" control was detected.
Marked `NOT_TESTED` at the form level, and Phase 10 explicitly recommended building an
apply-first-click mechanism as the top Phase 11 priority.

## What Phase 11 built and tried

`app.applications.apply_entry`/`browser_runtime.advance_apply_entry()` -- see
`docs/apply-entry-navigation.md` for the full mechanism, proven end-to-end on a real Greenhouse
posting.

`scripts/phase11_live_validation.py::validate_smartrecruiters()` fetched a fresh real posting from
`api.smartrecruiters.com/v1/companies/SmartRecruiters/postings`, opened its candidate-facing URL
in real headless Chromium, and ran `_detect_apply_entry_control()` against it.

## What was genuinely observed

A control WAS found on the page (`apply_entry_control_found=True`), but classified
`EXTERNAL_REDIRECT` -- its destination host differs from the current page's host. Per the safety
rule (CLAUDE.md Phase 11 section 6), this was correctly **never clicked**. The real form remains
unreached; `fields_detected` stayed at 1 (the same WeChat share widget Phase 10 found).

This was re-run twice (before and after a step-progress regex fix unrelated to SmartRecruiters)
with the same result both times.

## Honest assessment

SmartRecruiters' real candidate-facing posting genuinely does not expose a safe, same-host,
recognizably-labeled Apply link that this project's phrase-based DOM scan can find and follow.
Two non-exclusive, UNCONFIRMED hypotheses:

1. The actual "Apply" action is a JS-rendered SPA control (e.g. a `<div onclick>` or icon-only
   button with no matching visible text) that `_detect_apply_entry_control`'s
   `a, button, input[type=submit], input[type=button], [role="button"]` selector + text-phrase
   match genuinely cannot see.
2. The control that WAS found and classified `EXTERNAL_REDIRECT` may be the actual, intended
   apply action, routing candidates through an external ATS-hosted apply flow on a different
   subdomain -- in which case the correct behavior (pause, never auto-follow an
   `EXTERNAL_REDIRECT`) is exactly what happened, and reaching the real form would require a
   human to explicitly approve following that redirect.

Neither hypothesis is confirmed. `capability_evidence_records` for `smartrecruiters`/
`apply_first_click` is recorded `NOT_TESTED` with this exact ambiguity noted, not `LIVE_PUBLIC`,
and not inflated to guess which hypothesis is correct.

## Provider capability matrix impact

`app.applications.browser_capability_matrix`'s `smartrecruiters` row remains
`verification=NOT_TESTED` -- unchanged from Phase 10, now with a more specific note about exactly
what was tried and what was found, rather than "not attempted."

## Recommended next steps

- If a human candidate opens the same real posting and identifies the actual Apply control by
  inspecting the DOM manually, extend `_detect_apply_entry_control`'s selector list (e.g. add
  `[data-testid*="apply" i]` or a broader clickable-element scan) rather than assuming today's
  selector is exhaustive.
- Try a DIFFERENT real SmartRecruiters-hosted company posting (not SmartRecruiters' own board) --
  behavior may genuinely vary per company/tenant on this platform too (SmartRecruiters supports
  per-company branding/customization), matching CLAUDE.md Phase 11 section 46's "tenant/site
  variation" principle already established for Workday.
