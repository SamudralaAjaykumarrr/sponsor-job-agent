# Sponsorship Evidence Model

Phase 7's normalized schema for employer sponsorship HISTORY. Every row here
describes a company's past filing activity or an employer's general policy
statement -- never a specific current job's confirmation. See
`docs/sponsorship-decision-engine.md` for how (and how little) this data is
allowed to influence a job's `sponsorship_status`.

## `employer_sponsorship_evidence` (extends the Phase 6 table, additive only)

One row per source record (a government filing, a policy-page observation, a
manual verification). Fields (`app/sponsorship/evidence.py::SponsorshipEvidence`):

| Field | Meaning |
|---|---|
| `company_id` | FK to `registry_companies`, nullable until identity resolution matches it |
| `company_name_raw` / `company_normalized_name` / `company_domain` | As the source described it, plus the normalized form used for matching |
| `source_type` | One of the 8 `app.sponsorship.schema.SourceType` values |
| `source_quality` | Derived deterministically from `source_type` (see below) -- never set independently |
| `source_record_id` / `dataset_id` | Idempotency key -- see "Idempotent import" |
| `fiscal_year`, `filing_date`, `petition_type`, `visa_class` | Filing detail, when the source provides it |
| `job_title`, `occupation_code`, `occupation_title` | Role detail (DOL LCA data only -- USCIS's public Employer Data Hub has none, and this is never fabricated) |
| `worksite_city/state`, `employer_city/state` | Location detail |
| `status_outcome`, `count_value` | Outcome / aggregate count, when the source is aggregate (USCIS Employer Data Hub) |
| `confidence`, `raw_source_fingerprint`, `snippet` | Bounded (`snippet` truncated to 500 chars), never a full raw payload |
| `notes` | Free text, e.g. "USCIS Employer Data Hub: aggregate counts, no occupation field" |

**Never stored**: beneficiary/worker names, SSNs, dates of birth, or any
other immigration-filing personal data (CLAUDE.md Phase 7 section 37) --
enforced by the model simply never having those fields
(`tests/test_sponsorship_evidence_schema.py::test_no_beneficiary_pii_fields_exist`).

## Source types and quality tiers

`app/sponsorship/schema.py` defines both, with a fixed, deterministic
mapping (`SOURCE_TYPE_TO_QUALITY`) -- never inferred any other way:

| `SourceType` | `SourceQuality` | Weight |
|---|---|---|
| `USCIS_EMPLOYER_DATA` | `PRIMARY_GOVERNMENT` | 1.0 |
| `DOL_LCA_DATA` | `PRIMARY_GOVERNMENT` | 1.0 |
| `PUBLIC_GOVERNMENT_DATA` | `PRIMARY_GOVERNMENT` | 1.0 |
| `CURRENT_COMPANY_POLICY` | `PRIMARY_EMPLOYER_POLICY` | 0.9 |
| `OFFICIAL_EMPLOYER_CAREERS_PAGE` | `PRIMARY_EMPLOYER_POLICY` | 0.9 |
| `MANUAL_VERIFIED_EVIDENCE` | `MANUAL_VERIFIED` | 0.8 |
| `CURRENT_JOB_DESCRIPTION` | `PRIMARY_CURRENT_ROLE` | 0.8 |
| `OTHER_REPUTABLE_PUBLIC_SOURCE` | `SECONDARY_REPUTABLE` | 0.3 |
| (anything else / unset) | `UNVERIFIED` | 0.1 |

These weights feed `app.sponsorship.profile`'s `history_score` -- a relative
ranking signal, never a probability (CLAUDE.md Phase 7 section 4/15).

## Idempotent import

`record_evidence_idempotent()` / `bulk_record_evidence_idempotent()` key on
`(dataset_id, source_record_id)` -- a unique partial index
(`idx_sponsorship_evidence_source_record`, `WHERE source_record_id != ''`)
enforces this at the DB layer too. Re-running the identical import is always
a safe no-op. See `docs/sponsorship-data-import.md`.

## Dataset versioning (`sponsorship_datasets`)

Every import is scoped to one `sponsorship_datasets` row (`dataset_name`,
`dataset_version`, `fiscal_year`, `source_url`, `checksum`, `record_count`,
`status`, `resume_cursor`). Two different fiscal years or two different
versions of the same source are never silently combined into one dataset id
-- `get_or_create_dataset()` keys on `(dataset_name, dataset_version,
fiscal_year)`.

## Company aliases and relationships

`company_aliases` (`app/sponsorship/aliases.py`) lets evidence using a
different legal/DBA/brand/former name resolve to the same registry company,
always as an explicit stored row (never inferred from string similarity
alone). `company_relationships` (`app/sponsorship/relationships.py`) records
parent/subsidiary/affiliate/acquired links for display and doctor
contradiction-checking -- **evidence is never aggregated across a
relationship**; `app.sponsorship.profile` always scopes strictly to one
`company_id`. See `docs/employer-identity-resolution.md`.

## Derived employer profile (`employer_sponsorship_profile`)

One cached, recomputed-on-import row per company
(`app/sponsorship/profile.py`). Never queried live against raw evidence on
a job-classification request path (CLAUDE.md Phase 7 section 52) --
`get_or_compute_profile()` reads the cache, computing it once if missing.

## Decision audit (`sponsorship_decisions`)

Append-only, versioned per job (`app/sponsorship/decision.py`). See
`docs/sponsorship-decision-engine.md`.

## Identity review (`employer_identity_review`)

Ambiguous employer matches (same normalized name, different domains, no
alias) land here instead of being auto-merged. See
`docs/employer-identity-resolution.md`.
