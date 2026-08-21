# Registry Bulk Import

`app/registry/importers.py` + `app/registry/cli.py`. Turns CSV/JSON/JSONL rows into
`registry_companies` / `registry_portals` / `registry_provenance` records. Every row produces a
**candidate**, never an automatically-trusted VERIFIED/ACTIVE entry — see
`docs/registry-verification.md` for how a candidate gets promoted.

## Commands

```
python -m app.registry.cli import companies.csv [--dry-run] [--batch-size N] [--source-name NAME]
python -m app.registry.cli validate companies.csv        # dry-run + report only, writes nothing
python -m app.registry.cli stats                          # real DB-derived snapshot + per-provider breakdown
python -m app.registry.cli export registry.jsonl [--format jsonl|json]
python -m app.registry.cli doctor                          # integrity checker, see registry-operations.md
python -m app.registry.cli verify [--limit N] [--provider NAME]
```

Every command runs `app.db.init_db()` first (additive/idempotent) against the real
`app.config.DB_PATH`.

## Input formats

CSV (header row required), JSON (either a top-level array, or an object with a
`companies`/`records`/`items` array), and JSONL/NDJSON (one JSON object per line). Column/field
names are matched with a small alias table (`company_name`/`company`/`name`,
`company_domain`/`domain`, `careers_url`/`careers_page`/`url`, `provider`/`ats`/`ats_provider`,
`tenant_identifier`/`tenant`/`board_token`/`slug`, `country`, `source`/`source_name`, `source_url`).

Recognized fields: `company_name` (required), `company_domain`, `careers_url`, `provider`,
`tenant_identifier`, `country`, `source`, `source_url`. All except `company_name` may be absent.

## What happens to a row

1. **Validate.** Missing `company_name`, or a `careers_url` that isn't a well-formed `http(s)://`
   URL, is INVALID — reported in the summary, never silently dropped.
2. **Upsert the company.** Dedup key is `(normalize_company_name(name), normalize_domain(domain))`
   — see `docs/company-registry.md`. No domain → falls back to name-only lookup.
3. **Detect provider/tenant only when the row didn't already supply them**, via the existing
   `app.providers.detector.detect_provider()` against `careers_url`. Never overrides an explicit
   value, never claims more confidence than the detector actually reports. A URL that merely looks
   ATS-shaped but doesn't match a known deterministic pattern gets `tenant_identifier=""` and stays
   `DISCOVERED` — no fabricated tenant.
4. **Dedup the portal**: `(provider, tenant_identifier)` first if both are present, else the
   canonicalized `careers_url` (`app.registry.url_canon.canonicalize_portal_url`). A match updates
   provenance only (re-observed); no new row. This is what makes re-importing the same dataset
   idempotent (acceptance scenario A).
5. **Company-only row** (no provider and no careers_url) creates a `Company` with no portal —
   reported as `rows_skipped`, not an error.
6. **Provenance** is upserted (`source_type="bulk_import"`, keyed on `(portal_id, source_type,
   source_name)`) — a second import from the *same* source updates `observed_at` in place; a
   *different* source (`source` column, or `--source-name`) adds a second, independent provenance
   record on the same portal (acceptance scenario D).
7. A freshly-created portal's `confidence`/`confidence_reasons` are computed immediately via
   `app.registry.quality.score_portal` (see `docs/registry-verification.md`).

## Summary fields

`rows_total`, `rows_created`, `rows_updated`, `rows_skipped`, `rows_invalid`, `companies_created`,
`errors` (one string per invalid/failed row, row-numbered). One bad row never aborts the batch.

## Batching

`--batch-size` (default 500) only bounds how many `RegistryCandidate` objects are buffered from
the input generator at a time — a 100k-row file streams through without ever holding the whole
dataset in memory. Each row's DB writes are their own small transaction
(`app.db.db_session`).

## RegistrySource interface

`app.registry.importers.RegistryCandidate` is the common shape every source produces. Today's
sources are the three file readers (`read_csv`/`read_json`/`read_jsonl`) plus
`app.registry.page_discovery` (safe bounded career-page discovery, produces a candidate when it
finds a confident ATS link on a supplied company's own site — see `docs/registry-operations.md`).
Adding a future source (e.g. an external open dataset) means writing one function that yields
`RegistryCandidate`s and feeding it to `import_candidates()` — no changes to the dedup/upsert
engine itself.
