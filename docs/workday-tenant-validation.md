# Workday Tenant Validation

> **Phase 12 update**: repeated per-attempt observations (never overwritten) and stability
> classification (STABLE/VARIABLE/UNVERIFIED/STALE) were added on top of this aggregate model --
> see `docs/workday-observation-model.md` for the full mechanism and this phase's genuine 3x
> repeated Walmart-tenant result.

## Core principle: per-tenant, never universal

CLAUDE.md is explicit: "Track Workday behavior by tenant/site rather than claiming one universal
adapter." `app.applications.workday_tenant` is the data model for this -- one row per
`(tenant, site)` pair, each capability column independently nullable (`NULL` = not observed,
distinct from `0` = observed absent). `render_tenant_matrix()` and the
`/applications/workday-tenants` dashboard page always render one row per tenant, never a
collapsed summary line.

## URL parsing

`parse_workday_tenant(url)` recognizes both the candidate-facing page shape
(`https://{tenant}.{wdHost}/{site}/job/{location}/{title}_{requisition}`) and the CXS API shape
(`https://{tenant}.{wdHost}/wday/cxs/{tenant}/{site}/jobs`). It never guesses a tenant that isn't
actually present in the URL -- an unrecognized host returns `recognized=False`, `tenant=""`.

## Genuine live tenants used

**Phase 3/10 dogfood tenant** (`workday.wd5.myworkdayjobs.com/Workday`): re-checked this phase,
still redirects (HTTP 303) to `community.workday.com/maintenance-page`. Genuinely offline, not a
code defect. No substitute was guessed for this specific tenant.

**Phase 11's own tenant** (`walmart.wd504.myworkdayjobs.com/WalmartExternal`): found via a plain
web search for `site:myworkdayjobs.com jobs careers` -- a real, publicly documented careers board
(the same category of open discovery `app.registry.page_discovery` already performs), never
fabricated. Its CXS API (`POST /wday/cxs/walmart/WalmartExternal/jobs`) returned real job postings
with HTTP 200.

## What was genuinely observed

A single real posting (`.../job/CAN-AB-MEDICINE-HAT-03150-WM-SUPERCENTER/PT-OMNI-Customer-
Fulfillment-Associate_R-2618734`) was opened in real headless Chromium twice (two separate runs of
`scripts/phase11_live_validation.py`). Results were **not consistent** between the two runs:

| Run | Apply-entry control found | Classification | Followed | Fields reached |
|---|---|---|---|---|
| 1 | yes | `NAVIGATION_SAFE` | click attempted, did not complete within the bounded timeout | 0 |
| 2 | yes | `LOGIN_TRIGGER` | not applicable (never NAVIGATION_SAFE) | 0 |

This is reported honestly as **inconsistent, not proven working end to end** -- not cherry-picked
to the more favorable first run. The most likely explanation (not confirmed) is that the real
page presents both an "Apply" and a "Sign In"/account-related control, and which one the
phrase-based DOM scan encounters/prioritizes varies with page load timing or minor content
variation between requests. `capability_evidence_records` for `workday`/`apply_first_click` is
recorded `NOT_TESTED` with this exact explanation, not `LIVE_PUBLIC`.

`field_discovery` IS recorded `LIVE_PUBLIC` for `workday`, since the real page (before/without the
apply click) was genuinely opened and its DOM genuinely scanned (0 fields is itself a real,
correctly-reported observation, not a failure to run the scan).

The `workday_tenant_observations` row for `(walmart, WalmartExternal)` records
`landing_navigation=True` (the page opened, domain-allowlist-matched, without any error) and
`login_required=False` for THIS specific run's outcome, with `multi_step`/`custom_questions`/
`review_page`/`confirmation_detection` left `NULL` (never observed, since the real form was never
reached).

## Recommended next steps

- Re-run against the same tenant/posting several more times to determine whether the
  inconsistency is genuine A/B-tested page variation or a `_detect_apply_entry_control` candidate-
  ordering artifact worth hardening (e.g. preferring a control whose `aria-label`/context
  explicitly says "apply" over a bare "Sign In" link when both are visible).
- Try a SECOND real, live Workday tenant (also found via search, never guessed) to see whether
  the inconsistency is tenant-specific or a general Workday-flow characteristic.
- Never claim "Workday supported" from this data -- only ever "tenant X site Y observed behavior
  Z on date D", exactly as `render_tenant_matrix()` already renders it.
