"""CLAUDE.md Phase 15 large-state dashboard validation: a benchmark measured
the dashboard's rendered pipeline table growing unboundedly with total job
count (24MB HTML / 5.7s at 50,000 synthetic jobs). Fixed by capping the
final, already-filtered/sorted table to config.DASHBOARD_MAX_TABLE_ROWS.
This locks in: the cap applies, the "showing top N of M" note appears, and
-- critically -- the cap is applied LAST so needs_action_only/resume_status
filters still search the full matching set, not just the first page."""

from fastapi.testclient import TestClient

from app.applications.models import ExecutionMode, ExecutionStatus
from app.applications.repo import create_execution, update_execution
from app.jobs_repo import insert_job
from app.main import app
from app.models import ApplicationMode, Job


def _job(i: int) -> Job:
    return Job(
        title=f"Backend Engineer {i}", company=f"Acme {i}", location="Remote (US)",
        description="Python FastAPI role. Visa sponsorship available.", mode=ApplicationMode.ASSIST,
        employment_type="FULL_TIME",
    )


def test_dashboard_caps_rendered_rows(tmp_env, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "DASHBOARD_MAX_TABLE_ROWS", 5)
    for i in range(20):
        insert_job(_job(i))

    client = TestClient(app)
    resp = client.get("/", params={"full_time_only": "false"})
    assert resp.status_code == 200
    body = resp.text
    assert "Showing top 5 of 20 matching jobs" in body


def test_dashboard_shows_no_cap_note_when_under_cap(tmp_env, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "DASHBOARD_MAX_TABLE_ROWS", 500)
    for i in range(3):
        insert_job(_job(i))

    client = TestClient(app)
    resp = client.get("/", params={"full_time_only": "false"})
    assert resp.status_code == 200
    assert "matching jobs" not in resp.text


def test_needs_action_filter_searches_beyond_the_cap(tmp_env, monkeypatch):
    """The job needing action is job #19 (beyond a cap of 5) -- if the cap
    were applied BEFORE filtering (a wrong/regressed implementation), this
    job would never be found."""
    import app.config as config

    monkeypatch.setattr(config, "DASHBOARD_MAX_TABLE_ROWS", 5)
    job_ids = [insert_job(_job(i)) for i in range(20)]

    target_job_id = job_ids[19]
    execution_id = create_execution(target_job_id, provider="mock_ats", mode=ExecutionMode.ASSIST.value)
    update_execution(execution_id, target_job_id, ExecutionStatus.NEEDS_USER_ACTION, requires_user_action=1)

    client = TestClient(app)
    resp = client.get("/", params={"needs_action_only": "true", "full_time_only": "false"})
    assert resp.status_code == 200
    assert f"Acme 19" in resp.text
