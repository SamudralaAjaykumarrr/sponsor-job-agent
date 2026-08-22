# Phase 13: Provider Resilience and Real-World ATS Reliability

## Goal

Make the browser-assist/discovery system resilient enough for production-style everyday
operation against changing real ATS application flows, without ever attempting real
submission or bypassing any anti-bot/CAPTCHA/auth mechanism.

## What was built

| Module | Purpose |
|---|---|
| `app/applications/title_normalization.py` (new) | Deterministic title canonicalization/equivalence, never fuzzy similarity |
| `app/applications/job_identity.py` (extended) | Formal multi-signal `JobIdentityVerification` (`verify_job_identity_full`) + bounded evidence persistence (`job_identity_verifications`), layered on top of the unchanged Phase 12 single-signal check |
| `app/applications/provider_health.py` (new) | Real-browser assist-flow health per (provider, tenant, site), separate from discovery/submission circuit breakers |
| `app/applications/confirmation_evidence.py` (new) | `STRONG`/`MODERATE`/`WEAK`/`NONE` confirmation evidence grading |
| `app/applications/checkpoints.py` (new) | Append-only session checkpoint log + advisory ordering-anomaly detection |
| `app/applications/resume_integrity.py` (new) | Resume/JD-fingerprint staleness check before upload |
| `app/applications/canary.py` (new) | Safe, read-only, PII-free application-flow canary validation |
| `app/applications/browser_runtime.py` (extended) | JSON-LD job-meta extraction, identity full-check wired before upload/final-submit, provider-health hooks, confirmation-evidence grading, a real CAPTCHA-detection precision fix (see below) |
| `app/applications/browser_assist.py` (extended) | Checkpoint recording, resume-freshness pre-check wired into session start |
| `app/applications/doctor.py` (extended) | 8 new Phase 13 integrity checks |
| `app/applications/metrics.py` (extended) | `collect_phase13()` |
| `app/applications/cli.py` (extended) | `provider-health`, `canary`, `job-identity` |
| Dashboard | New `/applications/provider-health` page + 3 new JSON API endpoints |
| `scripts/phase13_live_validation.py` (new) | Bounded, read-only, real-ATS canary validation across 6 providers |

See `docs/application-job-identity.md`, `docs/provider-assist-health.md`,
`docs/ats-canary-validation.md`, `docs/application-checkpoints.md`,
`docs/confirmation-evidence.md` for the deep dives.

## A real bug this phase's own live validation caught

`scripts/phase13_live_validation.py`'s very first run against Greenhouse, Lever, Ashby, and
Workable's **current** real postings reported `captcha_detected=True` on all four — a real,
live-caught false positive, not a hypothetical. Investigation (see
`docs/ats-canary-validation.md` for the full writeup) found these providers now defensively load
a reCAPTCHA v3 script tag on every page visit (invisible verification, no widget shown), and the
old CAPTCHA heuristic's bare `"captcha" in content_lower` substring check across the whole page
matched that script's own URL text. Fixed by narrowing to the three DOM-element-based checks
alone, verified against the real end-to-end fixture (still catches it, since `"recaptcha"`
contains `"captcha"`) and against a second live run (Greenhouse and Workable now correctly reach
their real forms; Lever's genuinely visible hCaptcha widget still correctly pauses).

## Honest real-ATS findings this phase (bounded live validation, no submission)

| Provider | Posting | Result |
|---|---|---|
| Greenhouse | GitLab (`job-boards.greenhouse.io/gitlab`) | Form + upload control + final-submit control found, no CAPTCHA/login |
| Workable | Flosum (`apply.workable.com/j/...`) | Form + upload control + final-submit control found, no CAPTCHA/login |
| SmartRecruiters | SmartRecruiters' own careers board | Form found, no CAPTCHA — a genuinely *different* posting shape than the `oneclick-ui` DataDome challenge Phase 12 found, confirming "do not assume every SmartRecruiters posting behaves identically" |
| Lever | Lever's own demo (`leverdemo`) | Genuine, visible hCaptcha widget on the real apply page (`class="h-captcha"`, confirmed `visible=True`) — a true positive, correctly paused, never bypassed |
| Ashby | Ashby's own careers board | reCAPTCHA v3 telemetry elements present (badge/logo/error divs, hidden response textarea) — ambiguous under a simple visibility heuristic; conservatively still pauses. See "Honest limitations" below |
| Workday | Walmart (`walmart.wd504.myworkdayjobs.com`) | Two bounded repeated observations disagreed (`apply_entry_found` true then false) — classified `VARIABLE`, consistent with Phase 12's own finding for this tenant |

## Job identity gate

`app.applications.job_identity.verify_job_identity_full()` compares company (suffix-normalized),
title (via `title_normalization`, never bare similarity), requisition id, tenant/site, and a weak,
corroborating-only `location` signal — never provider name (both sides would trivially be the same
in-process value, not independent evidence). Verdicts: `VERIFIED`/`PROBABLE`/`AMBIGUOUS`/
`MISMATCH`/`INSUFFICIENT`.

**Acceptance-corrected gate**: only `VERIFIED` (2+ independent corroborating signals, or a
matching requisition id) may continue unattended past a resume-upload field or
`READY_FOR_FINAL_SUBMIT`, by default. `PROBABLE` (exactly one signal matched), `AMBIGUOUS` (only
the weak `location` signal matched), and `INSUFFICIENT` (nothing comparable at all) all pause
`PAUSED_JOB_IDENTITY_UNVERIFIED`. A confirmed `MISMATCH` always pauses
`PAUSED_JOB_IDENTITY_MISMATCH`, unconditionally -- never configurable, unlike the other three
verdicts (`APPLICATION_IDENTITY_MIN_CONFIDENCE`, default `VERIFIED`, lets an operator explicitly
accept weaker corroboration as a deliberate, documented risk decision). Live-verified end-to-end
against a real Chromium session for all five verdicts (`tests/test_browser_assist_phase13_e2e.py`).

This was a genuine correction made during this phase's own review: an earlier version of this
gate treated PROBABLE/AMBIGUOUS/INSUFFICIENT as non-blocking ("recorded as evidence but never
blocks by itself"), which was too permissive against the actual CLAUDE.md Phase 13 acceptance
criteria (PROBABLE requires review; AMBIGUOUS/MISMATCH/INSUFFICIENT must all block). Fixed by
introducing a distinct `PAUSED_JOB_IDENTITY_UNVERIFIED` status/`JOB_IDENTITY_UNVERIFIED` pause
reason for the three non-MISMATCH, non-VERIFIED verdicts, and re-auditing every path that opens a
real browser session (`browser_assist.start_session`/`resume_session` are the ONLY two call sites
of `browser_runtime.open_session()` in the entire codebase -- confirmed by direct grep -- so the
fix is centralized and applies uniformly across browser assist, worker/scheduler-triggered
preparation, and every dashboard action; none of them bypass it).

## Safety boundaries (unchanged, reaffirmed)

- No stealth, no fingerprint spoofing, no CAPTCHA solving, no proxy rotation, no anti-bot bypass,
  no hidden/automated login, no MFA interception — Lever's real hCaptcha widget and Ashby's
  reCAPTCHA telemetry were both left alone.
- `app.applications.canary` never fills PII, never uploads a resume, never clicks a final submit
  — verified directly in `tests/test_canary.py` (field-emptiness check, no-error-implies-no-click
  reasoning) and by static code inspection (no fill/upload/submit code path exists in the module).
- `browser_runtime` still never has a function that clicks a final submit/apply action —
  `_check_no_browser_auto_submit_capability` continues to statically scan the module's public API.
- `mock_ats` remains the only `submission_supported=True` provider. Every real ATS provider stays
  `ASSIST_ONLY`. **No real production application was submitted during this phase's development or
  validation.**

## Test results

- Default `pytest` (offline, no browser/network): 1018 passed (up from the Phase 12 baseline of
  925 under the same `-m "not postgres and not browser"` filter — the ~93 new tests span title
  normalization, the multi-signal job-identity gate (all five verdicts, plus the
  `meets_min_confidence` acceptance-correction logic), provider health, confirmation-evidence
  grading, checkpoints, resume integrity, the canary module, and the new Phase 13 doctor/metrics
  checks). One unrelated test (`test_reprocessing_a_claimed_but_crashed_row_after_lease_expiry_is_safe`,
  in the Phase 6 registry-acquisition module) was observed to fail once under full-suite load and
  passed cleanly both standalone and on a full-suite re-run -- a pre-existing timing-sensitive
  test, not a Phase 13 regression.
- `pytest -m browser` (real Chromium, launched via the documented non-root library workaround):
  57 passed (38 Phase 10-12 + 19 new Phase 13 — 7 canary + 12 job-identity/health/checkpoint/
  confirmation E2E, covering all five `JobIdentityVerdict` values end-to-end against a real
  browser), 0 failed.
- `pytest -m postgres` (embedded `pgserver`): 40 passed (35 Phase 1-12 + 5 new Phase 13 schema
  round-trip tests), 0 failed — no regression from the new additive schema.
- `python -m app.applications.cli doctor`: 0 serious, 0 warnings on the real dev database after
  this phase's changes (migrations 33-38 applied cleanly).

## Provider capability matrix (unchanged support levels; new evidence)

Real auto-submit providers: **none** except the `mock_ats` fixture. Real ASSIST_ONLY providers
with genuine, live-verified form-discovery evidence this phase: Greenhouse, Workable,
SmartRecruiters (on the non-`oneclick-ui` shape). Lever and Ashby remain ASSIST_ONLY with a
genuine CAPTCHA/anti-bot signal observed on their current real postings (not previously recorded
at this granularity). Workday remains explicitly `VARIABLE`-per-tenant, never a blanket claim.

## Honest limitations

- Distinguishing an invisible reCAPTCHA v3 badge/telemetry element from a genuinely blocking,
  interactive challenge reliably would require deeper visibility/size heuristics than a simple
  DOM-element-presence check can offer — Ashby's page currently still pauses conservatively on
  this ambiguity. This is the correct fail-safe direction (never a bypass risk) but is
  acknowledged as an over-cautious false-positive-prone edge, deferred to Phase 14 as a targeted
  follow-up requiring more real samples to avoid overfitting to today's specific DOM shape.
- `FILE_READY` is not currently recorded as a distinct checkpoint from `FIELDS_PREPARED` — see
  `docs/application-checkpoints.md`.
- No provider-specific POST-submission confirmation pattern was added this phase — no real
  provider's genuine confirmation page text has actually been observed yet (this project never
  clicks the final-submit control that would reach one). See `docs/confirmation-evidence.md`.
- BambooHR/Breezy/Recruitee/Comeet were not re-audited this phase beyond their existing
  classification in `docs/application-provider-capabilities.md` — no new evidence, no changed
  claim.

## Recommended Phase 14

- A dedicated, careful pass at distinguishing invisible reCAPTCHA v3 telemetry from a genuinely
  blocking challenge (Ashby's ambiguous case above), backed by more real samples.
- A larger, still-bounded sample of SmartRecruiters postings to characterize what fraction
  present the `oneclick-ui` DataDome challenge vs. a plain reachable form (this phase found one of
  each, confirming variability but not yet its prevalence).
- Unified dashboard/resume optimization (the originally-scoped Phase 14 focus per the Phase 12
  build brief) — this phase's new provider-health/job-identity/checkpoint data is exposed via API
  and one new HTML page, but a unified operations view combining all of it (sessions, health,
  identity mismatches, canary results, checkpoints) in one place remains future work.
