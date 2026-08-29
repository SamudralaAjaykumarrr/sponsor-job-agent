# Sponsorship Intelligence Coverage V1

Evidence-enrichment build for the sponsorship intelligence layer built in
Phase 7 (`docs/phase7-sponsorship-intelligence.md`). This is **evidence
enrichment, not automatic sponsorship approval** -- no classification rule,
threshold, or terminology from Phase 7 was loosened. See CLAUDE.md's
"Sponsorship Intelligence Rules (recorded after Phase 7)" section, which
this build fully reuses and extends with three new, additive pieces.

## Why this was needed

Before this build, `employer_sponsorship_evidence` had **zero rows**.
Of the 19 real, currently-discovered employers, only two had reached
anything past `UNKNOWN`:

- **Airbnb** -> `LIKELY_SPONSOR`, via the small local `known_h1b_sponsors.json`
  reference list (a hand-maintained ~40-employer illustrative list, not real
  government data).
- **Anthropic** -> `LIKELY_SPONSOR`, via current-JD conditional language
  ("case-by-case") in one specific posting.

Neither came from the real historical-evidence pipeline Phase 7 built
(`app.sponsorship.evidence`/`profile`/`decision`) -- it existed, was fully
tested, but had never actually been fed real data for these employers.

## What was added

Three new, additive pieces, all reusing the existing evidence/identity/
profile/decision architecture unmodified:

1. **A third evidence source** -- `app.sponsorship.public_source_importer`
   (h1bdata.info LCA-aggregator snapshots). See
   `docs/sponsorship-data-import.md` for the full rationale: USCIS's own
   Employer Data Hub has no job-title/occupation field, which caps every
   employer at `historical_strength=SOME` and blocks the
   `UNKNOWN -> LIKELY_SPONSOR` upgrade path entirely (it requires
   `STRONG_RECENT` + role-similarity, both needing occupation-title data).
   DOL's own LCA disclosure files were not reachable over this network
   (403/503 from dol.gov and foreignlaborcert.doleta.gov). Every row from
   this source is recorded at `SECONDARY_REPUTABLE` quality, never
   `PRIMARY_GOVERNMENT` -- a doctor check statically enforces this.
2. **Verified employer identity/alias seeds** --
   `data/sponsorship/employer_identity_seed.json` (registry_companies rows
   for a discovered employer with no identity row yet: Anthropic, Pump.co)
   and `employer_alias_seed.json` (5 verified legal-name/DBA aliases: e.g.
   "Ramp Business Corporation" -> the `ramp` registry company). Every entry
   was verified against real USCIS/h1bdata records during this build, never
   guessed. `app.sponsorship.aliases.seed_known_aliases()` and
   `app.sponsorship.registry_backfill.seed_missing_employer_identities()`
   are the loaders; both are idempotent and skip (never force-apply)
   anything ambiguous.
3. **`app.sponsorship.refresh.refresh_job_sponsorship()`** -- the missing
   "recompute sponsorship for an existing job because EVIDENCE changed"
   path. `app.pipeline.analyze_job()` would also reset `application_state`
   (unsafe for a job already mid-application); `app.pipeline.reanalyze_job()`
   is a no-op unless the JD *text* changed (this feature's whole scenario).
   `refresh_job_sponsorship()` calls the unmodified
   `app.sponsorship.decision.persist_decision()` and writes ONLY the
   sponsorship-related columns -- never `application_state`, priority,
   score, or resume fields. `app.sponsorship.coverage.coverage_snapshot()`
   provides the real, DB-derived before/after numbers below.

## Real government/public data used

- **USCIS H-1B Employer Data Hub, FY2021-2024** (aggregate approval/denial
  counts per employer per fiscal year; no occupation field, per its own
  documented limitation). Sourced from a GitHub mirror
  (github.com/JohnBroberg/H1B_Hub) of USCIS's own downloadable per-year
  files -- verified genuine by its raw UTF-16LE tab-delimited encoding and
  exact column layout, which is USCIS's own export tool's distinctive
  quirk, not something a mirror would introduce. Fetched once via `curl`
  (an operator action, never live-fetched by application code) and
  normalized to the documented importer CSV column layout before import;
  `app/sponsorship/importers.py` itself is unmodified.
- **h1bdata.info LCA-aggregator snapshots** for the 17 employers with a
  matching registry identity, fetched once via `curl` to a local `.html`
  file per employer, imported via the new
  `app.sponsorship.public_source_importer`.
- **GitLab**: genuinely has zero matching records in either source (verified
  directly; the only "GITLAB"-adjacent h1bdata record is GITLAB FOUNDATION,
  a distinct nonprofit, correctly rejected by the anti-contamination guard
  and never attributed to GitLab Inc.). This is an honest gap, not a bug --
  GitLab stays `UNKNOWN`/no-evidence.
- **Pump.co**: no matching records in either source. Registered in the
  identity seed for completeness (it is a real discovered employer) but
  stays unmatched/no-evidence -- also honest, not fabricated.

No occupation/SOC/job-title text was ever invented for a USCIS row (that
source genuinely has none); h1bdata rows carry the real, employer-filed job
title text and nothing beyond it (no SOC code fabricated either).

## Entity matching

Exact-suffix-normalization handled 12 of 19 employers automatically
(`normalize_company_name` already strips Inc/LLC/Corp/etc; "STRIPE INC" ->
"stripe" already matches the registry brand name "Stripe" with zero extra
work). Five needed a verified legal-name alias because the real legal
entity has an extra word `normalize_company_name` correctly leaves alone
(never guessed away, since "markets"/"labs"/"business"/"pbc" are not
generic legal suffixes):

| Registry brand | Real legal entity (verified) |
|---|---|
| Robinhood | Robinhood Markets Inc |
| Instacart | Maplebear Inc (D/B/A Instacart) |
| Ramp | Ramp Business Corporation |
| Notion | Notion Labs Inc |
| Anthropic | Anthropic PBC (D/B/A Anthropic Inc) |

**Anti-contamination guard (real bug caught live during this build):**
searching h1bdata.info for `GITLAB` returns real records for **GITLAB
FOUNDATION**, a distinct nonprofit -- not GitLab Inc, the software company.
`app.sponsorship.public_source_importer` requires an exact expected-employer
match per row (normalized) and rejects anything else into a separate
counter, never silently attaching it to the wrong company. See
`tests/test_public_source_importer.py::test_unrelated_employer_never_inherits_evidence`.

## Coverage: before -> after

Real discovered employers only (fixtures/demo/Acme-Corp excluded), via
`python -m app.sponsorship.cli coverage`:

| Metric | Before | After |
|---|---|---|
| employers_total | 19 | 19 |
| employers_matched_to_evidence | 0 | 17 |
| employers_unmatched | 19 | 2 (Gitlab, Pump.co -- genuinely no records in either source) |
| jobs_total | 452 | 452 |
| jobs_confirmed_sponsor | 0 | 0 (historical evidence never produces CONFIRMED -- unchanged by design) |
| jobs_likely_sponsor | 8 | 176 |
| jobs_unknown | 444 | 276 |
| jobs_no_sponsorship | 0 | 0 |

168 jobs moved from `UNKNOWN` to `LIKELY_SPONSOR` via
`app.sponsorship.refresh.refresh_job_sponsorship()`, using real, newly
imported historical evidence -- job 327 (Airbnb) and job 454 (Anthropic)
were explicitly excluded from this run and remain untouched.

## What this build deliberately did NOT do

- Did not touch `app.sponsorship.classifier` (current-role-only pattern
  matcher) or `app.sponsorship.decision`'s hard invariants (historical
  evidence can only ever move UNKNOWN -> LIKELY_SPONSOR; never CONFIRMED;
  never overrides NO_SPONSORSHIP).
- Did not lower the `STRONG_RECENT` + role-similarity threshold required
  for the historical upgrade path -- some employers may still end this
  build at `SOME` strength (real, honest data, just not enough recency/
  volume/role-match to cross the existing bar).
- Did not retry job 327 (Airbnb) or job 454 (Anthropic) -- both explicitly
  excluded from `refresh-jobs` in this build.
- Did not start any live browser session or real application submission.
