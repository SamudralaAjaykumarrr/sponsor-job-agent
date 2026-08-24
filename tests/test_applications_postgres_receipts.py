"""Provider Post-Approval Execution V1: application_receipts (migration 55)
against REAL PostgreSQL -- clean bootstrap, insert/read round-trip, and
idempotent re-run of migrations. Marked `postgres` -- skipped automatically
if `pgserver` isn't installed (see tests/conftest.py::postgres_url)."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_clean_bootstrap_reaches_schema_version_55(pg_db):
    import app.migrations as migrations

    with pg_db.db_session() as conn:
        assert migrations.current_db_version(conn) >= 55
        assert migrations.is_compatible(conn)


def test_migration_55_idempotent_rerun(pg_db):
    import app.migrations as migrations

    with pg_db.db_session() as conn:
        newly = migrations.run_pending(conn, "postgres")
        assert newly == []  # already applied by db.init_db() -- a second run is a no-op


def test_receipt_insert_and_read_round_trip(pg_db):
    from app.applications import receipts

    row = receipts.record_receipt(
        execution_id="exec_pg_1", job_id=1, provider="mock_ats", submitted_via="headless_provider:mock_ats",
        confirmation_id="conf-pg-1", sanitized_url="https://example.test/confirm", evidence_strength="STRONG",
        raw_message_fingerprint="fp-pg-1", approval_id="appr_pg_1",
    )
    assert row["receipt_id"].startswith("rcpt_")

    fetched = receipts.get_receipt(row["receipt_id"])
    assert fetched["execution_id"] == "exec_pg_1"
    assert fetched["evidence_strength"] == "STRONG"

    latest = receipts.get_latest_receipt_for_execution("exec_pg_1")
    assert latest["receipt_id"] == row["receipt_id"]

    listed = receipts.list_receipts(provider="mock_ats")
    assert any(r["receipt_id"] == row["receipt_id"] for r in listed)


def test_receipt_boolean_free_no_bool_coercion_needed(pg_db):
    """application_receipts has no boolean column (unlike several other
    Phase 8+ tables) -- this test documents that fact rather than assuming
    it, so a future column addition that DOES add a bool is forced to
    re-examine this test rather than silently skip the coercion rule."""
    with pg_db.db_session() as conn:
        cols = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'application_receipts'"
        ).fetchall()
        assert not any(c["data_type"] == "boolean" for c in cols)
