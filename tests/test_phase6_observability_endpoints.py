"""CLAUDE.md Phase 6 sections 29-34: /metrics, /readiness, enhanced /fleet
page, and admin safety actions (force-probe, close circuit, mark worker
offline, reap orphans)."""

from fastapi.testclient import TestClient

from app.main import app


def test_readiness_ok_on_healthy_sqlite_db(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True
        assert data["database_backend"] == "sqlite"
        assert "schema_version" in data
        # Never leaks a DSN/credentials.
        assert "postgresql://" not in str(data)
        assert "password" not in str(data).lower()


def test_health_never_touches_database(tmp_env, monkeypatch):
    import app.db as db

    with TestClient(app) as client:
        # Patch only AFTER app startup (which legitimately calls init_db())
        # -- the guarantee under test is that the /health *handler* itself
        # never touches the database, not that the app never does at boot.
        def _boom():
            raise RuntimeError("DB should never be touched by /health")

        monkeypatch.setattr(db, "db_session", _boom)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_metrics_endpoint_returns_prometheus_text_format(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "sponsor_job_agent_workers_online" in resp.text
        assert "# TYPE" in resp.text
        # No candidate PII leaks into metrics.
        assert "@" not in resp.text  # no email addresses


def test_metrics_endpoint_disabled_returns_404(tmp_env, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "METRICS_ENABLED", False)
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 404


def test_fleet_page_shows_system_info(tmp_env):
    with TestClient(app) as client:
        resp = client.get("/fleet")
        assert resp.status_code == 200
        assert "Database backend" in resp.text
        assert "sqlite" in resp.text
        assert "Schema version" in resp.text


def test_admin_force_probe_and_close_circuit(tmp_env):
    from app.workers import circuit

    # Trip the circuit open first.
    for _ in range(6):
        circuit.record_result("greenhouse", success=False)
    assert circuit.get_status("greenhouse").state == "OPEN"

    with TestClient(app) as client:
        resp = client.post("/fleet/circuit/greenhouse/force-probe")
        assert resp.status_code in (200, 303)
    assert circuit.get_status("greenhouse").state == "HALF_OPEN"

    with TestClient(app) as client:
        resp = client.post("/fleet/circuit/greenhouse/close")
        assert resp.status_code in (200, 303)
    assert circuit.get_status("greenhouse").state == "CLOSED"


def test_admin_mark_worker_offline(tmp_env):
    from app.workers import repo as workers_repo

    workers_repo.upsert_worker("w1", hostname="h", pid=1, shard_index=0, shard_count=1, status="IDLE")
    with TestClient(app) as client:
        resp = client.post("/fleet/workers/w1/mark-offline")
        assert resp.status_code in (200, 303)
    assert workers_repo.get_worker("w1")["status"] == "OFFLINE"


def test_admin_reap_orphans_endpoint(tmp_env):
    from datetime import datetime, timedelta, timezone

    from app.db import db_session
    from app.workers import repo as workers_repo

    workers_repo.upsert_worker("stale1", hostname="h", pid=1, shard_index=0, shard_count=1, status="IDLE")
    stale_hb = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db_session() as conn:
        conn.execute("UPDATE workers SET last_heartbeat_at = ? WHERE worker_id = ?", (stale_hb, "stale1"))

    with TestClient(app) as client:
        resp = client.post("/fleet/reap-orphans")
        assert resp.status_code in (200, 303)
    assert workers_repo.get_worker("stale1")["status"] == "OFFLINE"


def test_no_destructive_delete_all_admin_route_exists():
    routes = [r.path for r in app.routes]
    assert not any("delete-all" in r or "purge" in r or "wipe" in r for r in routes)
