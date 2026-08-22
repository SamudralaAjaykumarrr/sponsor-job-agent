"""CLAUDE.md Phase 9 section 6: per-attempt history persistence."""

from app.applications import attempts as attempts_repo


def _record(execution_id="exec-1", **overrides):
    defaults = dict(
        attempt_id=attempts_repo.new_attempt_id(), execution_id=execution_id, job_id=1,
        worker_id="w1", provider="mock_ats", stage="applied", result="APPLIED",
    )
    defaults.update(overrides)
    attempts_repo.record_attempt(attempts_repo.ApplicationAttemptRecord(**defaults))


def test_record_and_list_attempt(tmp_env):
    _record()
    rows = attempts_repo.list_attempts_for_execution("exec-1")
    assert len(rows) == 1
    assert rows[0]["result"] == "APPLIED"
    assert rows[0]["provider"] == "mock_ats"


def test_never_stores_secrets_columns(tmp_env):
    """Structural guard: the attempts table has no column shaped like a
    secret/credential -- only ids/stage/result/timestamps/bounded text."""
    from app.db import db_session

    with db_session() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(application_attempts)").fetchall()}
    forbidden = {"password", "token", "secret", "session_cookie", "mfa_code"}
    assert not (cols & forbidden)


def test_history_is_bounded_per_execution(tmp_env, monkeypatch):
    monkeypatch.setattr(attempts_repo, "_MAX_ATTEMPTS_PER_EXECUTION", 3)
    for i in range(6):
        _record(execution_id="exec-bounded", attempt_id=f"attempt-{i}")
    rows = attempts_repo.list_attempts_for_execution("exec-bounded", limit=100)
    assert len(rows) == 3


def test_list_recent_attempts_filters_by_worker_and_result(tmp_env):
    _record(execution_id="exec-2", worker_id="w2", result="NEEDS_USER_ACTION")
    _record(execution_id="exec-3", worker_id="w3", result="APPLIED")
    only_w2 = attempts_repo.list_recent_attempts(worker_id="w2")
    assert len(only_w2) == 1
    assert only_w2[0]["execution_id"] == "exec-2"

    only_applied = attempts_repo.list_recent_attempts(result="APPLIED")
    assert all(r["result"] == "APPLIED" for r in only_applied)
