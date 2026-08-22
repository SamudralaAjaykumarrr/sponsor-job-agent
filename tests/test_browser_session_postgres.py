"""CLAUDE.md Phase 10 section 62: browser_assist_sessions migration +
distributed leasing under REAL PostgreSQL. Marked `postgres` -- skipped
automatically if `pgserver` isn't installed."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_migration_creates_table_and_partial_unique_index(pg_db):
    from app.applications import browser_session

    row = browser_session.create_session(
        execution_id="exec_pg_1", job_id=1, provider="greenhouse", application_url="https://x",
    )
    assert row["status"] == "STARTING"

    with pytest.raises(browser_session.DuplicateSessionError):
        browser_session.create_session(execution_id="exec_pg_2", job_id=1, provider="greenhouse", application_url="https://y")

    browser_session.update_session(row["session_id"], status="CLOSED")
    # Now that the first session is terminal, a fresh one for the same job succeeds.
    browser_session.create_session(execution_id="exec_pg_2", job_id=1, provider="greenhouse", application_url="https://y")


def test_concurrent_claim_only_one_worker_wins(pg_db):
    """CLAUDE.md Phase 10 section 63: distributed session ownership -- the
    same atomic claim guarantee Phase 8/9 already proved for
    application_executions, now for browser_assist_sessions."""
    import threading

    from app.applications import browser_session

    row = browser_session.create_session(execution_id="exec_pg_3", job_id=2, provider="lever", application_url="https://z")
    session_id = row["session_id"]

    results = []
    lock = threading.Lock()

    def _claim(worker_id: str):
        claimed = browser_session.claim_session(session_id, worker_id=worker_id, lease_seconds=60)
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=_claim, args=(f"worker-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successful = [r for r in results if r is not None]
    assert len(successful) == 1
