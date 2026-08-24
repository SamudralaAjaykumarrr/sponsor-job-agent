"""Postgres backend for app.db (CLAUDE.md Phase 6 sections 2-4). Imported
lazily -- only when DATABASE_URL actually points at Postgres -- so `psycopg`
is never required for the default SQLite-only local install.

Design: rather than adopting SQLAlchemy (which would mean rewriting every
`?`-placeholder raw-SQL call site across app/jobs_repo.py, app/registry/*.py,
app/workers/*.py -- dozens of call sites, all currently well-tested against
SQLite), this module provides a thin connection/cursor wrapper that makes a
psycopg3 connection behave like the sqlite3.Connection the rest of the app
already calls against:

  - `?` positional placeholders are mechanically translated to psycopg's
    `%s` (safe here specifically because no SQL string anywhere in this
    codebase contains a literal `?` character in string data -- verified;
    this is NOT a general-purpose SQL parser).
  - `cursor.lastrowid` is emulated by appending `RETURNING id` to INSERT
    statements against tables whose primary key column is literally named
    `id` (every table except `workers` (worker_id) and
    `provider_circuit_state` (provider), neither of which any caller reads
    lastrowid from -- see _TABLES_WITHOUT_ID_PK below).
  - Rows come back as plain dicts (`psycopg.rows.dict_row`), matching how
    `sqlite3.Row` is used everywhere in this codebase (`row["col"]`,
    `dict(row)`) -- no caller anywhere does positional `row[0]` indexing
    (verified by grep), so this is a safe substitution.
  - `ON CONFLICT (...) DO UPDATE/NOTHING ... excluded.col` and partial
    unique indexes (`WHERE col != ''`) are valid, identical syntax in both
    SQLite and Postgres, so no translation is needed for any of the
    upsert statements already in the codebase.

Timestamps are stored as ISO-8601 TEXT in both backends (never a native
TIMESTAMP/TIMESTAMPTZ column) -- this is a deliberate, durable design choice:
every `utcnow()` helper across the codebase already produces
`datetime.now(timezone.utc).isoformat()` strings, and every reader already
parses them back with `datetime.fromisoformat()`. Keeping TEXT in both
backends means zero behavior difference to port, no timezone-adapter edge
cases, and one one less thing that can silently diverge between SQLite and
Postgres. Boolean flag columns are likewise kept as INTEGER (0/1) in both
backends for the same reason -- the Python code already treats them as ints.
"""

import re
from typing import Any, Optional

from app.db import (
    COMPANY_REGISTRY_ADDITIVE_COLUMNS,
    JOBS_ADDITIVE_COLUMNS,
    REGISTRY_PORTALS_ADDITIVE_COLUMNS,
    SCHEMA,
)

# Tables whose primary key is NOT a column literally named `id` -- INSERT
# statements against these never get an automatic `RETURNING id` appended
# (and no caller anywhere reads .lastrowid after inserting into them --
# verified by grep across app/).
_TABLES_WITHOUT_ID_PK = {
    "workers", "provider_circuit_state", "schema_migrations",
    # CLAUDE.md Phase 9: application_provider_circuit_state mirrors
    # provider_circuit_state's own `provider TEXT PRIMARY KEY` shape (see
    # app/migrations.py::_m022_application_provider_circuit_state_table).
    "application_provider_circuit_state",
    # Premium UI: app_settings has a `key TEXT PRIMARY KEY` shape (see
    # app/migrations.py::_m051_app_settings_table).
    "app_settings",
}

_INSERT_TABLE_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _translate_paramstyle(sql: str) -> str:
    """Translates `?` positional placeholders to psycopg's `%s`. A literal
    `%` in the SQL text itself (e.g. a `LIKE 'SKIPPED%'` pattern -- several
    call sites across app/pipeline_dashboard.py, app/agent/doctor.py,
    app/applications/doctor.py, and app/applications/metrics.py use these)
    is never a placeholder in this codebase's own `?`-only convention, but
    psycopg's client-side binding always tries to parse ANY `%` in the query
    text as the start of one -- doubling it to `%%` first (a real,
    live-reproduced bug this phase's own release-QA pass caught: the
    Applications and Dashboard pages both threw
    `psycopg.ProgrammingError: only '%s', '%b', '%t' are allowed as
    placeholders` under Postgres) tells psycopg it's a literal percent sign,
    exactly like the standard %-style DB-API escaping convention. Must run
    BEFORE the `?` -> `%s` replacement so the newly-inserted `%s` markers are
    never themselves doubled."""
    return sql.replace("%", "%%").replace("?", "%s")


def _maybe_add_returning_id(sql: str) -> tuple[str, bool]:
    """Returns (sql, wants_lastrowid). Appends `RETURNING id` to a bare
    INSERT (one without its own RETURNING/executemany use) against a table
    that has an `id` primary key, so PGCursor can emulate `.lastrowid`."""
    match = _INSERT_TABLE_RE.match(sql)
    if not match:
        return sql, False
    table = match.group(1).lower()
    if table in _TABLES_WITHOUT_ID_PK:
        return sql, False
    if "returning" in sql.lower():
        return sql, False
    return sql.rstrip().rstrip(";") + " RETURNING id", True


class PGCursor:
    """Wraps a real psycopg cursor so it looks like a sqlite3.Cursor to
    callers: `.execute()`/`.executemany()` accept `?` placeholders,
    `.lastrowid` is populated after a single-row INSERT, `.rowcount` and
    `.fetchone()`/`.fetchall()` behave the same."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor
        self.lastrowid: Optional[int] = None

    def execute(self, sql: str, params: Any = ()) -> "PGCursor":
        translated = _translate_paramstyle(sql)
        translated, wants_id = _maybe_add_returning_id(translated)
        self._cur.execute(translated, tuple(params) if params is not None else ())
        self.lastrowid = None
        if wants_id:
            try:
                row = self._cur.fetchone()
                self.lastrowid = row["id"] if row else None
            except Exception:
                self.lastrowid = None
        return self

    def executemany(self, sql: str, seq_of_params) -> "PGCursor":
        translated = _translate_paramstyle(sql)
        self._cur.executemany(translated, [tuple(p) for p in seq_of_params])
        self.lastrowid = None
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def close(self) -> None:
        self._cur.close()


class PGConnection:
    """Wraps a real psycopg connection so app.db.db_session()'s
    `conn.execute(...)` / `conn.commit()` / `conn.close()` usage (the exact
    surface sqlite3.Connection already provides) works unchanged."""

    def __init__(self, raw_connection):
        self._conn = raw_connection

    def execute(self, sql: str, params: Any = ()) -> PGCursor:
        cur = PGCursor(self._conn.cursor())
        return cur.execute(sql, params)

    def executemany(self, sql: str, seq_of_params) -> PGCursor:
        cur = PGCursor(self._conn.cursor())
        return cur.executemany(sql, seq_of_params)

    def executescript(self, script: str) -> None:
        """Splits the DDL script into individual ';'-terminated statements
        and executes each one (one statement per call, so a single bad
        statement's error message points at exactly that statement).
        `--` line comments are stripped first -- app.db.SCHEMA's comment
        prose contains literal semicolons and dashes (e.g. "untouched; a
        VERIFIED/ACTIVE..."), which would otherwise be mistaken for
        statement separators by a naive split."""
        code_only_lines = []
        for line in script.splitlines():
            idx = line.find("--")
            code_only_lines.append(line[:idx] if idx != -1 else line)
        cleaned = "\n".join(code_only_lines)
        for statement in cleaned.split(";"):
            statement = statement.strip()
            if statement:
                self._conn.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def cursor(self):
        return PGCursor(self._conn.cursor())

    @property
    def raw(self):
        return self._conn


def _dsn(database_url: str) -> str:
    # psycopg accepts both "postgres://" and "postgresql://" natively.
    return database_url


def get_connection(database_url: str) -> PGConnection:
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(_dsn(database_url), row_factory=dict_row, autocommit=False)
    return PGConnection(conn)


def _translate_schema_for_postgres(schema_sql: str) -> str:
    """The single source of truth for the schema is app.db.SCHEMA (SQLite
    DDL). The only SQLite-specific construct in it is
    `INTEGER PRIMARY KEY AUTOINCREMENT` -- everything else (TEXT/REAL/
    INTEGER column types, REFERENCES, partial `CREATE UNIQUE INDEX ...
    WHERE`, `CREATE INDEX IF NOT EXISTS`) is valid, identical Postgres DDL
    already. Translating mechanically here (instead of hand-maintaining a
    second full schema file) means the two schemas can never drift apart."""
    return re.sub(
        r"INTEGER PRIMARY KEY AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
        schema_sql,
    )


def _add_columns_if_missing(conn: PGConnection, table: str, columns: list[tuple[str, str]]) -> None:
    for col_name, col_def in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_def}")


def run_with_deadlock_retry(fn, *, max_attempts: int = 5):
    """Retries `fn()` on a transient Postgres DDL contention error --
    DeadlockDetected or LockNotAvailable -- with a short randomized backoff.

    TWO real bugs this phase's own live multi-worker validation caught:

    1. Multiple worker processes each calling init_db()/run_pending() at
       startup against a shared Postgres database can deadlock on
       concurrent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE
       INDEX IF NOT EXISTS` statements against different tables in
       different orders -- Postgres takes an ACCESS EXCLUSIVE lock for
       these even when the column/index already exists and the statement
       is a no-op.
    2. Against a genuinely FRESH database (no tables yet at all), two
       sessions racing to `CREATE TABLE IF NOT EXISTS` for the very first
       time can BOTH pass the "doesn't exist yet" check before either
       commits -- this is a well-known PostgreSQL limitation, not a bug in
       this schema -- and one loses with a UniqueViolation on a system
       catalog entry (observed in practice: the implicit sequence a
       BIGSERIAL column creates, `<table>_id_seq`, racing in pg_class).

    Both are standard, expected PostgreSQL behavior under concurrent DDL,
    and both are transient: Postgres itself already detected/resolved the
    conflict by aborting one side, and simply retrying that side's
    transaction from scratch succeeds (the "loser" now sees the table/
    column/index the "winner" created and skips it via its own IF NOT
    EXISTS check)."""
    import random
    import time

    import psycopg

    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable, psycopg.errors.UniqueViolation) as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            time.sleep(0.1 * (2 ** attempt) + random.uniform(0, 0.1))
    raise last_exc


def init_db_with_retry(database_url: str) -> None:
    run_with_deadlock_retry(lambda: init_db(database_url))


# Fixed, arbitrary 63-bit-safe key for a Postgres session advisory lock
# (pg_advisory_lock) that serializes ALL schema-DDL sequences (init_db()
# here, and app.migrations.run_pending() via acquire_schema_lock/
# release_schema_lock below) across every process sharing this database.
# This is the STRUCTURAL fix for the two real races this phase's own live
# multi-worker validation caught (see run_with_deadlock_retry's docstring)
# -- rather than retrying after a deadlock/unique-violation happens, only
# one session ever runs schema DDL at a time; every other session blocks on
# this lock (a normal wait, not a deadlock) until it's free, then quickly
# no-ops through its own IF NOT EXISTS checks. The retry helper above is
# kept as defense-in-depth for any DDL path that doesn't take this lock
# (e.g. a future direct db_postgres.init_db() caller), not as the primary
# mechanism.
_SCHEMA_LOCK_KEY = 728973265012345


def acquire_schema_lock(conn: PGConnection) -> None:
    conn.execute("SELECT pg_advisory_lock(?)", (_SCHEMA_LOCK_KEY,))


def release_schema_lock(conn: PGConnection) -> None:
    conn.execute("SELECT pg_advisory_unlock(?)", (_SCHEMA_LOCK_KEY,))


def init_db(database_url: str) -> None:
    conn = get_connection(database_url)
    try:
        acquire_schema_lock(conn)
        try:
            translated = _translate_schema_for_postgres(SCHEMA)
            conn.executescript(translated)
            _add_columns_if_missing(conn, "jobs", JOBS_ADDITIVE_COLUMNS)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_provider_external_id "
                "ON jobs (provider, external_job_id) WHERE external_job_id != ''"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedup_fingerprint ON jobs (dedup_fingerprint)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_canonical_url ON jobs (canonical_url)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_first_seen_at ON jobs (first_seen_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_published_at ON jobs (published_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_application_state ON jobs (application_state)")
            conn.execute("UPDATE jobs SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL")

            _add_columns_if_missing(conn, "company_registry", COMPANY_REGISTRY_ADDITIVE_COLUMNS)
            _add_columns_if_missing(conn, "registry_portals", REGISTRY_PORTALS_ADDITIVE_COLUMNS)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_company_registry_lease_expiry ON company_registry (lease_expires_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_registry_portals_verify_lease_expiry ON registry_portals (verify_lease_expires_at)"
            )
            conn.commit()
        finally:
            release_schema_lock(conn)
    finally:
        conn.close()
