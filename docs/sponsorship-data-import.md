# Sponsorship Government Data Import

`app/sponsorship/importers.py` + `app/sponsorship/cli.py`. Two supported,
documented source formats. Both importers are streaming, batched,
idempotent, resumable, and provenance-preserving (CLAUDE.md Phase 7
sections 5-7).

## Live download: NOT implemented, by design

This layer only reads **already-downloaded local CSV files**. It never
performs a live network download of a government dataset itself. This is a
deliberate boundary, not a limitation to "fix" later:

- USCIS's public H-1B Employer Data Hub and DOL's OFLC LCA disclosure
  files are published as periodic bulk downloads (not a stable, versioned
  API) -- a live-fetch step would need to track moving download URLs that
  change every quarter/year, which is exactly the kind of "robust file
  importer instead of an unstable live-fetch" tradeoff CLAUDE.md Phase 7
  section 5 explicitly asks for.
- Keeping network I/O out of the importer means every test in
  `tests/test_sponsorship_importers.py` runs with zero internet dependency
  (CLAUDE.md section 56), and an operator stays in control of exactly which
  file/version is being imported (see "Dataset versioning" below).

An operator downloads the file manually (see the two format sections below
for the real government source pages) and runs the CLI against the local
path.

## USCIS H-1B Employer Data Hub

Public dataset: aggregate H-1B petition approval/denial **counts** per
employer per fiscal year. **No job-title or occupation field exists in this
source** -- `occupation_code`/`occupation_title` are always left blank for
these rows; never fabricated.

Expected CSV columns (matching the public download; aliases in parens):

```
Fiscal Year, Employer, Initial Approval, Initial Denial,
Continuing Approval, Continuing Denial, NAICS Code, State, City
```

```
python -m app.sponsorship.cli import-uscis path/to/uscis_employer_data.csv \
    --dataset-version 2024Q4
```

`count_value` = `Initial Approval + Continuing Approval`. Idempotency key:
a fingerprint of `(employer, fiscal_year, state, city)` (this source has no
natural per-row id).

## DOL OFLC LCA Disclosure Data

Public dataset: one row per Labor Condition Application, with job
title/SOC occupation/worksite detail -- the richer per-record source.

Expected CSV columns (matching the public quarterly disclosure download):

```
CASE_NUMBER, EMPLOYER_NAME, JOB_TITLE, SOC_CODE, SOC_TITLE, VISA_CLASS,
WORKSITE_CITY, WORKSITE_STATE, EMPLOYER_CITY, EMPLOYER_STATE,
DECISION_DATE, CASE_STATUS
```

```
python -m app.sponsorship.cli import-dol-lca path/to/lca_disclosure.csv \
    --dataset-version 2024Q4
```

Idempotency key: `CASE_NUMBER` directly (DOL's own stable identifier).
`fiscal_year` is derived by parsing `DECISION_DATE`.

## Import mechanics

- **Streaming**: `csv.DictReader` over the file handle -- the whole file is
  never loaded into memory (`app/sponsorship/importers.py::_stream_csv_rows`).
- **Batched**: rows accumulate in memory only up to `batch_size` (default
  1000) before a single transaction (`bulk_record_evidence_idempotent`)
  inserts the whole batch -- one commit per batch, not per row.
- **Idempotent**: re-running the identical file is a safe no-op (duplicate
  rows are skipped, counted separately in the result as
  `rows_skipped_duplicate`).
- **Resumable**: every `batch_size` rows, `sponsorship_datasets.resume_cursor`
  is checkpointed. `--resume` re-invokes with the same `dataset_id` and skips
  rows before the checkpoint.
- **Malformed-row-safe**: one bad row (missing employer name, unparseable
  date) is recorded in `rows_invalid` / `errors` and never aborts the rest
  of the import.
- **Company identity resolution runs inline**: each row is resolved against
  the registry via `app.sponsorship.identity.resolve_company()` as it's
  imported. An unambiguous match attaches `company_id` immediately; an
  ambiguous match creates an `employer_identity_review` row and leaves
  `company_id` null (never force-matched). See
  `docs/employer-identity-resolution.md`.
- **Profile recompute is a separate, explicit step**
  (`recompute_profiles_for_dataset()`, always run by the CLI after import) --
  it touches only the companies the import actually matched, never the
  whole registry.

## Dataset versioning

Every import call is tied to one `sponsorship_datasets` row via
`get_or_create_dataset(dataset_name, dataset_version, fiscal_year)`. Two
different fiscal years, or two different downloads of the "same" dataset
with different `--dataset-version`, are always tracked as separate dataset
rows -- never silently combined (`tests/test_sponsorship_importers.py::
test_year_reimport_does_not_combine_unrelated_datasets`).

## CLI

```
python -m app.sponsorship.cli import-uscis FILE [--dataset-version V] [--resume]
python -m app.sponsorship.cli import-dol-lca FILE [--dataset-version V] [--resume]
python -m app.sponsorship.cli datasets
python -m app.sponsorship.cli stats
python -m app.sponsorship.cli company "Company Name"
python -m app.sponsorship.cli doctor
python -m app.sponsorship.cli review-queue
```

## Public-web LCA-aggregator snapshots (third source, Sponsorship Intelligence Coverage V1)

`app/sponsorship/public_source_importer.py` + `python -m app.sponsorship.cli
import-public-source`. A THIRD supported source, added because USCIS's
Employer Data Hub has no job-title/occupation field (see above) -- without
per-record occupation data, `app.sponsorship.profile` can never set
`recent_technical=True`, which caps every employer at `historical_strength=
SOME` and blocks the UNKNOWN -> LIKELY_SPONSOR upgrade path entirely. DOL's
own OFLC LCA disclosure files carry the needed detail but were not reachable
over the network available while building this feature (dol.gov and
foreignlaborcert.doleta.gov returned 403/503 to every fetch attempted).

[h1bdata.info](https://h1bdata.info) is a long-running, public,
unauthenticated, robots.txt-open mirror of the same DOL LCA disclosure
records (real per-record job title, worksite, submit/start date, and the
real DOL case number), searchable by exact employer name. Every row imported
from it is recorded at `SourceType.OTHER_REPUTABLE_PUBLIC_SOURCE` /
`SourceQuality.SECONDARY_REPUTABLE` (weight 0.3) -- **never**
`PRIMARY_GOVERNMENT` -- because it is a third-party re-publication, not a
government file downloaded directly. `app.sponsorship.doctor` statically
enforces this (`third_party_source_quality_inflated`).

Same operator-driven, never-live-fetch contract as the two CSV importers: an
operator saves a company's h1bdata.info employer-search results page to a
local `.html` file (e.g. `curl -A "Mozilla/5.0" -L
"https://h1bdata.info/index.php?em=<EXACT+LEGAL+NAME>&job=&city=&year=All+Years"
-o company.html`) and this importer parses that already-downloaded snapshot.

**Anti-contamination guard**: a search on h1bdata.info can return an
unrelated company whose name happens to share a word or substring --
verified live while building this feature: searching `GITLAB` returns real
LCA rows for **GITLAB FOUNDATION**, a distinct nonprofit, not the GitLab
Inc. software company. `--employer` (the exact legal-entity name searched
for) is therefore REQUIRED, and every row is compared against it via
`normalize_company_name` -- any row whose employer text doesn't match is
rejected and counted separately, never silently imported under the wrong
company.

```
python -m app.sponsorship.cli import-public-source company.html --employer "STRIPE INC" [--dataset-version V]
```

## Employer identity/alias seeds

`data/sponsorship/employer_alias_seed.json` and
`data/sponsorship/employer_identity_seed.json` are small, human-curated (not
guessed/fuzzy-matched) lists verified against real government/public LCA
records during the Sponsorship Intelligence Coverage V1 build -- e.g. "Ramp
Business Corporation" (the real legal entity, per USCIS/h1bdata) -> the
`ramp` registry company (the product brand name). Load with:

```
python -m app.sponsorship.cli seed-identities   # registry_companies rows for a discovered employer with none yet
python -m app.sponsorship.cli seed-aliases      # verified legal-name/DBA aliases
python -m app.sponsorship.cli coverage          # before/after coverage metrics
python -m app.sponsorship.cli refresh-jobs      # recompute sponsorship_status from current evidence (never touches application_state)
```

## Large-data safety

`scripts/sponsorship_benchmark.py` measures streaming import, profile
aggregation, and lookup at 10k/100k (and optionally 500k/1M) synthetic rows
in an isolated temp DB -- never the real registry, never claiming a
synthetic run proves anything about real government-data quality. See
`docs/phase7-sponsorship-intelligence.md` for the measured numbers from this
build.

## Real-data validation status

See `docs/phase7-sponsorship-intelligence.md`'s "Real data validation"
section for the exact, honest status of live-dataset validation in this
build.
