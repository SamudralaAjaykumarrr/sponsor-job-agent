"""CLAUDE.md Phase 11 section 53: collect_phase11() metrics. Every value is
a live DB query -- no PII, no in-memory counters."""

import pytest

from app.applications import browser_session, metrics, repo as executions_repo
from app.applications.capability_evidence import EvidenceVerificationType, record_evidence
from app.jobs_repo import insert_job
from app.models import ApplicationState, Job, SponsorshipStatus


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _job() -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Full-time role.", employment_type="full_time",
        sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, application_state=ApplicationState.READY_TO_APPLY,
    )


def test_empty_state_all_zero():
    m = metrics.collect_phase11()
    assert m["apply_entry_detected_total"] == 0
    assert m["ready_for_final_submit"] == 0
    assert m["capability_evidence_stale"] == 0


def test_apply_entry_detected_counts_clicked_sessions():
    job_id = insert_job(_job())
    execution_id = executions_repo.create_execution(job_id, provider="smartrecruiters", mode="ASSIST")
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="smartrecruiters",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], apply_entry_clicked=1)
    m = metrics.collect_phase11()
    assert m["apply_entry_detected_total"] == 1


def test_reconstructed_total_sums_across_sessions():
    for i in range(2):
        job_id = insert_job(_job())
        execution_id = executions_repo.create_execution(job_id, provider="greenhouse", mode="ASSIST")
        session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                                   application_url=f"https://x{i}")
        browser_session.update_session(session["session_id"], reconstructed_count=i + 1)
    m = metrics.collect_phase11()
    assert m["browser_sessions_reconstructed_total"] == 3


def test_ready_for_final_submit_and_confirmed_counts():
    job_id = insert_job(_job())
    execution_id = executions_repo.create_execution(job_id, provider="greenhouse", mode="ASSIST")
    session = browser_session.create_session(execution_id=execution_id, job_id=job_id, provider="greenhouse",
                                               application_url="https://x")
    browser_session.update_session(session["session_id"], status="READY_FOR_FINAL_SUBMIT")
    m = metrics.collect_phase11()
    assert m["ready_for_final_submit"] == 1


def test_capability_evidence_stale_reflected():
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    record_evidence("greenhouse", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC, observed_at=old)
    m = metrics.collect_phase11()
    assert m["capability_evidence_stale"] == 1
