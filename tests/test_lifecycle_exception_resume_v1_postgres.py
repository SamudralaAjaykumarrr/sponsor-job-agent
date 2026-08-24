"""Application-lifecycle-exception-resume-v1's new persistence
(application_blockers) against REAL PostgreSQL -- skipped automatically if
pgserver isn't installed (see tests/conftest.py::postgres_url), matching
every prior phase's `test_*_postgres_*.py` convention."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_blocker_round_trip_and_partial_unique_index(pg_db):
    from app.applications import blockers

    first = blockers.raise_blocker("pg-exec-1", 1, blockers.BlockerCode.NEEDS_CAPTCHA, provider="mock_ats")
    second = blockers.raise_blocker("pg-exec-1", 1, blockers.BlockerCode.NEEDS_CAPTCHA, provider="mock_ats")
    assert first["id"] == second["id"]

    resolved = blockers.resolve_blocker("pg-exec-1", resolution_note="done")
    assert resolved is not None
    assert blockers.get_active_blocker_for_execution("pg-exec-1") is None

    history = blockers.list_blockers_for_execution("pg-exec-1")
    assert len(history) == 1
    assert history[0]["resolved_at"] is not None


def test_blocker_terminal_class_and_job_history(pg_db):
    from app.applications import blockers

    blockers.raise_blocker("pg-exec-2", 42, blockers.BlockerCode.JOB_EXPIRED, provider="mock_ats")
    row = blockers.get_active_blocker_for_execution("pg-exec-2")
    assert row["blocker_class"] == blockers.BlockerClass.TERMINAL.value
    assert blockers.list_blockers_for_job(42)[0]["blocker_code"] == blockers.BlockerCode.JOB_EXPIRED.value


def test_concurrent_raise_never_violates_unique_index(pg_db):
    """Simulates two concurrent workers hitting the same blocking condition
    for the same execution -- both must resolve to the SAME row, never a
    UniqueViolation surfacing to the caller."""
    from app.applications import blockers

    results = [
        blockers.raise_blocker("pg-exec-3", 7, blockers.BlockerCode.NEEDS_AUTH, provider="mock_ats")
        for _ in range(5)
    ]
    ids = {r["id"] for r in results}
    assert len(ids) == 1
