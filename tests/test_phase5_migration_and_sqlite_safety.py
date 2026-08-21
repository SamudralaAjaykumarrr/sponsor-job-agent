import sqlite3

from app import config
from app.db import db_session, init_db


def test_init_db_is_idempotent_and_additive(tmp_env):
    init_db()
    init_db()  # must not raise, must not lose anything
    with db_session() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for expected in ("poll_attempts", "workers", "dead_letters", "provider_circuit_state", "registry_acquisition_batches"):
        assert expected in tables


def test_migration_preserves_existing_jobs_row(tmp_env):
    """Simulates a DB that already has real data (a job row) from before
    Phase 5 -- running init_db() again must never lose it."""
    from app.jobs_repo import insert_job
    from app.models import Job

    job_id = insert_job(Job(title="Existing Job", company="Acme", description="d"))
    init_db()  # re-run migrations against the now-populated DB
    with db_session() as conn:
        row = conn.execute("SELECT title FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["title"] == "Existing Job"


def test_migration_preserves_existing_registry_entry(tmp_env):
    from app.registry.models import CompanyRegistryEntry
    from app.registry import repo as registry_repo

    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    init_db()
    entry = registry_repo.get_entry(entry_id)
    assert entry is not None
    assert entry.company_name == "Acme"
    # New lease columns exist and default to unset -- no data loss, no
    # spurious lease.
    assert entry.lease_owner is None


def test_wal_mode_and_busy_timeout_configured(tmp_env):
    with db_session() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert mode.lower() == "wal"
    assert timeout >= 1000  # a real, non-trivial busy timeout is configured


def test_lease_columns_have_indexes(tmp_env):
    with db_session() as conn:
        indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_company_registry_lease_expiry" in indexes
    assert "idx_registry_portals_verify_lease_expiry" in indexes
    assert "idx_poll_attempts_status" in indexes
    assert "idx_dead_letters_open" in indexes


def test_concurrent_writers_do_not_corrupt_or_deadlock(tmp_env):
    """Two connections writing in overlapping short transactions must both
    eventually succeed (busy_timeout lets the second wait rather than error)
    rather than raising 'database is locked'."""
    import threading

    errors = []

    def writer(n):
        try:
            for i in range(20):
                with db_session() as conn:
                    conn.execute(
                        "INSERT INTO discovery_log (provider, started_at) VALUES (?, ?)",
                        (f"writer{n}", "2026-01-01T00:00:00"),
                    )
        except sqlite3.OperationalError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected locking errors: {errors}"
    with db_session() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM discovery_log").fetchone()["c"]
    assert count == 80
