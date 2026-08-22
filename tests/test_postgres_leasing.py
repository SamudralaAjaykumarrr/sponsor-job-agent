"""CLAUDE.md Phase 6 sections 7, 22, 48 (scenario A): real concurrent-claim
correctness test against actual PostgreSQL -- multiple worker "processes"
(here: threads, each with its own psycopg connection, which is what
actually matters for correctness since Postgres serializes at the
connection/transaction level, not the Python thread level) claiming the
same due-portal pool must never double-claim a row."""

import threading
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.postgres


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    db.init_db()
    return db


def _seed_due_portals(db, count: int) -> None:
    now = utcnow()
    with db.db_session() as conn:
        for i in range(count):
            conn.execute(
                """INSERT INTO company_registry
                     (company_name, provider, tenant_identifier, enabled, next_poll_at, created_at, updated_at)
                   VALUES (?, 'greenhouse', ?, 1, ?, ?, ?)""",
                (f"Company {i}", f"tenant-{i}", now, now, now),
            )


def test_concurrent_workers_never_double_claim(pg_db):
    from app.workers import leasing

    portal_count = 200
    _seed_due_portals(pg_db, portal_count)

    worker_count = 8
    per_worker_limit = 50
    claimed_ids: list[list[int]] = [[] for _ in range(worker_count)]
    errors: list[Exception] = []
    barrier = threading.Barrier(worker_count)

    def _claim(worker_index: int) -> None:
        try:
            barrier.wait(timeout=10)
            rows = leasing.claim_poll_batch(
                worker_id=f"worker-{worker_index}", limit=per_worker_limit, lease_seconds=120,
            )
            claimed_ids[worker_index] = [r["id"] for r in rows]
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_claim, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"unexpected errors during concurrent claim: {errors}"

    all_claimed = [pid for ids in claimed_ids for pid in ids]
    assert len(all_claimed) == len(set(all_claimed)), "a portal was claimed by more than one worker at once"
    assert len(all_claimed) <= portal_count


def test_crash_recovery_via_lease_expiry(pg_db):
    """A worker that claims a portal and then never finishes (crash) must
    have that lease become reclaimable once it expires -- no manual
    crash-detection logic involved, just lease_expires_at passing."""
    from app.workers import leasing

    _seed_due_portals(pg_db, 1)

    # A 2s lease with a 2.5s sleep margin -- tight enough to run fast, wide
    # enough to not flake under normal CI/test-runner scheduling jitter (a
    # 1s/1.2s margin was observed to flake occasionally in this suite).
    first = leasing.claim_poll_batch(worker_id="worker-crashed", limit=1, lease_seconds=2)
    assert len(first) == 1
    portal_id = first[0]["id"]

    immediate_retry = leasing.claim_poll_batch(worker_id="worker-recovering", limit=1, lease_seconds=60)
    assert immediate_retry == [], "a live lease must not be reclaimable before it expires"

    import time

    time.sleep(2.5)

    recovered = leasing.claim_poll_batch(worker_id="worker-recovering", limit=1, lease_seconds=60)
    assert len(recovered) == 1
    assert recovered[0]["id"] == portal_id
    assert recovered[0]["lease_owner"] == "worker-recovering"


def test_verification_queue_concurrent_claims(pg_db):
    from app.workers import leasing

    now = utcnow()
    with pg_db.db_session() as conn:
        conn.execute(
            "INSERT INTO registry_companies (normalized_name, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("acme", "Acme", now, now),
        )
        company_id = conn.execute("SELECT id FROM registry_companies WHERE normalized_name = ?", ("acme",)).fetchone()["id"]
        for i in range(30):
            conn.execute(
                """INSERT INTO registry_portals
                     (company_id, provider, tenant_identifier, verification_status, enabled, created_at, updated_at)
                   VALUES (?, 'greenhouse', ?, 'DISCOVERED', 1, ?, ?)""",
                (company_id, f"tenant-{i}", now, now),
            )

    claimed_a = leasing.claim_verification_batch(worker_id="a", limit=15, lease_seconds=120)
    claimed_b = leasing.claim_verification_batch(worker_id="b", limit=15, lease_seconds=120)
    ids_a = {r["id"] for r in claimed_a}
    ids_b = {r["id"] for r in claimed_b}
    assert not (ids_a & ids_b), "verification queue must not double-claim across workers"
    assert len(ids_a) == 15
    assert len(ids_b) == 15
