# Workday Application Assist

## Why Workday is different

Workday tenants are hosted per-employer (`{tenant}.{wdN}.myworkdayjobs.com`), frequently require
candidate account creation/login before the real application form is even reachable, and often
span several distinct pages per application. CLAUDE.md is explicit: **do not build a fake
universal Workday auto-apply system**, and never automate account creation or bypass login/MFA.

## What this project supports

`app.applications.browser_assist`/`browser_runtime`'s generic engine applies to Workday exactly
like any other provider — there is no Workday-specific code path, by design (see
`docs/phase10-real-ats-assist.md`'s "why one generic engine" section). Concretely, for a Workday
posting:

1. `browser_assist.start_session()` opens the real application URL in a visible browser.
2. `domain_allowlist` recognizes `myworkdayjobs.com`/`myworkdaysite.com`/`workday.com` as
   expected hosts for this provider, so normal Workday-internal redirects (tenant subdomain,
   session routing) never trigger `PAUSED_PLATFORM_RESTRICTED`.
3. If the page presents a password field, the session pauses `PAUSED_LOGIN_REQUIRED` — the
   candidate logs in themselves in that same visible window (this project never touches Workday
   credentials), then triggers **Mark User Action Complete**, which rediscovers the
   now-authenticated page from scratch.
4. Ordinary field discovery/safe-autofill/resume-upload/multi-step-advance then behaves exactly
   as documented in `docs/browser-assist-sessions.md`.

## Live validation status (honest)

The Phase 3 discovery connector previously dogfooded a real Workday tenant
(`workday.wd5.myworkdayjobs.com/Workday` — Workday's own careers site, used because it is a
company validating its own product against its own careers page). This phase's own live check
(`scripts/phase10_live_validation.py`) found that tenant's CXS API now redirects to
`community.workday.com/maintenance-page` — it is **currently unavailable**, not a defect in this
project's code. No substitute tenant was guessed or fabricated.

**Result: `verification=NOT_TESTED`** for Workday in
`app.applications.browser_capability_matrix` as of this phase. The mechanism itself (domain
allowlisting, login-pause/resume, generic DOM discovery) is exercised by the local sandbox E2E
suite (`tests/test_browser_assist_e2e.py`), which is provider-agnostic HTML, not a Workday-shaped
fixture specifically.

## What will never be built

- Automated Workday account creation.
- Any bypass of Workday's login or MFA.
- A "smart" per-tenant selector library — Workday tenants vary too much for a static list to
  stay correct, so the generic, real-DOM discovery engine is deliberately the only mechanism.
- Final-submit automation. Workday's `submission_supported` stays `False` regardless of how
  thoroughly a tenant is verified.

## Recommended next step

Re-run `scripts/phase10_live_validation.py`'s Workday check against a currently-live Workday
tenant (any real employer's own public careers page) before upgrading this provider's row past
`NOT_TESTED` — never bump the matrix from a stale or fabricated observation.
