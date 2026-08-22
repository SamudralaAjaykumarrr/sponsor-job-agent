"""CLAUDE.md Phase 15 large-state dashboard validation: a genuine N+1 query
pattern was found in the dashboard's active-execution lookup (one query per
job, unlike the already-batched resume variant/quality-report lookups) and
fixed by app.applications.repo.get_active_executions_for_jobs(). This test
locks in both correctness and the single-query behavior so it can't
regress back to N+1."""

from app.applications.models import ExecutionMode, ExecutionStatus
from app.applications.repo import create_execution, get_active_executions_for_jobs, update_execution
from app.jobs_repo import insert_job
from app.models import Job


def _job() -> int:
    return insert_job(Job(title="Backend Engineer", company="Acme", description="x"))


def test_empty_input_returns_empty_dict(tmp_env):
    assert get_active_executions_for_jobs([]) == {}


def test_returns_correct_mapping_for_multiple_jobs(tmp_env):
    job_a = _job()
    job_b = _job()
    job_c = _job()  # no execution at all

    exec_a = create_execution(job_a, provider="mock_ats", mode=ExecutionMode.ASSIST.value)
    exec_b = create_execution(job_b, provider="mock_ats", mode=ExecutionMode.ASSIST.value)

    result = get_active_executions_for_jobs([job_a, job_b, job_c])

    assert result[job_a]["execution_id"] == exec_a
    assert result[job_b]["execution_id"] == exec_b
    assert job_c not in result


def test_terminal_execution_excluded(tmp_env):
    job_a = _job()
    exec_a = create_execution(job_a, provider="mock_ats", mode=ExecutionMode.ASSIST.value)
    update_execution(exec_a, job_a, ExecutionStatus.WITHDRAWN)

    result = get_active_executions_for_jobs([job_a])
    assert job_a not in result


def test_issues_exactly_one_query_for_many_jobs(tmp_env, monkeypatch):
    """Regression guard for the N+1 pattern this function was added to fix
    -- N jobs must always cost exactly one SELECT against
    application_executions, never N. Uses sqlite3's own trace-callback
    mechanism (Connection is an immutable C type -- its methods can't be
    monkeypatched, but tracing every executed statement is a real, public
    sqlite3 API built for exactly this)."""
    import app.db as db_module

    job_ids = [_job() for _ in range(25)]
    for jid in job_ids[:10]:
        create_execution(jid, provider="mock_ats", mode=ExecutionMode.ASSIST.value)

    calls = []
    real_get_sqlite_connection = db_module.get_sqlite_connection

    def _traced_get_sqlite_connection():
        conn = real_get_sqlite_connection()
        conn.set_trace_callback(calls.append)
        return conn

    monkeypatch.setattr(db_module, "get_sqlite_connection", _traced_get_sqlite_connection)

    result = get_active_executions_for_jobs(job_ids)

    execution_queries = [c for c in calls if "application_executions" in c]
    assert len(execution_queries) == 1, f"expected exactly 1 query, got {len(execution_queries)}: {execution_queries}"
    assert len(result) == 10
