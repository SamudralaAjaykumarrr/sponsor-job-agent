"""CLAUDE.md Phase 9 section 48: application doctor extensions."""

from datetime import datetime, timedelta, timezone

from app.applications.doctor import run_doctor
from app.db import db_session


def test_expired_execution_lease_detected(tmp_env):
    now = datetime.now(timezone.utc)
    expired = (now - timedelta(minutes=10)).isoformat()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO application_executions
               (execution_id, job_id, provider, mode, status, active, started_at,
                lease_owner, lease_attempt_id, lease_acquired_at, lease_expires_at, created_at, updated_at)
               VALUES ('exec-lease-1', 1, 'mock_ats', 'ASSIST', 'STARTED', 1, ?, 'w1', 'a1', ?, ?, ?, ?)""",
            (now.isoformat(), expired, expired, now.isoformat(), now.isoformat()),
        )
        conn.execute(
            "INSERT INTO jobs (title, company, description, first_seen_at, last_seen_at, created_at, updated_at) "
            "VALUES ('T', 'C', 'D', ?, ?, ?, ?)",
            (now.isoformat(), now.isoformat(), now.isoformat(), now.isoformat()),
        )
    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "expired_execution_lease" in checks


def test_orphan_execution_lease_detected(tmp_env):
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO application_executions
               (execution_id, job_id, provider, mode, status, active, started_at,
                lease_owner, lease_attempt_id, created_at, updated_at)
               VALUES ('exec-orphan-1', 1, 'mock_ats', 'ASSIST', 'APPLIED', 0, ?, 'w1', 'a1', ?, ?)""",
            (now, now, now),
        )
        conn.execute(
            "INSERT INTO jobs (title, company, description, first_seen_at, last_seen_at, created_at, updated_at) "
            "VALUES ('T', 'C', 'D', ?, ?, ?, ?)", (now, now, now, now),
        )
    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "orphan_execution_lease" in checks


def test_duplicate_confirmation_detected(tmp_env):
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        for exec_id, job_id in (("exec-dupconf-1", 1), ("exec-dupconf-2", 2)):
            conn.execute(
                """INSERT INTO application_executions
                   (execution_id, job_id, provider, mode, status, active, started_at, confirmation_id,
                    created_at, updated_at)
                   VALUES (?, ?, 'mock_ats', 'ASSIST', 'APPLIED', 0, ?, 'SAME-CONF-ID', ?, ?)""",
                (exec_id, job_id, now, now, now),
            )
            conn.execute(
                "INSERT INTO jobs (id, title, company, description, first_seen_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, 'T', 'C', 'D', ?, ?, ?, ?)", (job_id, now, now, now, now),
            )
    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "duplicate_confirmation" in checks


def test_multiple_active_leases_same_job_detected(tmp_env):
    """A buggy state: the current active execution holds a lease, AND an
    older, already-terminal execution for the SAME job also still holds a
    (never-released) lease -- the partial unique index only prevents two
    simultaneously ACTIVE executions, not two simultaneously LEASED rows."""
    now = datetime.now(timezone.utc)
    future = (now + timedelta(minutes=5)).isoformat()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO application_executions
               (execution_id, job_id, provider, mode, status, active, started_at,
                lease_owner, lease_attempt_id, lease_expires_at, created_at, updated_at)
               VALUES ('exec-multi-active', 9, 'mock_ats', 'ASSIST', 'STARTED', 1, ?, 'w0', 'a0', ?, ?, ?)""",
            (now.isoformat(), future, now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO application_executions
               (execution_id, job_id, provider, mode, status, active, started_at,
                lease_owner, lease_attempt_id, lease_expires_at, created_at, updated_at)
               VALUES ('exec-multi-stale', 9, 'mock_ats', 'ASSIST', 'PERMANENT_SUBMISSION_FAILURE', 0, ?,
                        'w1', 'a1', ?, ?, ?)""",
            (now.isoformat(), future, now.isoformat(), now.isoformat()),
        )
        conn.execute(
            "INSERT INTO jobs (id, title, company, description, first_seen_at, last_seen_at, created_at, updated_at) "
            "VALUES (9, 'T', 'C', 'D', ?, ?, ?, ?)", (now.isoformat(), now.isoformat(), now.isoformat(), now.isoformat()),
        )
    report = run_doctor()
    checks = {i.check for i in report.issues}
    assert "multiple_active_leases_same_job" in checks
    assert "orphan_execution_lease" in checks  # the stale terminal row's own lease is also flagged


def test_clean_database_reports_no_phase9_issues(tmp_env):
    report = run_doctor()
    phase9_checks = {
        "expired_execution_lease", "orphan_execution_lease", "multiple_active_leases_same_job",
        "duplicate_confirmation", "unknown_submission_retried", "non_full_time_queued",
        "non_confirmed_sponsorship_queued", "rate_limit_accounting_inconsistency",
    }
    found = {i.check for i in report.issues}
    assert not (found & phase9_checks)
