# Workday Observation Model

How this project tracks and classifies genuinely-varying Workday tenant behavior (CLAUDE.md
Phase 11 sections 10-13, 45; Phase 12 sections 18-21, 54, 68, 77). Implemented in
`app.applications.workday_tenant`.

## Why per-attempt, not just per-tenant

Phase 11 introduced `workday_tenant_observations`, ONE row per (tenant, site), upserted on every
observation -- an aggregate "current capability" view. That is still useful (and unchanged), but it
cannot answer "was this tenant's behavior actually consistent, or did I just get lucky/unlucky on
one run?" -- the exact question Phase 11's own finding (two loads of the same Walmart tenant URL
producing different apply-entry classifications) left open.

Phase 12 adds `workday_tenant_attempts`: an APPEND-ONLY log, one row per real observation, never
overwritten. `record_attempt()` inserts; nothing in this module ever deletes or updates a prior
attempt.

## Stability classification

`classify_stability(tenant, site)` returns one of:

- **`UNVERIFIED`** -- fewer than 2 attempts recorded. Not enough evidence either way.
- **`STABLE`** -- 2+ attempts, and every attempt's `result` field is identical.
- **`VARIABLE`** -- 2+ attempts with genuine disagreement. Reported honestly, never cherry-picked
  to whichever run looked cleaner.
- **`STALE`** -- the most recent attempt is older than `CAPABILITY_EVIDENCE_MAX_AGE_DAYS` (default
  30), regardless of what it once showed. Staleness always wins over STABLE/VARIABLE.

`stability_report()` returns a per-tenant summary (`consistent_count`/`variable_count` out of
`attempt_count`) for the dashboard and `python -m app.applications.cli workday-stability`.

## This phase's real repeated observation

`scripts/phase12_live_validation.py::validate_workday_repeated()` reloaded the SAME real Walmart
Workday posting (`walmart.wd504.myworkdayjobs.com/WalmartExternal`, found via web search in Phase
11, re-verified live this phase) 3 times, 3 seconds apart:

```
per_attempt_results: ["LOGIN_TRIGGER", "LOGIN_TRIGGER", "NAVIGATION_SAFE"]
stability: VARIABLE  (2/3 consistent)
```

This CONFIRMS Phase 11's single-pair finding with genuine repeated evidence, rather than resolving
it either way -- consistent with CLAUDE.md Phase 12 section 77 ("success means repeated
observations clarify behavior; it may still remain VARIABLE/ASSIST_ONLY. Do not force
consistency"). The underlying cause (A/B-tested page variation, hydration timing, or session/
cookie state affecting which control the candidate scan finds first) remains genuinely
undetermined by this project's read-only, unauthenticated observation method -- honestly reported
as unknown, not guessed.

## Never generalized

- `classify_stability` operates on exactly one (tenant, site) pair. `walmart/WalmartExternal`
  being VARIABLE says nothing about any other tenant.
- `app.applications.doctor._check_workday_universal_claim_from_one_tenant` statically enforces
  this at the capability-matrix level: `app.applications.browser_capability_matrix`'s Workday row
  can never claim `LIVE_FORM_VERIFIED` unless at least one tenant/site has genuinely repeated
  `STABLE` evidence behind it. Today that row remains `NOT_TESTED` -- honest, since no tenant has
  reached STABLE.

## Per-attempt fields recorded

`tenant`, `site`, `host`, `requisition_id`, `url_initial`, `url_final`, `stage`,
`apply_control_result`, `render_time_ms`, `fields_detected`, `resume_upload_detected`,
`step_indicator`, `result`, `notes`, `observed_at`. No candidate PII -- only ids, stage/result
labels, counts, and durations, matching every other structured-event table in this project.
