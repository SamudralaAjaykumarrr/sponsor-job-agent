# Phase 7: Sponsorship Intelligence

Phase 7 builds the project's advanced H-1B / employer sponsorship
intelligence layer: a normalized evidence model, government-dataset
importers, deterministic employer identity resolution, a cached historical
profile with recency/role/location weighting, and a decision engine that
combines all of that with the existing current-role classifier -- without
weakening the Phase 1-6 hard gate. See the cross-referenced docs for detail
on each subsystem; this is the map.

## The core rule, restated

> Historical sponsorship evidence helps answer *"is this employer worth
> prioritizing/reviewing?"*. It must **never** answer *"this specific
> current role definitely sponsors."*

Every design decision below traces back to this. See
`docs/sponsorship-decision-engine.md` for the full mechanics.

## What actually changed

| Area | Phase 6 | Phase 7 |
|---|---|---|
| Evidence schema | Minimal storage foundation, unused | Full schema (source type/quality, dataset linkage, occupation/location, idempotency key) |
| Government data | N/A | Streaming/batched/idempotent/resumable USCIS + DOL LCA CSV importers |
| Employer identity | N/A | Deterministic resolution (domain → verified alias → unambiguous name), aliases, parent/subsidiary safety |
| Employer profile | N/A | Cached, recency-weighted, role/occupation/location-aware historical profile + `history_score` |
| Current-role classifier | Positive/negative patterns only | + negation-safety patterns, conditional-language detection, same-JD conflict detection |
| Sponsorship decision | `classify_sponsorship()` only | New `decide_sponsorship()`/`persist_decision()` layer: blends history in (UNKNOWN→LIKELY only), versioned audit trail, JD-change reclassification |
| Pipeline | `analyze_job()` called classifier directly | Calls `persist_decision()`; new `reanalyze_job()` for JD-change detection, safe on terminal states |
| Dashboard | Job list/detail, registry, fleet, acquisition | + company sponsorship pages, review queue, sponsorship doctor, identity review, decision panel on job detail |
| Metrics | Fleet/registry only | + evidence/dataset/decision/conflict/review-queue counts |
| CLI | `app.registry.cli` | + `app.sponsorship.cli` (import, datasets, stats, company, doctor, review-queue) |

## Layer map

```
app/sponsorship/
  schema.py          shared enums + deterministic weight tables
  evidence.py         normalized evidence model + idempotent/batched insert
  datasets.py          dataset versioning
  aliases.py            company aliases
  relationships.py       parent/subsidiary/affiliate/acquired (display only)
  identity.py             deterministic employer identity resolution
  similarity.py             role/occupation/location similarity
  profile.py                 cached employer historical profile
  classifier.py (extended)    current-role-only pattern classifier (unchanged boundary)
  decision.py                  THE integration point: blends history into UNKNOWN only
  review_queue.py               LIKELY_SPONSOR review ordering
  doctor.py                      integrity checks
  metrics.py                      observability counts
  acquisition_integration.py       wires real evidence into Phase 6's acquisition-priority signal
  importers.py                     USCIS + DOL LCA importers
  cli.py                            operational CLI
```

## Preserved sponsorship safety (unchanged, verified)

- `NO_SPONSORSHIP` → hard skip, always. Historical evidence never overrides
  it (`tests/test_sponsorship_decision.py::test_historical_evidence_never_overrides_no_sponsorship`,
  `app/sponsorship/doctor.py::_check_no_sponsorship_contradicted_by_state`).
- `UNKNOWN` → never auto-applied.
- `LIKELY_SPONSOR` → review-only, always (`app/pipeline.py` routes it to
  `REVIEW_REQUIRED`, never `READY_TO_APPLY`).
- `CONFIRMED_SPONSOR` → only ever from current-role evidence
  (`app/sponsorship/doctor.py::_check_confirmed_decision_missing_current_evidence`
  catches any decision that violates this).
- Historical evidence can only ever move `UNKNOWN` → `LIKELY_SPONSOR`, never
  further, never for `NO_SPONSORSHIP`/`CONFIRMED_SPONSOR` jobs.

## Government data ingestion

Two supported, documented formats: the USCIS H-1B Employer Data Hub
(aggregate approval/denial counts, no occupation field) and the DOL OFLC LCA
disclosure data (per-application, with job title/SOC occupation/location).
Both are file-based (no live download -- see
`docs/sponsorship-data-import.md` for why), streaming, batched, idempotent,
resumable, and never load a whole large file into memory.

## Employer identity resolution

Domain match → verified alias match → unambiguous name-only match → else
sent to `employer_identity_review` (ambiguous) or left unresolved (no
match). Never merges on name similarity alone. See
`docs/employer-identity-resolution.md`.

## Historical profile and scoring

Recency buckets (`CURRENT`/`ONE_YEAR`/`TWO_YEARS`/`THREE_TO_FIVE_YEARS`/
`OLDER`, weights 1.0→0.15), source-quality weights (government sources
weighted highest), continuity (fiscal years active in the last 4), trend
(recent 2yr vs. prior 2yr), role/occupation similarity via deterministic
token+SOC-family matching, location similarity via state overlap. Combined
into `history_score` (an auditable relative ranking number, never a
probability) and a `historical_strength` bucket
(`STRONG_RECENT`/`SOME`/`OLD`/`NONE`) used for dashboard filtering and the
decision-engine threshold.

## Decision engine

See `docs/sponsorship-decision-engine.md` for the full rule table, including
the required CLAUDE.md section 43 examples (A-G) and the conflict/negation/
conditional test matrix.

## Testing

12 new test files, 117 new tests, covering: evidence schema/idempotency,
identity resolution (10+ scenarios), aliases/relationships/doctor
contradictions, profile/recency/role/location similarity, the decision
engine's 7 required examples (A-G) plus negation-safety (6 phrases) and
versioning/JD-change scenarios, importers (streaming/batched/idempotent/
resumable/malformed-row-safe), review queue, sponsorship doctor, CLI,
dashboard/API routes, the 8 required end-to-end acceptance scenarios
(section 57), and 4 real-PostgreSQL tests. All pre-existing Phase 1-6 tests
(478) continue passing unmodified.

**Total: 595 tests passing** (571 default `pytest` + 24 `pytest -m postgres`,
up from Phase 6's 498).

## A real bug this phase caught and fixed

`app/sponsorship/evidence.py`'s idempotent-insert existence check
(`WHERE dataset_id = ? AND source_record_id = ?`) was silently doing a
**full table scan** instead of using the partial unique index
`idx_sponsorship_evidence_source_record` (`WHERE source_record_id != ''`) --
SQLite's query planner cannot prove a bound parameter satisfies a partial
index's `!=` condition without the redundant clause spelled out explicitly.
This turned a large import into an accidental O(n²): a 110,000-row import
that should take ~2-3 seconds was measured taking 5+ minutes before the fix
(caught live while running `scripts/sponsorship_benchmark.py` during this
build, not found by unit tests since they only exercise small row counts).
Fixed by adding the explicit `AND source_record_id != ''` to both the
single-row and batched idempotent-insert queries, confirmed via `EXPLAIN
QUERY PLAN` (now `SEARCH ... USING COVERING INDEX`) and re-measured. See
`app/sponsorship/evidence.py`'s inline comment for anyone touching that
query again.

## Synthetic large-import benchmark (`scripts/sponsorship_benchmark.py`)

Isolated temp SQLite DB, 500 synthetic companies, run this build (after the
fix above):

| N (new rows) | Streaming+batched import | Recompute 500 profiles | 100 cached-profile lookups | 50 `decide_sponsorship()` calls |
|---|---|---|---|---|
| 10,000 | 0.23s | 5.03s | 0.065s | 0.133s |
| 100,000 (110,000 cumulative) | 3.07s | 5.65s | 0.070s | 0.124s |
| 500,000 (610,000 cumulative) | 21.23s | 7.73s | 0.061s | 0.123s |

Import scales linearly (as expected, post-fix). Cached-profile lookup and
`decide_sponsorship()` cost stay flat regardless of total evidence-table
size -- both read the cached `employer_sponsorship_profile` row, never
scanning raw evidence on the request path (CLAUDE.md section 52). Profile
recompute (500 companies, all touched by every synthetic batch) grows
slowly with per-company row count, not with total table size.

This proves the storage/query layer holds up at these row counts on one
machine -- it says nothing about a real government dataset's actual size or
quality (see below), and it is never run as part of the normal `pytest`
suite.

## Real data validation

**NOT RUN.** This build environment has no internet access, so a live
USCIS/DOL government dataset could not be downloaded during implementation.
The importers, their exact expected column formats
(`docs/sponsorship-data-import.md`), and idempotent/streaming/resumable
behavior are fully implemented and tested against deterministic fixture
CSVs (`tests/test_sponsorship_importers.py`, 11 tests) matching the real
public formats as documented. If/when a real file is available, running:

```
python -m app.sponsorship.cli import-uscis <real-file>.csv --dataset-version <vX>
python -m app.sponsorship.cli import-dol-lca <real-file>.csv --dataset-version <vX>
python -m app.sponsorship.cli company "<a real employer name>"
python -m app.sponsorship.cli doctor
```

would report exact companies matched/ambiguous/unmatched and technical
occupation matches, honestly, the same way the fixture-based tests already
prove the mechanism works.

## Exact limitations

- **No live download**: government data must be downloaded manually and
  pointed at by file path (deliberate -- see
  `docs/sponsorship-data-import.md`).
- **Real-data validation not run** (no internet access in this build
  environment) -- see above.
- **`historical_strength` dashboard filter matches on registry company
  `display_name` text**, not full identity resolution -- a company
  registered under a different exact display name than a job's `company`
  field won't be matched by this filter (the underlying decision engine's
  identity resolution, used for the actual LIKELY_SPONSOR upgrade, does not
  have this limitation).
- **`acquisition_integration.sync_acquisition_signal` only sets the
  `has_sponsorship_history_signal` boolean** on a registry company; it
  deliberately does not recompute the full `acquisition_priority` score
  (which needs portal-level inputs this module doesn't own) -- a future
  phase should wire a full recompute into the registry sync process.
- **Role similarity is deterministic token/SOC-family matching, not a
  learned model** -- by design (CLAUDE.md explicitly forbids "opaque fake AI
  probability" here), but it will occasionally under- or over-match titles
  that a human would judge differently.
- **`employer_sponsorship_profile` is a per-company cache recomputed
  explicitly** (on import, or on-demand if missing) -- it does not
  auto-invalidate on a schedule; a very stale profile (no new evidence
  imported in a long time) still reflects the data as of its last
  recompute, which is surfaced honestly via `computed_at`.

## Recommended Phase 8

1. **Real government-data validation** once internet access or a supplied
   dataset file is available -- run the exact CLI commands above and report
   real companies-matched/ambiguous/unmatched numbers.
2. **Full acquisition-priority recompute wiring**: extend
   `app.sponsorship.acquisition_integration` (or the registry sync process)
   to recompute the complete `compute_priority()` score (not just the
   sponsorship-signal boolean) whenever a portal's other inputs change.
3. **Employer identity resolution improvements**: fuzzy-but-reviewed
   suggestions (still never auto-merged) to reduce the pending-review
   backlog faster than one-by-one manual resolution at scale.
4. **`historical_strength` dashboard filter via true identity resolution**
   instead of `display_name` text matching, once jobs carry a resolved
   `company_id` link of their own (would also let the job detail page link
   directly to its employer's sponsorship profile).
5. **Company-policy evidence discovery**: a safe, bounded, explicitly-
   triggered fetch of a company's own immigration/careers policy page
   (`OFFICIAL_EMPLOYER_CAREERS_PAGE`/`CURRENT_COMPANY_POLICY` source types
   already exist in the schema; no fetcher was built this phase since
   CLAUDE.md section 25 requires it be "explicitly supplied or safely
   discovered," which needs its own careful scope).
