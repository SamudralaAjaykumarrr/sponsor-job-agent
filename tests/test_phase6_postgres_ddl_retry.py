"""CLAUDE.md Phase 6: real bug caught during this phase's own live
multi-worker validation -- two worker processes calling init_db()
concurrently against a fresh shared PostgreSQL database can deadlock on
overlapping `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements (Postgres
takes an ACCESS EXCLUSIVE lock even for a no-op). Fixed via
app.db_postgres.run_with_deadlock_retry -- these tests cover the retry
helper itself (fast, deterministic, no real DB needed) and a real
concurrent-startup reproduction against actual Postgres."""

import pytest

pytestmark = pytest.mark.postgres


def test_deadlock_retry_succeeds_after_transient_deadlocks():
    from app.db_postgres import run_with_deadlock_retry
    import psycopg

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise psycopg.errors.DeadlockDetected("simulated deadlock")
        return "ok"

    result = run_with_deadlock_retry(flaky, max_attempts=5)
    assert result == "ok"
    assert attempts["n"] == 3


def test_deadlock_retry_gives_up_after_max_attempts():
    from app.db_postgres import run_with_deadlock_retry
    import psycopg

    def always_fails():
        raise psycopg.errors.DeadlockDetected("simulated persistent deadlock")

    with pytest.raises(psycopg.errors.DeadlockDetected):
        run_with_deadlock_retry(always_fails, max_attempts=3)


def test_concurrent_worker_startup_against_fresh_postgres_does_not_crash(pg_fresh_db, monkeypatch):
    """Real reproduction of the bug: many threads (simulating many worker
    processes) call app.db.init_db() concurrently against the SAME fresh
    Postgres database with no prior schema. Before the fix, this reliably
    produced an unhandled DeadlockDetected in at least one thread."""
    import threading

    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)

    errors = []

    def _init():
        try:
            db.init_db()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_init) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent init_db() calls raised: {errors}"
