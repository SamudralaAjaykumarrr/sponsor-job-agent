# PostgreSQL Backend

## Selecting the backend

`DATABASE_URL` (env var, read by `app/db.py`) selects the backend:

- Unset, empty, or `sqlite:///...` → SQLite, exactly as in Phase 1-5. The
  actual file path is controlled by `app.config.DB_PATH` regardless of
  whatever path string appears in `DATABASE_URL` -- this preserves the
  existing test/monkeypatch contract (`tests/conftest.py` patches
  `db.DB_PATH` directly) unchanged.
- `postgresql://user:password@host:port/dbname` (or `postgres://...`) →
  PostgreSQL, via `app/db_postgres.py`.

`app.db.backend()` returns `"sqlite"` or `"postgres"`, re-derived from
`app.db.DATABASE_URL` on every call (so tests can monkeypatch it the same
way `DB_PATH` is already monkeypatched).

## Design: a thin wrapper, not SQLAlchemy

The codebase has ~20 modules with hand-written `?`-placeholder SQL against
`sqlite3.Row`-shaped results. Rewriting all of it onto SQLAlchemy Core would
touch every one of those call sites and risk regressing the 423 tests that
already passed before Phase 6. Instead, `app/db_postgres.py` provides:

- **`PGConnection`/`PGCursor`**: wrap a real `psycopg` (v3) connection/
  cursor so they present the exact same surface `sqlite3.Connection`/
  `Cursor` already provide: `.execute(sql, params)` with `?` placeholders
  (mechanically translated to `%s` -- safe because no SQL string anywhere
  in this codebase contains a literal `?` character in string data,
  verified by grep, not assumed), `.executemany()`, `.rowcount`,
  `.fetchone()`/`.fetchall()` returning dict-like rows (`psycopg.rows.
  dict_row`, matching how `sqlite3.Row` is used everywhere -- `row["col"]`,
  `dict(row)` -- no caller anywhere does positional `row[0]` indexing,
  verified by grep), and `.lastrowid` (emulated by appending `RETURNING id`
  to a bare `INSERT` against a table whose primary key is literally named
  `id` -- every table except `workers` (`worker_id`) and
  `provider_circuit_state` (`provider`), neither of which any caller reads
  `.lastrowid` from).
- **Schema translation**: `app.db.SCHEMA` (the SQLite DDL) is the single
  source of truth. `_translate_schema_for_postgres()` mechanically replaces
  `INTEGER PRIMARY KEY AUTOINCREMENT` with `BIGSERIAL PRIMARY KEY` --
  everything else (`TEXT`/`REAL`/`INTEGER` column types, `REFERENCES`,
  partial `CREATE UNIQUE INDEX ... WHERE`, `CREATE INDEX IF NOT EXISTS`,
  `ON CONFLICT (...) DO UPDATE SET ... = excluded.col`) is valid, identical
  DDL/DML in both engines already. This means the two schemas can never
  drift apart -- there's only one schema.
- **Timestamps as ISO-8601 TEXT in both backends**, never a native
  `TIMESTAMP`/`TIMESTAMPTZ` column. Every `utcnow()` helper across the
  codebase already produces `datetime.now(timezone.utc).isoformat()`
  strings, and every reader already parses them back with
  `datetime.fromisoformat()`. Keeping TEXT in both backends means zero
  behavior difference to port and no timezone-adapter edge cases.
- **Boolean flags stay `INTEGER` (0/1)** in both backends, matching how the
  Python code already treats them.

## Schema DDL concurrency (a real bug this phase caught)

Concurrent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF
NOT EXISTS` from multiple processes against a shared Postgres database can
genuinely deadlock, or race into a `UniqueViolation` on a system catalog
entry (a well-documented PostgreSQL limitation for concurrent first-time
DDL, not a bug in this schema). This was caught live during this phase's
own 2-worker (then 6-thread) validation against real Postgres.

Fixed structurally: `app.db_postgres.acquire_schema_lock()`/
`release_schema_lock()` take a Postgres session advisory lock
(`pg_advisory_lock`) around the entire DDL sequence in both
`db_postgres.init_db()` and `app.db.init_db()`'s migration-running path, so
only one process ever runs schema DDL at a time; every other process
blocks (a normal wait, not a deadlock) until it's free, then quickly no-ops
through its own `IF NOT EXISTS` checks. `run_with_deadlock_retry()` is kept
as defense-in-depth. See `tests/test_phase6_postgres_ddl_retry.py` for a
real reproduction (with the fix, 6 concurrent threads calling `init_db()`
against a genuinely fresh database complete cleanly, run repeatedly).

## Leasing: SKIP LOCKED

`app/workers/leasing.py` dispatches to `app/workers/leasing_postgres.py`
when the backend is Postgres. That module uses `SELECT ... FOR UPDATE SKIP
LOCKED` (the idiomatic Postgres pattern) instead of the SQLite path's
per-row `UPDATE ... WHERE (unleased OR expired)` loop -- both are correct
(Postgres's MVCC read-committed re-check means the SQLite-style loop is
ALSO correct there, just less efficient under contention), but SKIP LOCKED
avoids wasted round-trips.

A real efficiency bug here (flat 4x overfetch causing worker starvation,
not just waste) was caught and fixed -- see `docs/phase6-production-scale.md`
and `app/workers/leasing_postgres.py::_select_limit`'s docstring.

## Testing

- `pgserver` (a Python package bundling a real, unmodified PostgreSQL
  binary, run in a temp data directory with no root/Docker/apt access
  needed) backs `tests/conftest.py::postgres_url` (session-scoped) and
  `pg_fresh_db` (a fresh logical database per test). Install via
  `requirements-dev.txt` -- never required for the default `pytest` run.
- Every Postgres-dependent test is marked `@pytest.mark.postgres`.
  `pytest.ini`'s `addopts = -m "not postgres"` excludes them by default;
  run `pytest -m postgres` to include them (this overrides the default
  marker filter).
- `pgserver` was not available as a system package here -- it was
  installed via `pip install pgserver` (works because this build had
  outbound internet access; if a target environment doesn't, install a
  real PostgreSQL server another way and set `DATABASE_URL_TEST_POSTGRES`
  or just point `DATABASE_URL` manually).

## Migrating an existing SQLite database

See `docs/database-migration.md` for `python -m app.db_migrate
sqlite-to-postgres`.
