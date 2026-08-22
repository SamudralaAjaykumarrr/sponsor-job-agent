"""CLAUDE.md Phase 6 sections 8, 19, 20: multi-machine worker identity
metadata, schema-compatibility startup check, and the orphan-worker
reaper. All against SQLite (the default test backend) -- Postgres-specific
sharing of this same mechanism is covered by tests/test_postgres_leasing.py."""

from datetime import datetime, timedelta, timezone

import pytest

from app import migrations
from app.workers import repo as workers_repo
from app.workers.identity import generate_worker_identity
from app.workers.models import WorkerStatus
from app.workers.reaper import reap_orphans
from app.workers.runner import Worker


def test_generated_identity_has_no_pii_and_includes_version_metadata(tmp_env):
    identity = generate_worker_identity()
    assert identity.worker_id
    assert identity.hostname
    assert identity.pid > 0
    assert identity.worker_version
    assert identity.schema_version == migrations.CURRENT_SCHEMA_VERSION
    assert identity.capability_version
    assert identity.backend == "sqlite"
    # No candidate PII fields exist on the dataclass at all -- structural
    # guarantee, not just a runtime check.
    assert set(identity.__dataclass_fields__) == {
        "worker_id", "hostname", "pid", "worker_version", "schema_version", "capability_version", "backend",
    }


def test_upsert_worker_persists_version_metadata(tmp_env):
    workers_repo.upsert_worker(
        "w1", hostname="host-a", pid=111, shard_index=0, shard_count=1, status="STARTING",
        worker_version="6.0.0", schema_version=7, capability_version="abc123", backend="postgres",
    )
    stored = workers_repo.get_worker("w1")
    assert stored["worker_version"] == "6.0.0"
    assert stored["schema_version"] == 7
    assert stored["capability_version"] == "abc123"
    assert stored["backend"] == "postgres"


def test_worker_refuses_to_start_against_older_schema(tmp_env, monkeypatch):
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", migrations.CURRENT_SCHEMA_VERSION + 1000)
    w = Worker(single_cycle=True)
    with pytest.raises(RuntimeError, match="refusing to start"):
        w._check_schema_compatibility()


def test_worker_warns_but_proceeds_against_newer_schema(tmp_env, monkeypatch, caplog):
    from app.db import db_session

    with db_session() as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, backend, applied_at) VALUES (?, ?, ?, ?)",
            (99999, "future_migration_from_a_newer_worker", "sqlite", datetime.now(timezone.utc).isoformat()),
        )
    w = Worker(single_cycle=True)
    with caplog.at_level("WARNING"):
        w._check_schema_compatibility()  # must not raise
    assert any("newer than this worker's code" in rec.message for rec in caplog.records)


def test_reap_orphans_marks_stale_worker_offline_not_live_ones(tmp_env):
    stale_hb = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    fresh_hb = datetime.now(timezone.utc).isoformat()
    workers_repo.upsert_worker("stale-worker", hostname="h1", pid=1, shard_index=0, shard_count=1, status="IDLE")
    workers_repo.upsert_worker("fresh-worker", hostname="h2", pid=2, shard_index=0, shard_count=1, status="IDLE")
    from app.db import db_session

    with db_session() as conn:
        conn.execute("UPDATE workers SET last_heartbeat_at = ? WHERE worker_id = ?", (stale_hb, "stale-worker"))
        conn.execute("UPDATE workers SET last_heartbeat_at = ? WHERE worker_id = ?", (fresh_hb, "fresh-worker"))

    reaped = reap_orphans(stale_after_seconds=300)

    assert "stale-worker" in reaped
    assert "fresh-worker" not in reaped
    assert workers_repo.get_worker("stale-worker")["status"] == WorkerStatus.OFFLINE.value
    assert workers_repo.get_worker("fresh-worker")["status"] == "IDLE"


def test_reap_orphans_never_touches_already_stopped_workers(tmp_env):
    stale_hb = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    workers_repo.upsert_worker("stopped-worker", hostname="h1", pid=1, shard_index=0, shard_count=1, status="STOPPED")
    from app.db import db_session

    with db_session() as conn:
        conn.execute("UPDATE workers SET last_heartbeat_at = ? WHERE worker_id = ?", (stale_hb, "stopped-worker"))

    reaped = reap_orphans(stale_after_seconds=300)
    assert "stopped-worker" not in reaped
    assert workers_repo.get_worker("stopped-worker")["status"] == "STOPPED"
