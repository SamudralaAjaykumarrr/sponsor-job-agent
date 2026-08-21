# Registry Acquisition

How the REAL verified registry grows: seed dataset → company → careers-page
discovery → ATS detection → tenant extraction → verification → VERIFIED/
ACTIVE or QUARANTINED, tracked as a resumable batch. Implementation:
`app/registry/acquisition.py`, building on the unchanged Phase 4
`app/registry/importers.py` (candidate creation) and
`app/registry/verification.py`/`lifecycle.py`/`sync.py` (verification).

## Why a new batch executor, not just the Phase 4 importer

`app.registry.importers.import_candidates()` already does idempotent,
per-row-isolated candidate creation — Phase 5 doesn't reimplement that (it
reuses `process_row`, the same function, via a new public alias). What
Phase 4's importer *didn't* have:

1. **Checkpointed resume.** A `registry_acquisition_batches` row tracks
   `resume_cursor` (the last fully-processed row number), updated every
   `checkpoint_every` (default 25) rows. If the process crashes or is
   interrupted, `run_acquisition_batch(path, resume_batch_id=N)` picks up
   exactly where it left off — rows before the cursor are skipped, not
   reprocessed. (Reprocessing them would also be *safe*, since `process_row`
   is itself idempotent on `(provider, tenant_identifier)`/canonical URL —
   the checkpoint is purely an efficiency optimization, not a correctness
   requirement. Proven both ways in
   `test_acquisition_resume_after_crash_is_idempotent_no_duplicates`.)
2. **Immediate verification.** Each newly-created candidate portal is, by
   default, verified synchronously as part of the same batch
   (`verify_new_candidates=True`) — so a batch's `verified`/`active`/
   `quarantined`/`failed` counts reflect real outcomes by the time it
   completes, rather than staying at zero until some later, separate pass.
   Pass `--no-verify` (CLI) / `verify_new_candidates=False` (API) to skip
   this and leave new candidates for the worker fleet's verification queue
   instead (useful for genuinely large batches where synchronous
   verification would take too long inline).
3. **Progress visibility + resumability as first-class dashboard/CLI
   concepts** (`/acquisition`, `python -m app.registry.cli batches`).

## CLI

```
python -m app.registry.cli acquire seed.csv [--source-name NAME] [--source-type CSV] [--no-verify]
python -m app.registry.cli batches
python -m app.registry.cli resume BATCH_ID [--no-verify]
```

Every source must be attributable — `source_name` is recorded on every
`registry_provenance` row created by the batch (unchanged Phase 4
provenance mechanism). **No scraping of search engines, LinkedIn, or
Indeed is performed anywhere in this pipeline** — inputs are CSV/JSON/JSONL
files the operator supplies (same format Phase 4's importer already
accepted: `company_name`, `provider`, `tenant_identifier`, `careers_url`,
`country`, `source`, ...).

## Real growth exercised in this phase

`data/registry_seed/phase5_growth_seed.csv` — 6 additional real, well-known
companies, sourced the same way Phase 4's original 20-company seed was
(manually curated, publicly known Greenhouse boards), run through the real
acquisition pipeline against the real database with real live verification
(no mocking):

```
$ python -m app.registry.cli acquire data/registry_seed/phase5_growth_seed.csv --source-name phase5_growth_seed
batch 1: COMPLETED
  records_processed: 6/6
  companies_created: 6
  portal_candidates: 6
  verified:          3
  active:            3
  quarantined:       0
  failed:            3
```

**3 of 6 guessed tenant identifiers were simply wrong** (DoorDash, Plaid,
and Retool's board tokens 404'd — a real, honest, unforced outcome, not a
bug) and correctly stayed `CANDIDATE`, never fabricated as active. The other
3 (Dropbox, Affirm, Webflow) were confirmed live and promoted to `ACTIVE`,
then successfully polled for real jobs by the worker fleet in the same
session. `python -m app.registry.cli doctor` reported 0 issues throughout.
This is the acquisition pipeline's evidence-based design working exactly as
intended: a wrong guess never silently becomes a false "verified" claim.

## Batch fields

| Field | Meaning |
|---|---|
| `status` | `PENDING` → `RUNNING` → `COMPLETED` \| `FAILED` (an unhandled exception mid-run; resumable) \| `PAUSED` (reserved for a future manual pause action) |
| `records_total` / `records_processed` | Progress within the source file |
| `companies_created` | New `registry_companies` rows |
| `portal_candidates` | New `registry_portals` rows (company-only rows, and re-observations of an existing portal, don't count here) |
| `verified` / `active` | Portals that reached `VERIFIED`/`ACTIVE` via immediate verification |
| `quarantined` | Portals whose live check found a company-identity mismatch |
| `failed` | Portals whose live check hit a permanent or temporary failure this run (not yet enough to be demoted/quarantined under Phase 4's own threshold) |
| `resume_cursor` | The checkpoint used by `resume` |

## Verification queue vs. acquisition's inline verification

Portals created with `verify_new_candidates=False` (or discovered by
non-acquisition means — page discovery, a plain bulk import, manual
addition) still get verified eventually: the worker fleet's verification
queue (`SQLiteVerificationQueue`, `app/workers/runner.py`) picks up any
`DISCOVERED`/`CANDIDATE` portal on its own schedule, using the identical
leasing/idempotency mechanism as the poll queue. See
`docs/worker-architecture.md` and `docs/polling-leases.md`.
