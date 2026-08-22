# Apply-Entry Navigation

## Why this exists

Some real ATS postings show a job-description LANDING page first -- the actual application form
only exists behind a further "Apply"/"Apply Now"/"Start Application" click. Phase 10 observed
this live on SmartRecruiters but had no safe mechanism to follow it. Phase 11 adds one.

## The five-stage model

`app.applications.apply_entry.EntryStage`:

```
LANDING_PAGE -> APPLICATION_ENTRY -> APPLICATION_FORM -> FINAL_REVIEW -> CONFIRMATION
```

- `LANDING_PAGE`: no form fields, but a recognized apply-entry control was found.
- `APPLICATION_ENTRY`: no form fields, no recognized control either (default/fallback).
- `APPLICATION_FORM`: real form fields present.
- `FINAL_REVIEW`: review/summary text detected (`is_review_page_text()`), regardless of whether
  a handful of fields are still present.
- `CONFIRMATION`: success-phrase text detected -- wins over everything else.

`classify_stage()` computes this from four booleans; see `app/applications/apply_entry.py` for
the exact priority order and `tests/test_apply_entry.py` for the full table of cases.

## Control classification

`classify_apply_control(text, href="", current_host="")` -> one of:

| Classification | Example text | What happens |
|---|---|---|
| `NAVIGATION_SAFE` | "Apply Now", "Start Application" | `advance_apply_entry()` may click it |
| `FINAL_SUBMIT` | "Submit Application", "Send Application" | Never clicked, ever |
| `LOGIN_TRIGGER` | "Sign In to Apply", "Create Account" | Session pauses `PAUSED_LOGIN_REQUIRED` |
| `EXTERNAL_REDIRECT` | any text, but `href` host != current host | Session pauses `PAUSED_PLATFORM_RESTRICTED` |
| `UNKNOWN` | unrecognized text | Session pauses `PAUSED_APPLY_ENTRY_UNRECOGNIZED` |

The phrase tables (`NAVIGATION_SAFE_PHRASES`, `FINAL_SUBMIT_PHRASES`, `LOGIN_TRIGGER_PHRASES`)
are deliberately disjoint -- no phrase appears in two tables, so classification never needs a
priority tie-break between two simultaneously-matching categories.

**Real bug this phase fixed**: Phase 10's `browser_runtime._SUBMIT_BUTTON_PHRASES` included
`"apply now"`. That table drove BOTH "is this a final submit button" (never click) AND, until
this phase, there was no separate apply-entry concept at all -- so a genuine "Apply Now" landing
control had no path to being safely followed. Fixed by giving apply-entry its own table and
removing `"apply now"` from the final-submit table.

## How a click happens

`app.applications.browser_runtime._detect_apply_entry_control(page, current_host)` scans every
visible `a`/`button`/`input[type=submit]`/`input[type=button]`/`[role="button"]`, classifies each
via the SAME `classify_apply_control()` function, and returns the first `NAVIGATION_SAFE` match
(or, if none, the first `LOGIN_TRIGGER`/`EXTERNAL_REDIRECT` match, for honest pause-reason
reporting -- never returned as something to click).

`_do_advance_apply_entry()` re-derives this FRESH (never trusts a stale control from an earlier
discovery call) and refuses to click anything not classified `NAVIGATION_SAFE` in that same call.
The public wrapper is `browser_runtime.advance_apply_entry(session_id)` -- deliberately NOT named
with a `click_apply`/`auto_submit`/`click_submit`/`submit_application` fragment, since
`app.applications.doctor._check_no_browser_auto_submit_capability` statically scans
`browser_runtime`'s public API for those exact patterns.

`app.applications.browser_assist._advance_through_apply_entry()` is the orchestration-layer loop
that calls this repeatedly (bounded to `_MAX_APPLY_ENTRY_HOPS = 3`) to handle a multi-hop chain
(career page -> ATS landing page -> account/start page), re-validating the domain allowlist and
CAPTCHA/login state after every single hop via the normal `rediscover()` path -- an apply-entry
click is not exempt from any existing safety check.

## Entry-detection result

`detect_entry_result()` normalizes one page's situation:

```
ENTRY_READY | FORM_ALREADY_VISIBLE | LOGIN_REQUIRED | REDIRECT_REQUIRED
  | USER_ACTION_REQUIRED | UNSUPPORTED
```

`FORM_ALREADY_VISIBLE` always wins when real fields are present (a page can legitimately have
both an apply control AND fields, e.g. a single-page form with an unrelated "back to search"
link) -- checked directly on `outcome.fields`, not just the derived enum, so a `DiscoveryOutcome`
built without an explicit `entry_detection_result` (every pre-Phase-11 test/call site) is never
mistaken for "no form" when fields are actually present.

## What this phase genuinely proved live

See `docs/phase11-ats-flow-hardening.md` and `docs/real-ats-validation.md` for the full,
per-provider findings from `scripts/phase11_live_validation.py` -- apply-first-click was proven
end-to-end on a real Greenhouse posting; SmartRecruiters/Lever/Ashby postings each had an
apply-entry-shaped control that was correctly classified something OTHER than `NAVIGATION_SAFE`
and therefore never clicked.
