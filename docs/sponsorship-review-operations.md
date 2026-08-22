# Sponsorship Review Operations

Day-to-day operator surfaces for the Phase 7 sponsorship intelligence layer.

## Sponsorship review queue

Dashboard: `/sponsorship/review-queue`. JSON: `GET /api/sponsorship/review-queue`.
CLI: `python -m app.sponsorship.cli review-queue`.

Contains only `LIKELY_SPONSOR` jobs (review-only, never auto-applied --
`app/pipeline.py` never routes `LIKELY_SPONSOR` to `READY_TO_APPLY`).
Ordered by employer historical strength, then technical match score, then
priority score (`app/sponsorship/review_queue.py::build_review_queue`).
Each row shows `missing_confirmation` -- the exact reason the job isn't
`CONFIRMED_SPONSOR` (e.g. "current role lacks explicit sponsorship
confirmation", or the conflict/conditional blocking reason) -- pulled
directly from that job's latest `sponsorship_decisions` row.

## Employer sponsorship company page

Dashboard: `/companies` (list, searchable) → `/companies/{id}` (profile).
JSON: `GET /api/companies/{id}/sponsorship`.

Every page/response is labeled **"HISTORICAL EVIDENCE -- NOT A GUARANTEE
FOR ANY CURRENT ROLE"**. Shows: historical strength, years active, recent/
historical filing and LCA counts, continuity, trend, recent occupation
families/titles, recent states, evidence source coverage, history score +
reasons, aliases, parent/subsidiary relationships. Every claim traces to
provenance (`evidence` list on the page -- fiscal year, source type,
occupation, state, source quality per row) -- never an unexplained "this
company sponsors" statement (CLAUDE.md section 54).

## Job detail sponsorship panel

`/jobs/{id}` now includes a "Sponsorship decision explanation" card:
current status, decision version + classifier version, conflict flag,
blocking reason, the full `reasons[]` list, and (when a job has been
reclassified) the version history. JSON: `GET /api/jobs/{id}/sponsorship`.
Makes it obvious *why* a job is `READY_TO_APPLY` / `REVIEW_REQUIRED` /
`SKIPPED_NO_SPONSORSHIP` without reading code.

## Employer identity review

Dashboard: `/sponsorship/identity-review`. An ambiguous employer match
(same normalized name, more than one registry company, no domain/alias to
disambiguate) is never auto-merged -- it lands here. An operator picks the
correct candidate company (or rejects the match entirely) per pending item.
See `docs/employer-identity-resolution.md`.

## Sponsorship doctor

Dashboard: `/sponsorship/doctor`. CLI: `python -m app.sponsorship.cli doctor`
(nonzero exit on any serious issue -- safe to wire into a pre-deploy check).
Read-only; never auto-repairs. Checks: orphan evidence, invalid fiscal
years, verified alias collisions, parent/subsidiary contradictions,
`CONFIRMED_SPONSOR` decisions with no recorded current-role evidence, a job
whose current `sponsorship_status` is `NO_SPONSORSHIP` sitting in an
apply-eligible `application_state` (the concrete, checkable version of "no
history override" -- see `docs/sponsorship-decision-engine.md`), and a
pending-identity-review backlog (warning only).

## Filters

Dashboard filters (`/?...`): the existing `sponsorship_status`
(`CONFIRMED_SPONSOR`/`LIKELY_SPONSOR`/`UNKNOWN`/`NO_SPONSORSHIP`) filters are
unchanged. A new, additive `historical_strength` filter
(`STRONG_RECENT`/`SOME`/`OLD`/`NONE`) filters by the job's employer's cached
profile -- **never a replacement for `sponsorship_status`**, and never
capable of surfacing a `NO_SPONSORSHIP` job as eligible. Implementation
note: this filter matches on the employer's registry `display_name` text
(not full identity resolution), so it's best-effort for employers not yet
registered under an exact matching name -- a known limitation, see
`docs/phase7-sponsorship-intelligence.md`.

## CLI quick reference

```
python -m app.sponsorship.cli import-uscis FILE [--dataset-version V] [--resume]
python -m app.sponsorship.cli import-dol-lca FILE [--dataset-version V] [--resume]
python -m app.sponsorship.cli datasets
python -m app.sponsorship.cli stats
python -m app.sponsorship.cli company "Company Name"
python -m app.sponsorship.cli doctor
python -m app.sponsorship.cli review-queue
```

## Observability

`/metrics` (Prometheus text) now includes: `sponsorship_evidence_records`,
`sponsorship_datasets_loaded`, `companies_with_recent_h1b_history`,
`sponsorship_decisions_total{status=...}`, `sponsorship_conflicts_total`,
`identity_ambiguous_total`, `sponsorship_review_queue_depth`. No candidate
PII in any of these (counts only). JSON summary:
`GET /api/sponsorship/stats`.
