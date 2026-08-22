# Backup & Restore (CLAUDE.md Phase 15 section 23)

Documentation only -- this project does not automate backup/restore, and nothing here has
been run destructively against real data as part of writing it.

## SQLite (`LOCAL_DEVELOPMENT` / default)

The entire database is one file, `data/app.db` (plus WAL/journal sidecar files while a
process has it open).

**Backup** (safe, does not require stopping the server -- SQLite's WAL mode allows a
consistent read while writers are active, but the simplest fully-safe approach is still to
briefly stop the app first):

```bash
# Stop the app, then:
cp data/app.db data/app.db.backup-$(date +%Y%m%d-%H%M%S)
# Or, live (via the sqlite3 CLI's own backup command, safe under WAL):
sqlite3 data/app.db ".backup data/app.db.backup-$(date +%Y%m%d-%H%M%S)"
```

**Restore**:

```bash
# Stop the app first.
cp data/app.db.backup-<timestamp> data/app.db
# Remove any stale WAL/journal sidecars so SQLite doesn't try to replay a mismatched log:
rm -f data/app.db-wal data/app.db-shm data/app.db-journal
```

## PostgreSQL (`LOCAL_POSTGRES` / `PRODUCTION`)

Use PostgreSQL's own standard logical backup tools -- this project adds nothing on top of
them, and doesn't need to (schema/data are entirely standard tables, see
`docs/postgres-backend.md`).

**Backup**:

```bash
pg_dump "$DATABASE_URL" -F c -f sponsor_job_agent-$(date +%Y%m%d-%H%M%S).dump
```

**Restore** (to a fresh, empty database -- never run against a database with data you want
to keep without reviewing `pg_restore`'s own `--clean`/`--if-exists` behavior first):

```bash
createdb sponsor_job_agent_restored
pg_restore -d sponsor_job_agent_restored sponsor_job_agent-<timestamp>.dump
```

For a managed PostgreSQL provider (RDS, Cloud SQL, etc.), prefer that provider's own
point-in-time-recovery/snapshot mechanism for production data -- `pg_dump`/`pg_restore`
above are the portable, provider-agnostic fallback.

## What backup/restore does NOT cover

- `candidate_data/profile.json` -- back this up separately yourself (it's a small, private
  JSON file you already control; this project never writes it anywhere else).
- Generated resume artifacts under `output/<job_id>/` -- regenerable from the database +
  candidate profile (`python -m app.resume_optimizer.cli` / the dashboard's
  Generate/Regenerate action) if lost, so not treated as a durability-critical asset here.
- `.env` -- your own configuration; keep your own copy, never commit it.

## Safety notes (CLAUDE.md Phase 15 sections 90-91)

Nothing in this document was executed against the real `data/app.db` while writing it.
Every command above is presented for the operator to run deliberately, not something this
project's own tooling (doctors, acceptance runner, tests) ever invokes automatically.
