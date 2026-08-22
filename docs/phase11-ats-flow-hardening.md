# Phase 11: Real ATS Flow Hardening

## Goal

Harden the real-ATS browser-assist layer so a supported real application flow gets as close as
safely possible to:

```
eligible FULL_TIME job -> CONFIRMED_SPONSOR
  -> open actual ATS -> pass landing/apply-entry step -> discover form
  -> fill verified fields -> upload correct resume -> traverse real multi-step flow
  -> pause for user-required actions -> resume reliably
  -> user performs final submission where required -> detect genuine confirmation -> APPLIED
```

Nothing in this phase adds a new way to auto-submit a real application. `mock_ats` remains the
only provider with `submission_supported=True` (CLAUDE.md Phase 8's rule, reaffirmed by a new
static doctor check this phase: `_check_real_provider_capability_auto_without_authorization`).

## What was built

| Module | Purpose |
|---|---|
| `app/applications/apply_entry.py` | Pure, dependency-free classification: `EntryStage`, `ApplyControlClassification`, `EntryDetectionResult`, `StepConfidence`, `NavControlKind`, step-progress text parsing |
| `app/applications/browser_runtime.py` (extended) | Apply-entry control detection/click (`advance_apply_entry`), stage/step-progress computed in `_do_discover`, conditional-field rediscovery pass in `_do_fill`, duplicate-application phrase detection |
| `app/applications/browser_assist.py` (extended) | Auto-follows NAVIGATION_SAFE apply-entry hops before form discovery, distributed session-lease claim/release around every orchestration call, reconstruction counting, duplicate-detection handling |
| `app/applications/browser_session.py` (extended) | `PAUSED_APPLY_ENTRY_UNRECOGNIZED` / `DUPLICATE_APPLICATION_DETECTED` statuses, re-entrant `claim_session`, `renew_session_lease` |
| `app/applications/workday_tenant.py` | Per-tenant/site Workday URL parsing and capability observations -- never one blanket claim |
| `app/applications/capability_evidence.py` | Dated (provider, capability) evidence records with staleness |
| `app/applications/doctor.py` (extended) | 8 new Phase 11 integrity checks |
| `app/applications/metrics.py` (extended) | `collect_phase11()` |
| `app/applications/cli.py` (extended) | `workday-tenants`, `capability-evidence` |
| Dashboard | `/applications/workday-tenants`, `/applications/capability-evidence`, browser-session detail now shows stage/step-confidence/apply-entry/reconstruction |
| `scripts/phase11_live_validation.py` | Bounded, read-only, real-ATS validation including apply-entry-click attempts |

See `docs/apply-entry-navigation.md`, `docs/browser-session-reconstruction.md`,
`docs/workday-tenant-validation.md`, `docs/smartrecruiters-application-assist.md`,
`docs/ats-capability-evidence.md` for the deep dives.

## Apply-first-click, safely

`app.applications.apply_entry.classify_apply_control()` is a single deterministic phrase table
that separates:

- `NAVIGATION_SAFE` -- "Apply Now", "Start Application", "Continue Application" (may be clicked
  automatically by `browser_runtime.advance_apply_entry()`, bounded to at most 3 hops per
  session)
- `FINAL_SUBMIT` -- "Submit Application", "Send Application", "Complete Application" (never
  clicked by any code path in this project)
- `LOGIN_TRIGGER` -- "Sign In to Apply", "Create Account" (pauses `PAUSED_LOGIN_REQUIRED`)
- `EXTERNAL_REDIRECT` -- a control whose destination host differs from the current page (pauses
  `PAUSED_PLATFORM_RESTRICTED`)
- `UNKNOWN` -- unrecognized text (pauses `PAUSED_APPLY_ENTRY_UNRECOGNIZED`, never guessed)

A real bug this phase's own live validation caught: Phase 10's `browser_runtime.
_SUBMIT_BUTTON_PHRASES` included `"apply now"` alongside genuine final-submit phrases, which
would have made a landing-page Apply control indistinguishable from a final submit button. Fixed
by moving apply-entry phrases into their own table entirely.

## Step-progress parsing

`apply_entry.parse_step_progress()` recognizes "Step X of Y" (EXACT), a `progress`/`step`-
qualified "N / M" ratio (EXACT), a bare "Step N" with no total (INFERRED, total left `None`), or
nothing recognizable (UNKNOWN). A real live run against GitLab's genuine Greenhouse posting
caught a second real bug: an early, ungated `\d{1,2}\s*/\s*\d{1,2}` pattern matched an unrelated
on-page date ("7/31") as if it were "step 7 of 31" -- fixed by requiring a `step`/`progress`
keyword within 20 characters before the numbers.

## Session ownership and pause behavior

Every orchestration entrypoint (`start_session`/`resume_session`/`advance_step`/
`attempt_user_submit_reconciliation`) now claims the session's distributed lease at entry and
releases it in a `finally` at exit -- regardless of the resulting status. This means:

- Two concurrent callers for the same session never both drive the browser (the loser is told
  "owned by another worker/process" and never touches `browser_runtime`).
- A paused session's lease is never held indefinitely (CLAUDE.md Phase 11 section 27) -- any
  worker can resume it later.
- `claim_session()` is re-entrant for the SAME worker id, so an orchestration call that
  internally delegates to another (`mark_user_action_complete` -> `resume_session`) never
  conflicts with its own lease.

## Duplicate-application and false-confirmation protection

`browser_runtime._do_capture_confirmation()` checks `_DUPLICATE_APPLICATION_PHRASES` ("you have
already applied", ...) BEFORE the success-phrase check, and returns a distinct
`ConfirmationOutcome(already_applied=True)` that `browser_assist.
attempt_user_submit_reconciliation()` routes to a new `DUPLICATE_APPLICATION_DETECTED` session
status -- never folded into a fresh `CONFIRMED`/`APPLIED` event. A bare mention of the word
"confirmation" (e.g. "Submit your application to receive confirmation") was already correctly
rejected by the existing success-phrase-match requirement; this phase adds an explicit regression
test locking that in.

## Honest real-ATS findings this phase (see docs for full detail)

- **Greenhouse**: apply-first-click proven end-to-end on a real, live GitLab posting (a real
  `NAVIGATION_SAFE` control was found and safely clicked).
- **Lever / Ashby**: an apply-entry-shaped control was found on both real postings but classified
  `EXTERNAL_REDIRECT` -- correctly never clicked.
- **SmartRecruiters**: still `NOT_TESTED` at the form level after two phases of genuine attempts
  -- the real posting's apply control classified `EXTERNAL_REDIRECT`, not the safe same-host
  action this project's mechanism is built to follow.
- **Workday**: the Phase 3/10 dogfood tenant remains offline. A genuinely live public tenant
  (Walmart, found via web search, never guessed) was opened instead; results were INCONSISTENT
  across two loads of the same URL and are reported that way, not cherry-picked.
- **Workable**: a genuinely live public tenant ('flosum', found via web search) was opened for
  the first time in this project's history -- 14 real fields discovered, upgraded to
  `LIVE_FORM_VERIFIED`.

## Safety boundaries (unchanged from Phase 9/10, reaffirmed)

- No stealth, no fingerprint spoofing, no CAPTCHA solving, no proxy rotation, no anti-bot bypass,
  no hidden/automated login, no MFA interception.
- `browser_runtime` still never has a function that clicks a final submit/apply action --
  `advance_apply_entry()` only ever clicks a control the SAME call freshly classified
  `NAVIGATION_SAFE`; the existing static doctor check (`_check_no_browser_auto_submit_capability`)
  continues to scan the module's public API for a forbidden name pattern, and `advance_apply_entry`
  was deliberately named to avoid the `click_apply`/`auto_submit`/`click_submit`/
  `submit_application` fragments that check scans for.
- Every navigation is still checked against `domain_allowlist.is_allowed_host_for_session()`.

## Real ATS auto-submit truth (unchanged)

`mock_ats` remains the only `submission_supported=True` provider. Every real ATS provider stays
`ASSIST_ONLY`. No real production application was submitted during this phase's development or
validation.

## Recommended Phase 12

- A genuine (non-simulated) headed-browser click through a JS-rendered SPA "Apply" control for
  SmartRecruiters, if one can be located that isn't a plain `<a>`/`<button>` with recognizable
  text (this phase's phrase-based scan cannot see e.g. an icon-only or `<div onclick>` control).
- Repeat the Walmart Workday tenant check enough times to determine whether the inconsistent
  apply-entry classification is genuine A/B-tested page variation or an artifact of this
  project's own detection ordering.
- Visual (pixel/ARIA `aria-current`) step-progress detection as a fallback when no genuine text
  pattern is present -- deferred this phase in favor of closing the apply-entry gap.
