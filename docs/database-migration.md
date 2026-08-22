# Database Migration

Two distinct things are both called "migration" here -- keep them separate:

1. **Schema versioning** (`app/migrations.py`): how the live schema itself
   is kept up to date, on either backend, as the code evolves.
2. **Data migration** (`app/db_migrate.py`): a one-time tool to copy an
   existing SQLite database's *rows* into a PostgreSQL database, for
   cutting a real local deployment over from SQLite to Postgres.

## Schema versioning (`app/migrations.py`)

Phase 1-5's schema (`app.db.SCHEMA` + the additive-column helpers already
in `app/db.py`) is fully idempotent (`CREATE TABLE IF NOT EXISTS` / manual
column-existence checks) and stays exactly as it was -- recorded as the
implicit baseline, version 1, applied directly by `app.db.init_sqlite_db()`/
`app.db_postgres.init_db()` before the migration framework runs at all.

Every Phase 6 (and future) schema change is a new entry in
`app.migrations.MIGRATIONS`: `(version: int, name: str, fn: Callable)`,
applied strictly in ascending order. A `schema_migrations` table (same DDL
on both backends) records which versions have been applied; `init_db()`
calls `migrations.run_pending()` every startup, which is always a safe
no-op on an already-migrated database.

- `migrations.CURRENT_SCHEMA_VERSION`: the highest version this code knows
  about.
- `migrations.current_db_version(conn)`: the highest version actually
  recorded in the live database.
- `migrations.is_compatible(conn)`: `current_db_version >=
  CURRENT_SCHEMA_VERSION`. `app.workers.runner.Worker._check_schema_compatibility()`
  calls this at worker startup and **refuses to start** (raises) if the
  database is behind what this worker's code expects (querying a column
  that doesn't exist yet would corrupt nothing, but would crash
  immediately and confusingly) -- and only *warns* (proceeds) if the
  database is ahead (an older worker in a mixed-version rolling deployment;
  every migration so far is additive, so this is safe).
- `/readiness` also reports `schema_compatible` for external health checks.

No rollback mechanism is implemented or claimed -- every migration so far is
additive (new table / new nullable-or-defaulted column), so a `down()` step
was never needed or built. This is stated plainly rather than faking one.

On Postgres, the whole DDL sequence for both the baseline schema and every
migration is wrapped in a session advisory lock
(`app.db_postgres.acquire_schema_lock`/`release_schema_lock`) so concurrent
processes never race on schema DDL -- see `docs/postgres-backend.md`.

## Data migration (`app/db_migrate.py`)

```
python -m app.db_migrate sqlite-to-postgres \
    --sqlite-path data/app.db \
    --postgres-url postgresql://user:password@host:port/dbname \
    [--dry-run] [--batch-size 500]
```

- Copies every operational table (jobs, application state history,
  discovery cycles, registry companies/portals/provenance/health,
  migrations, import/acquisition batches, workers, poll attempts,
  dead-letters, provider circuit state, schema drift, sponsorship
  evidence) -- **never** `candidate_data/` or generated resume files; only
  the path *strings* already stored in `jobs.resume_docx_path` etc, which
  is schema data, not file bytes.
- Table order respects the only two real FK constraints in the schema
  (`registry_portals` → `registry_companies`, `registry_provenance` →
  `registry_portals`/`registry_companies`).
- Column list per table is read live from SQLite's own `PRAGMA table_info`
  -- never hand-duplicated, so it can't drift from the real schema.
- Idempotent: every insert is `... ON CONFLICT (<pk>) DO NOTHING`, so
  re-running after a partial failure only inserts the rows that didn't
  make it across, never duplicates or overwrites what did.
- After copying a table with an autoincrement `id` primary key, the
  Postgres sequence is advanced past the highest copied id (`setval(...)`),
  so the first insert the running app makes post-cutover doesn't collide.
- Row counts on both sides are compared and reported after every table; a
  mismatch is surfaced (`MISMATCH` printed, non-zero exit code), never
  silently ignored.
- `--dry-run` reads and reports source row counts without writing anything
  or creating any schema on the target.
- Never prints the target URL's password (redacted before printing).

### Tested

`tests/test_db_migrate.py` (marked `postgres`) exercises dry-run, full
migration + FK ordering, idempotent re-run, and sequence advancement --
against a real temp SQLite file and a real ephemeral Postgres database
(`pgserver`), never the user's real `data/app.db`.

### Not yet built

- No automatic *reverse* migration (Postgres → SQLite). Not needed for a
  one-way production cutover; add if a real use case appears.
- No continuous/live replication -- this is a one-time cutover tool, run
  once while the application is stopped (or accepting that rows written
  during migration on the SQLite side won't be carried over without a
  second run).
