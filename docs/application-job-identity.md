# Application Job Identity (Phase 13)

CLAUDE.md Phase 13 sections 4-10, 72.

## Two layers, kept deliberately separate

1. **`app.applications.job_identity.verify_job_identity()`** (Phase 12,
   unchanged) — a single-signal, URL-requisition-token comparison, wired
   into `browser_runtime._do_discover()`'s existing per-navigation
   `MISMATCH` pause. Kept exactly as it was so no already-tested SPA/apply-
   entry flow regresses.
2. **`app.applications.job_identity.verify_job_identity_full()`** (new this
   phase) — the formal, multi-signal `JobIdentityVerification` CLAUDE.md
   section 4 asks for. Compares every signal available on **both** sides:
   company, title (via `app.applications.title_normalization`, never bare
   similarity), requisition id (URL-extracted or explicitly parsed),
   tenant/site (Workday only). Called at the two highest-stakes moments:
   immediately before a resume-upload field would be filled, and
   immediately before `READY_FOR_FINAL_SUBMIT`.

## Verdicts

`JobIdentityVerdict`: `VERIFIED`, `PROBABLE`, `AMBIGUOUS`, `MISMATCH`,
`INSUFFICIENT`.

**Only `VERIFIED` may continue unattended past the pre-upload/pre-final-
submit gate. Every other verdict pauses the session** —
`meets_min_confidence()` is the single function that decides this, and its
default floor is `VERIFIED`:

- **VERIFIED**: a matching requisition id alone, OR two or more
  independently-corroborating STRONG signals (company/title/provider/
  tenant/site/requisition_id) agree. The only verdict that continues
  unattended by default.
- **PROBABLE**: exactly one STRONG signal was comparable and it matched.
  Pauses `PAUSED_JOB_IDENTITY_UNVERIFIED` by default — an operator may
  explicitly lower `APPLICATION_IDENTITY_MIN_CONFIDENCE` to `PROBABLE` to
  accept this as sufficient (a deliberate, documented risk acceptance,
  never the silent default).
- **AMBIGUOUS**: only a WEAK, non-corroborating signal (`location` —
  two genuinely different requisitions commonly share an identical
  location string) matched, with no STRONG signal comparable at all.
  Some very weak circumstantial evidence, never enough to be PROBABLE.
  Pauses `PAUSED_JOB_IDENTITY_UNVERIFIED`.
- **MISMATCH**: at least one STRONG comparable signal disagrees. A
  CONFIRMED contradiction — pauses `PAUSED_JOB_IDENTITY_MISMATCH`
  unconditionally, never configurable (unlike the three verdicts above,
  this is never affected by `APPLICATION_IDENTITY_MIN_CONFIDENCE`). A
  `location` mismatch alone is never treated as a contradiction (never
  counted toward MISMATCH at all) — a posting can legitimately be listed
  under more than one location string.
- **INSUFFICIENT**: no signal was comparable on both sides at all (e.g. a
  page with no JSON-LD and a URL carrying no requisition token). Never
  treated as a match or a mismatch, but never treated as safe to continue
  unattended either — pauses `PAUSED_JOB_IDENTITY_UNVERIFIED`.

This is deliberately conservative, matching every other identity/redirect
check in this project: a verdict is never inflated from silence, a
`MISMATCH` is never produced from ambiguity alone, and — the Phase 13
acceptance correction — nothing short of genuine, corroborated confidence
is ever treated as safe enough to skip human review before an upload or
final submit.

### Distinct pause reasons, same "must stop" outcome

`MISMATCH` and the other three non-VERIFIED verdicts are recorded as
DISTINCT `BrowserPauseReason`/`BrowserSessionStatus` values
(`JOB_IDENTITY_MISMATCH`/`PAUSED_JOB_IDENTITY_MISMATCH` vs.
`JOB_IDENTITY_UNVERIFIED`/`PAUSED_JOB_IDENTITY_UNVERIFIED`) so a human,
the doctor, or the dashboard can tell "this looks like the wrong job"
apart from "we simply could not confirm this is the right job" — but
neither status is ever treated as safe to continue past.

## Where "observed" signals come from

`app.applications.browser_runtime._extract_observed_job_meta()` reads a
`<script type="application/ld+json">` block for a schema.org `JobPosting`
entry (`title`, `hiringOrganization.name`, `identifier`) — the same
standard, publicly-documented mechanism search engines use. This is a
single, bounded `page.evaluate()` call doing one `JSON.parse` of
already-embedded page data — never arbitrary JS execution, and never a
guess when no such block exists (returns `{}`).

Company-name comparison normalizes common suffixes (`Inc`, `LLC`, `Corp`,
etc.) via `_norm_company()` before comparing, so `"Acme Corp, Inc."` and
`"acme corp"` still match.

## Title normalization (`app.applications.title_normalization`)

`normalize_title()` splits a title into a sorted, order-independent
**base role** and a set of **seniority/level markers** (`senior`, `staff`,
`ii`/`2`, etc). Two titles are `titles_equivalent()` only when BOTH the
base role and the marker set match exactly:

| A | B | Equivalent? |
|---|---|---|
| Senior Software Engineer | Software Engineer, Senior | yes |
| Software Engineer | Software Engineer II | **no** — different level |
| Backend Software Engineer | Software Engineer | **no** — different role |
| Platform Engineer | Engineer Platform | yes — word order only |

Title similarity is **never** used as identity proof by itself — it is
one signal among several `verify_job_identity_full()` requires to agree.

## Evidence persistence

`job_identity_verifications` (append-only, one row per check) via
`record_verification()`/`list_verifications()`. Columns are all
already-public job-posting metadata — no candidate PII. See
`app/migrations.py::_m033_job_identity_verifications_table`.

## Doctor coverage

- `identity_mismatch_but_session_active` — a recorded `MISMATCH` must never
  coexist with a still-active, non-paused session for the same job.
- `_check_job_identity_mismatch_unresolved` (Phase 12) — a
  `PAUSED_JOB_IDENTITY_MISMATCH` session must always have
  `needs_user_action=1`.

## Config

- `APPLICATION_IDENTITY_REQUIRED` (default `true`) — gates whether the
  full check runs at all before upload/final-submit.
- `APPLICATION_IDENTITY_MIN_CONFIDENCE` (default `VERIFIED`) — the minimum
  `JobIdentityVerdict` that may pass without pausing
  (`app.applications.job_identity.meets_min_confidence`). Only `VERIFIED`
  by default; an operator may explicitly lower it (e.g. to `PROBABLE`) to
  accept weaker corroboration as sufficient — a deliberate, documented risk
  acceptance. `MISMATCH` is never affected by this setting: a confirmed
  contradiction always pauses regardless of configuration.

## Honest limitations

- JSON-LD extraction only works when a provider's page actually embeds a
  `JobPosting` block. Several real providers tested this phase (Greenhouse,
  Lever's demo, Workable) do not — in that case the check honestly reports
  `INSUFFICIENT` rather than fabricating a signal.
- Provider name is never compared as an identity signal — both "stored"
  and "observed" would trivially be the same in-process value (this
  session's own belief about which adapter it is), not independent
  evidence from the page.
