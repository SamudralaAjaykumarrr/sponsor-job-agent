# Phase 10: Real ATS Assist

## Goal

Make a real application as close to one-click as legitimately possible, without ever
fabricating auto-submit support:

```
eligible FULL_TIME + CONFIRMED_SPONSOR job
  -> truthful resume + answers (Phase 1-9, unchanged)
  -> open the real ATS in a visible browser
  -> detect and map the real form
  -> fill safe, verified, non-sensitive fields
  -> upload the correct job-specific resume
  -> navigate a multi-step form where the form itself allows it
  -> pause for CAPTCHA / login / MFA / legal or unknown questions
  -> persist a safe, resumable handoff
  -> the user does the required thing themselves, in the same visible window
  -> continue / reconcile
  -> capture confirmation evidence
  -> mark APPLIED only when that evidence is genuine
```

Nothing in this phase adds a new way to auto-submit a real application. The only provider with
`submission_supported=True` remains the deterministic `mock_ats` fixture (Phase 8's own rule,
unchanged). Every real provider stays `ASSIST_ONLY` for its final step.

## What was built

| Module | Purpose |
|---|---|
| `app/applications/browser_session.py` | Persistent session model + DB-backed lifecycle/leasing (SQLite + Postgres) |
| `app/applications/browser_runtime.py` | The only module that touches Playwright: launch, DOM scan, safe fill, upload, step-advance, confirmation capture |
| `app/applications/browser_assist.py` | Orchestration: `start_session`/`resume_session`/`mark_user_action_complete`/`advance_step`/`close_session`/`attempt_user_submit_reconciliation` (plus the unchanged legacy `prepare_application()` one-shot helper) |
| `app/applications/domain_allowlist.py` | Navigation-safety allowlist per known ATS domain |
| `app/applications/browser_capability_matrix.py` | Data-only, dated, genuinely-observed browser-assist verification matrix (separate from the Phase 8 `ApplicationCapabilities` matrix) |
| `app/applications/background_scheduler.py` | Actually runs the Phase 9 reconciliation pass and the stale-session reaper on a schedule (both flags existed since Phase 9; nothing read them until now) |
| `app/applications/doctor.py` (extended) | 9 new browser-assist integrity checks |
| `app/applications/metrics.py` (extended) | `collect_browser_assist()` |
| `app/applications/cli.py` (extended) | `browser-start`/`browser-resume`/`browser-continue`/`browser-close`/`browser-reconcile`/`browser-status`/`browser-list`/`browser-capability-matrix` |
| Dashboard | `/applications/browser-sessions`, `/applications/browser-sessions/{id}`, `/applications/browser-capability-matrix`, plus a "Start Browser Assist" action on the job detail page |

See `docs/browser-assist-sessions.md` for the session model in depth, `docs/real-ats-validation.md`
for what was genuinely opened live this phase, `docs/greenhouse-application-assist.md` and
`docs/workday-application-assist.md` for the two providers CLAUDE.md's build brief calls out
by name.

## Why one generic engine, not per-provider adapters

`app.applications.browser_runtime`'s DOM scan/fill/detect code is provider-agnostic by
construction. It was opened, live, this phase, against real Greenhouse, Lever, and Ashby
application pages (three unrelated ATS platforms with completely different HTML/JS) and
correctly discovered 24, 22, and 28 real fields respectively, with no per-provider branching
anywhere in the engine. This is why Phase 10 did **not** build a `RealATSAssistProvider` class
hierarchy mirroring `app.applications.provider.ApplicationProvider` — that would reintroduce
exactly the per-provider special-casing the DOM-based approach exists to avoid. What genuinely
varies per provider (whether a real posting has actually been opened and inspected this phase,
vs. only exercised against a local fixture, vs. never attempted) is tracked as **data** in
`app.applications.browser_capability_matrix`, not as code.

## Safety boundaries (unchanged from Phase 9, reaffirmed)

- No stealth, no fingerprint spoofing, no CAPTCHA solving, no proxy rotation, no anti-bot
  bypass, no hidden/automated login, no MFA interception.
- Every browser context is fresh and ephemeral (`browser.new_context()`) — never
  `launch_persistent_context()`, never a saved `storage_state`. No password, MFA code,
  cookie, or auth token is ever a column in `browser_assist_sessions` or written to disk.
- `browser_runtime` never has a function that clicks a final submit/apply action — verified by
  a static doctor check (`_check_no_browser_auto_submit_capability`) that scans the module's own
  public API for a forbidden name pattern, not just a one-off test.
- Every navigation is checked against `domain_allowlist.is_allowed_host_for_session()` after each
  page load; an unexpected host pauses the session (`PAUSED_PLATFORM_RESTRICTED`) rather than
  continuing to interact with an unverified page.

## Two real bugs this phase's own live/E2E testing caught

1. **Domain-allowlist rejected every local `file://` fixture.** The original
   `is_allowed_host_for_session()` treated an *empty hostname* as automatically unsafe before
   ever comparing it to the original URL's hostname — but a `file://` URL (and, by the same
   logic, any two pages on the same host with no netloc at all) legitimately has an empty
   hostname on both sides. Every browser-marked test failed with `PAUSED_PLATFORM_RESTRICTED`
   until this was fixed to check for an empty **current URL string**, not an empty hostname.
2. **Radio/checkbox groups were labeled with their own first choice's text, not the question.**
   `Will you now or in the future require sponsorship?` (a `<fieldset><legend>`) was detected as
   label `"Yes"` (the first radio's own wrapping label) because the DOM-scan preferred a
   per-element label over the fieldset legend for every field type uniformly. Fixed so a
   radio/checkbox **group** prefers its fieldset legend (the actual question) over any single
   option's own choice text — single-value fields keep the opposite priority (their own label is
   the question). Caught live via `tests/test_browser_assist_e2e.py`'s conditional-sponsorship
   test against a real Chromium session.
3. **`advance_step()` false-positived `PAUSED_FORM_CHANGED` on every intentional step advance.**
   The same form-fingerprint-drift check `resume_session()` correctly uses to catch an
   *unexpected* mid-pause form change was also applied after an *intentional* multi-step
   `advance_step()` call — where the fields are supposed to be completely different (a new
   page). Fixed by adding a `check_drift` flag, `False` only for the advance-step path.

None of these were guessable from a code read alone — all three surfaced only once a real
Chromium session was actually driven end-to-end against realistic HTML.

## Running the real-browser test suite locally

```
pip install -r requirements-dev.txt      # includes playwright>=1.40
playwright install chromium
# If Chromium fails to launch with a "libnspr4.so: cannot open shared object" (or similar)
# error and you don't have root: download the missing .deb packages with
# `apt-get download <pkg>` (no root required), extract with `dpkg-deb -x <pkg> <dir>`, and
# set LD_LIBRARY_PATH to <dir>/usr/lib/x86_64-linux-gnu before running pytest. This was the
# exact workaround used to genuinely run tests/test_browser_assist_e2e.py in this project's own
# development sandbox, which had no system package manager root access.
pytest -m browser
```

Default `pytest` (no `-m` filter) never requires a browser — every browser-marked test skips
cleanly with a precise reason if Chromium can't actually launch.

## Honest limitations (see also each doc's own "Honest limitations" section)

- Browser fill does not mean auto-submit, for any real provider, ever.
- No real production application was submitted during this phase's development or validation.
- Provider support may change at any time (a real career page can change its form structure,
  add a CAPTCHA, or go offline — the Workday tenant dogfooded since Phase 3 was found offline
  during this phase's own live check, see `docs/real-ats-validation.md`).
- Login/CAPTCHA/MFA always require the user's own action in the visible window.
- Workday remains tenant-specific; no generic Workday auto-apply exists or is planned.
- Nothing here implies or predicts an interview or a job offer.
- Final candidate-side automation remains constrained by each ATS's own actual interface and
  policies — this project only ever uses what a real browser can observe/do on a public page a
  human could also open themselves.

## Recommended Phase 11

- A "click Apply first" pre-step for platforms (SmartRecruiters observed this phase) whose
  postings API returns a job-description landing page rather than the application form
  directly — detect and safely click a same-host "Apply"/"Apply Now" control (never the final
  submit) before running form discovery.
- Real, permission-gated live validation against Workday once a currently-live tenant is
  available (the Phase 3 dogfood tenant went offline between Phase 3 and this phase).
- Visual step/progress indicator parsing (many real multi-step ATS forms show "Step 2 of 4" —
  extracting that text would let `total_steps_if_known` be genuinely known rather than only a
  same-session "did we see a Next button" heuristic).
