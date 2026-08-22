"""CLAUDE.md Phase 8 section 60: application executor persistence on real
PostgreSQL. Marked `postgres` -- skipped automatically if `pgserver` isn't
installed (see tests/conftest.py::postgres_url)."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_application_tables_created_on_postgres(pg_db):
    with pg_db.db_session() as conn:
        for table in (
            "application_executions", "application_answer_snapshots",
            "application_audit_log", "application_form_baselines",
        ):
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            assert row["c"] == 0


def test_create_execution_and_partial_unique_index_on_postgres(pg_db):
    from app.applications import repo

    execution_id = repo.create_execution(1, provider="mock_ats", mode="ASSIST")
    assert execution_id

    with pytest.raises(repo.DuplicateExecutionError):
        repo.create_execution(1, provider="mock_ats", mode="ASSIST")

    execution = repo.get_execution(execution_id)
    assert execution["job_id"] == 1
    assert execution["active"] == 1


def test_execution_lifecycle_and_snapshot_on_postgres(pg_db):
    from app.applications import repo
    from app.applications.models import ExecutionStatus

    execution_id = repo.create_execution(2, provider="mock_ats", mode="ASSIST")
    repo.update_execution(execution_id, 2, ExecutionStatus.APPLIED, confirmation_id="PG-CONF-1")
    execution = repo.get_execution(execution_id)
    assert execution["status"] == "APPLIED"
    assert execution["active"] == 0

    # active=0 -- a fresh execution for the same job_id is now allowed.
    second_id = repo.create_execution(2, provider="mock_ats", mode="ASSIST")
    assert second_id != execution_id


def test_queue_claim_batch_on_postgres(pg_db):
    from app.applications import queue as app_queue
    from app.applications import repo

    e1 = repo.create_execution(10, provider="mock_ats", mode="ASSIST")
    e2 = repo.create_execution(11, provider="mock_ats", mode="ASSIST")

    claimed = app_queue.claim_execution_batch(worker_id="pg-worker-1", limit=10, lease_seconds=60)
    claimed_ids = {c["execution_id"] for c in claimed}
    assert {e1, e2} <= claimed_ids

    # A second worker must not be able to claim the same already-leased rows.
    claimed_again = app_queue.claim_execution_batch(worker_id="pg-worker-2", limit=10, lease_seconds=60)
    assert claimed_again == []
