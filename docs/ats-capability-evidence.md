# ATS Capability Evidence

## Purpose

`app.applications.capability_evidence` is the underlying, queryable EVIDENCE store the
hand-curated `app.applications.browser_capability_matrix` should be derived from: one row per
`(provider, capability)` pair, each carrying a `verification_type`, `observed_at` date, and
`source_domain`/`notes`. Distinct from that matrix (still the human-reviewed dashboard/docs
summary) -- this table lets staleness be computed mechanically instead of by memory.

## Verification types

- `LIVE_PUBLIC`: genuinely observed against a real, live, public posting this run.
- `FIXTURE`: only ever exercised against a local sandbox fixture (`tests/browser_fixtures.py`).
- `NOT_TESTED`: not attempted, or attempted but no positive evidence resulted (a control was
  found but correctly never followed, a page never reached a form, etc).

## Staleness

`config.CAPABILITY_EVIDENCE_MAX_AGE_DAYS` (default 30). `is_stale(row)` is `True` only for
`LIVE_PUBLIC` rows older than this threshold -- `FIXTURE`/`NOT_TESTED` rows are never time-
sensitive, so staleness doesn't apply to them. Staleness NEVER auto-disables a known-safe
capability (CLAUDE.md Phase 11 section 43); it only surfaces via:

- `app.applications.doctor._check_stale_capability_evidence` (warning severity)
- the `/applications/capability-evidence` dashboard page (a "STALE" badge)
- `python -m app.applications.cli capability-evidence` (an inline `[STALE]` marker)

## What genuinely populated this table this phase

`scripts/phase11_live_validation.py` records evidence for `field_discovery`, `resume_upload`,
`login_handoff`, `captcha_handoff`, `apply_first_click`, and (when genuinely EXACT) `step_progress`
for each of Greenhouse, Lever, Ashby, SmartRecruiters, Workday, and Workable -- see
`docs/phase11-ats-flow-hardening.md` for the full per-provider findings and
`docs/real-ats-validation.md` for the raw run output.

`apply_first_click` specifically is recorded `LIVE_PUBLIC` ONLY when a control was both
classified `NAVIGATION_SAFE` AND successfully clicked/navigated -- a control that was found but
correctly left unclicked (an `EXTERNAL_REDIRECT`/`LOGIN_TRIGGER`/`UNKNOWN` classification, the
safety mechanism working exactly as intended) is recorded `NOT_TESTED` with a note explaining
why, never inflated to look like a proven working click-through.

## Never write synthetic data here

Like every prior phase's benchmark convention, a script that generates SYNTHETIC evidence (for
load-testing the staleness query, say) must use a provider name that can never collide with a
real one (`benchmark-fixture`, matching `scripts/*_benchmark.py`'s existing convention) and must
never be pointed at a developer's real `data/app.db`. `scripts/phase11_live_validation.py` is NOT
synthetic -- it writes genuine, dated, real-observation rows and is meant to run against the real
database, the same as any other product code path that records real data.
