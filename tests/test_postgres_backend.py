"""CLAUDE.md Phase 6 sections 2-5, 22, 51-52: real PostgreSQL integration
tests for the database backend abstraction (app.db / app.db_postgres /
app.migrations). Marked `postgres` -- only run via `pytest -m postgres`,
and automatically skipped if `pgserver` isn't installed (see
tests/conftest.py::postgres_url). Never run as part of the default
`pytest` invocation, and never touches the user's real database."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_backend_detection(pg_db):
    assert pg_db.backend() == "postgres"


def test_init_db_is_idempotent(pg_db):
    pg_db.init_db()
    pg_db.init_db()
    with pg_db.db_session() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()
        assert row["c"] == 0


def test_insert_returns_lastrowid(pg_db):
    with pg_db.db_session() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (title, company, description, first_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Backend Engineer", "Acme", "desc", "2026-01-01T00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        assert cur.lastrowid is not None
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
        assert row["title"] == "Backend Engineer"


def test_upsert_on_conflict_do_update(pg_db):
    with pg_db.db_session() as conn:
        conn.execute(
            "INSERT INTO provider_circuit_state (provider, state, updated_at) VALUES (?, 'CLOSED', ?) "
            "ON CONFLICT(provider) DO NOTHING",
            ("greenhouse", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO provider_circuit_state (provider, state, updated_at) VALUES (?, 'CLOSED', ?) "
            "ON CONFLICT(provider) DO NOTHING",
            ("greenhouse", "2026-01-01T01:00:00"),
        )
        rows = conn.execute("SELECT * FROM provider_circuit_state WHERE provider = ?", ("greenhouse",)).fetchall()
        assert len(rows) == 1
        assert rows[0]["updated_at"] == "2026-01-01T00:00:00"


def test_rowcount_reflects_matched_rows(pg_db):
    with pg_db.db_session() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (title, company, description, first_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Engineer", "Acme", "desc", "2026-01-01T00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        job_id = cur.lastrowid
        matched = conn.execute("UPDATE jobs SET title = ? WHERE id = ? AND title = ?", ("New Title", job_id, "Engineer"))
        assert matched.rowcount == 1
        unmatched = conn.execute("UPDATE jobs SET title = ? WHERE id = ? AND title = ?", ("X", job_id, "WrongTitle"))
        assert unmatched.rowcount == 0


def test_migrations_recorded_and_idempotent(pg_db):
    from app import migrations

    with pg_db.db_session() as conn:
        version_before = migrations.current_db_version(conn)
        assert version_before == migrations.CURRENT_SCHEMA_VERSION
        assert migrations.is_compatible(conn)
    pg_db.init_db()
    with pg_db.db_session() as conn:
        assert migrations.current_db_version(conn) == version_before


def test_like_pattern_with_literal_percent_is_not_mistaken_for_a_placeholder(pg_db):
    """Real bug this integration QA pass caught live: app/pipeline_dashboard.py
    (and app/agent/doctor.py, app/applications/doctor.py,
    app/applications/metrics.py) run `?`-placeholder queries containing a
    literal `%` in a LIKE pattern (e.g. `LIKE 'SKIPPED%'`) -- this codebase's
    own `?`-only paramstyle convention means that `%` is never a placeholder,
    but psycopg's client-side binding tried to parse it as one anyway,
    raising `psycopg.ProgrammingError: only '%s', '%b', '%t' are allowed as
    placeholders` and 500-ing the Dashboard/Applications pages under
    Postgres. app.db_postgres._translate_paramstyle() must escape a literal
    `%` to `%%` before translating `?` -> `%s` so this never regresses."""
    with pg_db.db_session() as conn:
        conn.execute(
            "INSERT INTO jobs (title, company, description, application_state, first_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Engineer", "Acme", "desc", "SKIPPED_NO_SPONSORSHIP",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state LIKE 'SKIPPED%' AND is_test_fixture = 0"
        ).fetchone()
        assert row["c"] == 1
        # A mixed query -- real bound `?` params alongside a literal `%` LIKE
        # pattern in the same statement -- must also translate correctly.
        row2 = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE application_state LIKE 'SKIPPED%' AND company = ?",
            ("Acme",),
        ).fetchone()
        assert row2["c"] == 1


def test_partial_unique_index_enforced(pg_db):
    """Two portals with the same (provider, tenant_identifier) must
    conflict -- the same partial-unique-index semantics SQLite already
    relies on for registry_portals dedup."""
    import psycopg

    with pg_db.db_session() as conn:
        conn.execute(
            "INSERT INTO registry_companies (normalized_name, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("acme", "Acme", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
    with pg_db.db_session() as conn:
        company_id = conn.execute("SELECT id FROM registry_companies WHERE normalized_name = ?", ("acme",)).fetchone()["id"]
        conn.execute(
            "INSERT INTO registry_portals (company_id, provider, tenant_identifier, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (company_id, "greenhouse", "acme", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with pg_db.db_session() as conn:
            conn.execute(
                "INSERT INTO registry_portals (company_id, provider, tenant_identifier, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (company_id, "greenhouse", "acme", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
