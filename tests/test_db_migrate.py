"""CLAUDE.md Phase 6 section 6, 51: sqlite-to-postgres data migration tool.
Uses a real temp SQLite file + a real ephemeral Postgres (pgserver) --
never the user's real data/app.db."""

import sqlite3
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.postgres


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def seeded_sqlite_path(tmp_path):
    import app.db as db

    sqlite_path = tmp_path / "source.db"
    original_db_path, original_url = db.DB_PATH, db.DATABASE_URL
    db.DB_PATH = sqlite_path
    db.DATABASE_URL = ""
    try:
        db.init_db()
        now = utcnow()
        with db.db_session() as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO jobs (title, company, description, first_seen_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (f"Engineer {i}", "Acme", "desc", now, now, now),
                )
            conn.execute(
                "INSERT INTO registry_companies (normalized_name, display_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("acme", "Acme", now, now),
            )
            company_id = conn.execute(
                "SELECT id FROM registry_companies WHERE normalized_name = ?", ("acme",)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO registry_portals (company_id, provider, tenant_identifier, created_at, updated_at) "
                "VALUES (?, 'greenhouse', 'acme', ?, ?)",
                (company_id, now, now),
            )
            conn.execute(
                "INSERT INTO workers (worker_id, hostname, pid, started_at, last_heartbeat_at, updated_at) "
                "VALUES ('w1', 'host1', 123, ?, ?, ?)",
                (now, now, now),
            )
    finally:
        db.DB_PATH, db.DATABASE_URL = original_db_path, original_url
    return sqlite_path


def test_dry_run_reports_counts_without_writing(seeded_sqlite_path, pg_fresh_db):
    from app.db_migrate import run_sqlite_to_postgres

    results = run_sqlite_to_postgres(str(seeded_sqlite_path), pg_fresh_db, dry_run=True)
    jobs_result = next(r for r in results if r["table"] == "jobs")
    assert jobs_result["sqlite_row_count"] == 5
    assert jobs_result["rows_written"] == 0

    import psycopg

    with psycopg.connect(pg_fresh_db) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.jobs') AS t"
        ).fetchone()
        assert row[0] is None, "dry-run must not create any schema/tables"


def test_full_migration_copies_all_rows_and_respects_fk_order(seeded_sqlite_path, pg_fresh_db):
    from app.db_migrate import run_sqlite_to_postgres

    results = run_sqlite_to_postgres(str(seeded_sqlite_path), pg_fresh_db, dry_run=False)
    by_table = {r["table"]: r for r in results if not r.get("skipped")}

    assert by_table["jobs"]["rows_written"] == 5
    assert by_table["jobs"]["counts_match"] is True
    assert by_table["registry_companies"]["rows_written"] == 1
    assert by_table["registry_portals"]["rows_written"] == 1
    assert by_table["workers"]["rows_written"] == 1

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(pg_fresh_db, row_factory=dict_row) as conn:
        portal = conn.execute("SELECT * FROM registry_portals").fetchone()
        company = conn.execute("SELECT * FROM registry_companies WHERE id = %s", (portal["company_id"],)).fetchone()
        assert company["display_name"] == "Acme"


def test_migration_is_idempotent_on_rerun(seeded_sqlite_path, pg_fresh_db):
    from app.db_migrate import run_sqlite_to_postgres

    run_sqlite_to_postgres(str(seeded_sqlite_path), pg_fresh_db, dry_run=False)
    results_second = run_sqlite_to_postgres(str(seeded_sqlite_path), pg_fresh_db, dry_run=False)
    jobs_result = next(r for r in results_second if r["table"] == "jobs")
    # Every row already exists (ON CONFLICT DO NOTHING) -- count on the
    # target must still exactly match the source, never doubled.
    assert jobs_result["postgres_row_count_after"] == 5
    assert jobs_result["counts_match"] is True


def test_sequence_advanced_past_migrated_ids(seeded_sqlite_path, pg_fresh_db):
    from app.db_migrate import run_sqlite_to_postgres

    run_sqlite_to_postgres(str(seeded_sqlite_path), pg_fresh_db, dry_run=False)

    import psycopg

    with psycopg.connect(pg_fresh_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (title, company, description, first_seen_at, created_at, updated_at) "
                "VALUES ('New Job', 'NewCo', 'd', %s, %s, %s) RETURNING id",
                (utcnow(), utcnow(), utcnow()),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs WHERE id = %s", (new_id,))
            assert cur.fetchone()[0] == 1
        # The new autogenerated id must not collide with any migrated id.
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            assert cur.fetchone()[0] == 6
