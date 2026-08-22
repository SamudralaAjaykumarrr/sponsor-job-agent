"""One-time SQLite -> PostgreSQL data migration tool (CLAUDE.md Phase 6
section 6). Copies every operational table's rows across; never touches
candidate_data/ or output/ (resume files) -- only the path *strings* already
stored in jobs.resume_docx_path etc, which is schema data, not the files
themselves.

Usage:
    python -m app.db_migrate sqlite-to-postgres \\
        --sqlite-path data/app.db --postgres-url postgresql://user:pass@host/db \\
        [--dry-run] [--batch-size 500]

Design:
  - Table order respects the only two real FK constraints in the schema
    (registry_portals -> registry_companies, registry_provenance ->
    registry_portals/registry_companies) -- see _TABLE_ORDER.
  - Column list per table is read live from SQLite's own PRAGMA table_info,
    never hand-duplicated, so it can never silently drift from the real
    schema.
  - Idempotent: every insert is `... ON CONFLICT (<pk>) DO NOTHING`, so
    re-running the tool after a partial failure only inserts the rows that
    didn't make it across, never duplicates or overwrites what did.
  - After copying a table with an auto-incrementing `id` primary key, the
    Postgres sequence is advanced past the highest copied id, so the first
    INSERT the running app makes post-cutover doesn't collide with a
    migrated id.
  - Row counts on both sides are compared and reported after every table;
    a mismatch is surfaced, never silently ignored.
  - --dry-run reads and reports counts/compatibility without writing
    anything to Postgres.
  - Never prints the Postgres URL's password.
"""

import argparse
import sqlite3
import sys
from typing import Optional

# (table, id_column_or_None). id_column is the auto-increment PK to advance
# the Postgres sequence past after copying -- None for the two tables whose
# PK isn't an autoincrement id (workers.worker_id, provider_circuit_state.provider).
_TABLE_ORDER: list[tuple[str, Optional[str]]] = [
    ("registry_companies", "id"),
    ("registry_portals", "id"),
    ("registry_provenance", "id"),
    ("registry_portal_health_events", "id"),
    ("registry_migrations", "id"),
    ("registry_import_batches", "id"),
    ("registry_acquisition_batches", "id"),
    ("registry_acquisition_records", "id"),
    ("company_registry", "id"),
    ("jobs", "id"),
    ("job_provenance", "id"),
    ("discovery_cycles", "id"),
    ("discovery_log", "id"),
    ("application_state_history", "id"),
    ("poll_attempts", "id"),
    ("dead_letters", "id"),
    ("provider_schema_drift", "id"),
    ("employer_sponsorship_evidence", "id"),
    ("workers", None),
    ("provider_circuit_state", None),
]


def _redact(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, _, hostpart = rest.partition("@")
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{hostpart}"
    return url


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _pk_column_for_conflict(table: str, id_column: Optional[str]) -> str:
    if id_column:
        return id_column
    return "worker_id" if table == "workers" else "provider"


def migrate_table(
    sqlite_conn: sqlite3.Connection, pg_conn, table: str, id_column: Optional[str],
    *, batch_size: int, dry_run: bool,
) -> dict:
    if not _table_exists(sqlite_conn, table):
        return {"table": table, "skipped": True, "reason": "table not present in source SQLite DB"}

    columns = _columns(sqlite_conn, table)
    col_list = ", ".join(columns)
    placeholders = ", ".join("%s" for _ in columns)
    pk = _pk_column_for_conflict(table, id_column)
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({pk}) DO NOTHING"

    sqlite_conn.row_factory = sqlite3.Row
    total_read = 0
    total_written = 0
    offset = 0
    order_col = id_column or pk
    while True:
        rows = sqlite_conn.execute(
            f"SELECT {col_list} FROM {table} ORDER BY {order_col} LIMIT ? OFFSET ?", (batch_size, offset)
        ).fetchall()
        if not rows:
            break
        total_read += len(rows)
        if not dry_run:
            batch = [tuple(row[c] for c in columns) for row in rows]
            with pg_conn.cursor() as cur:
                cur.executemany(insert_sql, batch)
            pg_conn.commit()
            total_written += len(rows)
        offset += batch_size

    if not dry_run and id_column:
        with pg_conn.cursor() as cur:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                f"COALESCE((SELECT MAX({id_column}) FROM {table}), 1))",
                (table, id_column),
            )
        pg_conn.commit()

    pg_count = None
    if not dry_run:
        with pg_conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            pg_count = cur.fetchone()[0]
    sqlite_count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    return {
        "table": table,
        "skipped": False,
        "dry_run": dry_run,
        "sqlite_row_count": sqlite_count,
        "rows_read": total_read,
        "rows_written": total_written,
        "postgres_row_count_after": pg_count if not dry_run else None,
        "counts_match": (pg_count == sqlite_count) if not dry_run else None,
    }


def run_sqlite_to_postgres(
    sqlite_path: str, postgres_url: str, *, batch_size: int = 500, dry_run: bool = False,
) -> list[dict]:
    import psycopg

    from app import db_postgres, migrations

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    if not dry_run:
        # Ensure the target schema exists/is current (including every
        # Phase-6-and-later migration, not just the Phase 1-5 baseline)
        # before copying any data -- never migrate rows into a database
        # missing tables/columns.
        db_postgres.init_db(postgres_url)
        schema_conn = db_postgres.get_connection(postgres_url)
        try:
            migrations.run_pending(schema_conn, "postgres")
            schema_conn.commit()
        finally:
            schema_conn.close()

    pg_conn = psycopg.connect(postgres_url, autocommit=False)
    results: list[dict] = []
    try:
        for table, id_column in _TABLE_ORDER:
            result = migrate_table(
                sqlite_conn, pg_conn, table, id_column, batch_size=batch_size, dry_run=dry_run,
            )
            results.append(result)
    finally:
        pg_conn.close()
        sqlite_conn.close()
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db_migrate")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sqlite-to-postgres", help="Copy an existing SQLite database into PostgreSQL")
    p.add_argument("--sqlite-path", required=True, help="Path to the source SQLite .db file")
    p.add_argument("--postgres-url", required=True, help="Target postgresql:// DATABASE_URL")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--dry-run", action="store_true", help="Read and report only -- writes nothing to Postgres")

    args = parser.parse_args(argv)

    if args.command == "sqlite-to-postgres":
        print(f"Migrating {args.sqlite_path} -> {_redact(args.postgres_url)} (dry_run={args.dry_run})")
        results = run_sqlite_to_postgres(
            args.sqlite_path, args.postgres_url, batch_size=args.batch_size, dry_run=args.dry_run,
        )
        any_mismatch = False
        for r in results:
            if r.get("skipped"):
                print(f"  {r['table']}: skipped ({r['reason']})")
                continue
            if args.dry_run:
                print(f"  {r['table']}: {r['sqlite_row_count']} row(s) in source (dry-run, nothing written)")
            else:
                status = "OK" if r["counts_match"] else "MISMATCH"
                if not r["counts_match"]:
                    any_mismatch = True
                print(
                    f"  {r['table']}: wrote {r['rows_written']} row(s); "
                    f"source={r['sqlite_row_count']} target={r['postgres_row_count_after']} [{status}]"
                )
        if any_mismatch:
            print("MIGRATION COMPLETED WITH ROW-COUNT MISMATCHES -- see above.", file=sys.stderr)
            return 1
        print("Migration complete." if not args.dry_run else "Dry run complete -- no data written.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
