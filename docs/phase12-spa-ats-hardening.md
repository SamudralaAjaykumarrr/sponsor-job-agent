# Phase 12: SPA/Dynamic ATS Flow Hardening

## Goal

Harden the real-ATS browser-assist layer for JS-rendered/SPA application flows and resolve the
remaining real-provider uncertainty from Phase 11:

```
eligible FULL_TIME job -> CONFIRMED_SPONSOR
  -> open actual ATS -> wait for dynamic content to render (bounded, never a blind sleep)
  -> pass landing/apply-entry step, including a trusted cross-domain redirect where evidence supports it
  -> discover form (including inside an allowed iframe or an open shadow root)
  -> verify the page still corresponds to the intended job
  -> fill verified fields -> upload correct resume -> traverse real multi-step flow
  -> pause for user-required actions -> resume reliably
  -> user performs final submission where required -> detect genuine confirmation -> APPLIED
```

Nothing in this phase adds a new way to auto-submit a real application. `mock_ats` remains the
only provider with `submission_supported=True`.

## What was built

| Module | Purpose |
|---|---|
| `app/applications/trusted_redirects.py` | Deterministic redirect-trust model (`classify_redirect_trust`) and application-URL provenance (`resolve_application_url`) |
| `app/applications/job_identity.py` | Conservative requisition-token extraction/comparison to catch a session ending up on a different job's form |
| `app/applications/apply_entry.py` (extended) | `classify_apply_control_detailed` (reason/evidence, redirect-trust-aware), `select_apply_control` (ambiguous-multi-control detection), `is_valid_stage_transition` |
| `app/applications/browser_runtime.py` (extended) | Bounded DOM-stabilization wait (`_wait_for_stable_state`), SPA route-change detection (`_do_advance_to_route`), iframe scan (`_scan_iframes`, including cross-frame fill targeting), shadow-DOM-piercing DOM scan (`_DEEP_QUERY_JS`), job-identity check wired into discovery |
| `app/applications/browser_assist.py` (extended) | Prefers a provider-resolved direct application URL over an unnecessary landing hop, persists iframe/shadow/provenance fields, logs stage-transition anomalies |
| `app/applications/workday_tenant.py` (extended) | Append-only per-attempt observation log (`record_attempt`/`list_attempts`) and stability classification (`classify_stability`: STABLE/VARIABLE/UNVERIFIED/STALE), never generalized across tenants |
| `app/applications/capability_evidence.py` (extended) | `STATIC_HTML`/`REAL_BROWSER`/`REAL_BROWSER_REPEATED` verification types with a `repeat_count` that strengthens (never inflates) confidence |
| `app/applications/spa_events.py` | Append-only structured event log backing the new metrics and doctor checks |
| `app/applications/doctor.py` (extended) | 4 new Phase 12 integrity checks |
| `app/applications/metrics.py` (extended) | `collect_phase12()` |
| `app/applications/cli.py` (extended) | `workday-stability` |
| Dashboard | Workday tenant matrix now shows per-tenant stability; capability-evidence page shows repeat counts; browser-session detail shows iframe/shadow/URL-provenance |
| `scripts/phase12_live_validation.py` | Bounded, read-only, real-ATS validation including repeated Workday observations and a real trusted-redirect check |

See `docs/spa-application-navigation.md`, `docs/trusted-ats-redirects.md`,
`docs/workday-observation-model.md`, `docs/smartrecruiters-spa-validation.md` for the deep dives.

## Real, live-caught bugs this phase

1. **`trusted_redirects.classify_redirect_trust` initially treated `file://` as an unsafe scheme.**
   This project's *entire* browser-fixture test convention (`tests/browser_fixtures.py`) is
   `file://`-based -- a real live-Chromium run of the Phase 11 regression suite caught every
   apply-entry fixture failing immediately after this module was wired in. Fixed by adding the
   same `file://` carve-out `app.applications.domain_allowlist` already established.
2. **A field discovered inside an allowed-host iframe could not actually be filled.** The fill
   path (`_LiveSession._fill_one`/`_upload_one`) always targeted `self.page`, never the iframe's
   own `Frame` object -- a real live test against a same-origin-iframe fixture caught the field
   being correctly *discovered* but every fill silently failing. Fixed by tagging each
   iframe-sourced field dict with its source `Frame` (`rf["_frame"]`) and filling against that
   frame when present.
3. **The submit/next button scan never looked inside an iframe either.** Same fixture caught the
   session reaching `ACTIVE` (fields filled) instead of `READY_FOR_FINAL_SUBMIT`, because
   `_detect_button` only ever scanned the main document. Fixed by having `_scan_iframes` also
   locate the submit/next control within the same allowed frame.

None of these were guessable from a code read alone -- all three surfaced only once a real
Chromium session was actually driven end-to-end against realistic HTML, exactly the pattern
Phase 10/11 already documented.

## Honest real-ATS findings this phase (see the linked docs for full detail)

- **Greenhouse**: regression-confirmed. GitLab's board organically migrated from
  `boards.greenhouse.io/gitlab` to `job-boards.greenhouse.io/gitlab` between phases (a real ATS
  URL-shape change, not a project bug) -- the domain-allowlist suffix match and apply-first-click
  both kept working with zero code changes.
- **Lever / Ashby / Workable**: regression-confirmed, all still `LIVE_FORM_VERIFIED`. Lever's
  apply-entry-shaped control is confirmed `EXTERNAL_REDIRECT` with `redirect_trust=UNTRUSTED` (not
  merely "off-host" -- genuinely not a recognized ATS vendor domain), so the trusted-redirect
  model correctly does NOT reclassify it.
- **Trusted redirects**: proven against a real, live, non-ATS host. GitLab's own corporate
  careers page (`about.gitlab.com`, not a `greenhouse.io` domain) links to 10 real
  `job-boards.greenhouse.io` postings; all 10 classify `TRUSTED_ATS_REDIRECT`. See
  `docs/trusted-ats-redirects.md`.
- **SmartRecruiters**: conclusively characterized (success criterion B, CLAUDE.md section 76), not
  forced to a fake success. A newer, web-search-discovered "oneclick-ui" client-rendered posting
  shape was opened live and encountered a genuine DataDome bot-detection CAPTCHA
  (`geo.captcha-delivery.com`) before any content rendered -- correctly detected, never bypassed.
  The classic `postingUrl` shape's public API returned no postings for the company tried this run
  (honestly reported `NOT RUN`). See `docs/smartrecruiters-spa-validation.md`.
- **Workday**: the same real Walmart tenant from Phase 11 was reloaded 3 more times (bounded,
  spaced) this phase. Results: `LOGIN_TRIGGER, LOGIN_TRIGGER, NAVIGATION_SAFE` -- classified
  `VARIABLE` (2/3 consistent), confirming Phase 11's finding with genuine repeated evidence rather
  than resolving it either way. See `docs/workday-observation-model.md`.

## Safety boundaries (unchanged, reaffirmed)

- No stealth, no fingerprint spoofing, no CAPTCHA solving, no proxy rotation, no anti-bot bypass,
  no hidden/automated login, no MFA interception -- the SmartRecruiters CAPTCHA encounter above
  was detected and left alone, exactly as required.
- `browser_runtime` still never has a function that clicks a final submit/apply action --
  `_check_no_browser_auto_submit_capability` continues to statically scan the module's public API.
- Shadow-DOM discovery only ever pierces OPEN roots (`el.shadowRoot` is null for a closed one --
  never bypassed). Iframe discovery only ever reads frames Playwright can normally read (the same
  access a browser's own devtools has), never a cross-origin sandbox bypass.
- The trusted-redirect model only ever trusts the SAME per-provider domain suffixes
  `app.applications.domain_allowlist` already used for post-navigation host checks -- never a
  broader, second allowlist. `app.applications.doctor._check_unsafe_redirect_allowlist` statically
  enforces this.

## Real auto-submit truth (unchanged)

`mock_ats` remains the only `submission_supported=True` provider. Every real ATS provider stays
`ASSIST_ONLY`. No real production application was submitted during this phase's development or
validation.

## Test results

- Default `pytest` (offline, no browser/network): 956 passed (up from Phase 11's 856 baseline --
  the branch already carried some additional work before this phase started; the delta from this
  phase's own additions is the ~100 new/updated test cases across `tests/test_trusted_redirects.py`,
  `tests/test_job_identity.py`, the extended `tests/test_apply_entry.py`, `tests/test_workday_tenant.py`,
  `tests/test_capability_evidence.py`, the new Phase 12 doctor/metrics test files, and
  `tests/test_browser_assist_phase12_e2e.py`).
- `pytest -m browser` (real Chromium, launched via a documented non-root library workaround --
  see `docs/architecture.md`): 38 passed (27 Phase 11 + 11 new Phase 12), 0 failed.
- `pytest -m postgres` (embedded `pgserver`): 35 passed, matching Phase 11's documented baseline
  exactly -- no regression from the new additive schema.
- `python -m app.applications.doctor` (via the CLI): 0 serious, 0 warnings on the real dev
  database after this phase's changes.

## Recommended Phase 13

- Determine whether the SmartRecruiters `oneclick-ui` CAPTCHA challenge is present on every
  posting of this shape or only some (a genuinely different, larger sample, still bounded and
  respectful of rate limits).
- A visual/`aria-current` step-progress fallback (deferred again this phase, same as Phase 11's
  recommendation).
- Extend the job-identity check beyond requisition-token comparison to a second signal (e.g. job
  title similarity) for providers whose URLs carry no confidently-extractable token at all.
