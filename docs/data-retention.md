# Data Retention (CLAUDE.md Phase 15 sections 24-25)

## What is persisted (in the local database and `output/`)

| Category | Where | Notes |
|---|---|---|
| Discovered jobs | `jobs` table | title/company/location/description/URL/provider metadata, classification results, state history |
| Registry (company/portal) | `registry_companies`, `registry_portals`, `registry_provenance` | public company/career-portal metadata only |
| Application metadata | `application_executions`, `application_audit_log`, `browser_assist_sessions` | field-mapping confidence, status transitions, timestamps -- never the raw submitted form payload |
| Sponsorship evidence | `employer_sponsorship_evidence`, `employer_sponsorship_profile` | aggregate employer/role/location fields from public government datasets, no beneficiary/worker names |
| Resume metadata | `resume_variants`, `resume_quality_reports`, `resume_evidence_links` | fingerprints, scores, artifact paths -- the actual DOCX/PDF/TXT files live under `output/<job_id>/`, not in the DB |
| Confirmation evidence | `application_executions.confirmation_id/confirmation_url`, `spa_events`, `checkpoints` | text/URL evidence used to justify an `APPLIED` transition |
| Operational metrics | `poll_attempts`, `provider_circuit_state`, `application_provider_circuit_state`, `provider_schema_drift`, `workday_tenant_attempts` | timestamps, status codes, structural signatures -- never raw response bodies |

Candidate private facts (`candidate_data/profile.json`) live only on local disk, are
never written into the SQLite/PostgreSQL database, and are gitignored.

## What is deliberately NOT retained

- **Credentials** of any kind (login username/password) -- browser-assist never captures
  or stores them; a login page always pauses the session for the human.
- **MFA codes** -- same as above.
- **CAPTCHA tokens/solutions** -- CAPTCHA always pauses for the human; nothing is solved
  or stored.
- **Browser secrets / long-lived cookies / saved sessions** -- every browser-assist
  context is `browser.new_context()` (fresh, ephemeral), never
  `launch_persistent_context()`, and `storage_state` is never saved to disk. See
  `docs/browser-assist-sessions.md`.
- **Raw candidate form HTML** -- `app.applications.schema`/`browser_runtime` inspect page
  structure in-memory to map fields; the page's HTML itself is never persisted.
- **Beneficiary/worker names** from government sponsorship datasets -- only
  employer/role/location/aggregate fields are imported (see
  `docs/sponsorship-data-import.md`).
- **Raw provider response payloads** on schema drift -- `provider_schema_drift` stores
  only a structural signature (a hash of the shape-check's descriptive detail string),
  never the response body.

## Structured logs / metrics

`app/observability/logging_config.py::_STRUCTURED_FIELDS` is an explicit allowlist of
correlation fields (job id, execution id, session id, provider name, etc.) -- no field
resembling candidate PII (email/phone/resume content/password/SSN/date-of-birth) may be
added to it. Prometheus metric labels never carry a candidate value (see
`docs/production-observability.md`).

## Retention lifecycle

Nothing in this project auto-deletes rows on a timer today -- data lives until an operator
removes it (see `docs/backup-restore.md` for how to reset a local database entirely, and
this repo's `.gitignore`/`.dockerignore` for what never leaves your machine in the first
place). If you need a formal retention/expiry policy for a specific deployment, add it at
the deployment layer (a scheduled `DELETE ... WHERE created_at < ...` against the specific
tables above) -- this is intentionally not built into the base project, since retention
requirements are deployment/jurisdiction-specific and shouldn't be guessed at here.
