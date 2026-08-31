"""attempt_user_submit_reconciliation_from_evidence(): the same CONFIRMED/
duplicate/no-evidence decision and persistence as
attempt_user_submit_reconciliation(), but driven by independently obtained
page evidence rather than a live browser DOM read -- for the case a live
browser session cannot be safely re-inspected in-process. No Playwright/
Chromium needed at all -- this path never touches a live browser."""

from app.applications import browser_assist, browser_session
from app.applications import repo as executions_repo
from app.applications.models import ExecutionStatus
from app.jobs_repo import insert_job
from app.models import ApplicationState, Job, SponsorshipStatus

ROBINHOOD_BODY_TEXT = (
    "Thank you for your interest in joining our world-class team at Robinhood! "
    "What happens now? We will review your application and contact you if there "
    "is a good match. If you are not contacted, be assured that your resume will "
    "remain in our database for future openings. Sincerely, The Robinhood "
    "Recruiting Team."
)
CONFIRMATION_URL = "https://job-boards.greenhouse.io/robinhood/jobs/7263592/confirmation?gh_src=gh_src%3D"


def _prepared_session(tmp_env):
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Full-time role. H-1B sponsorship is available.", employment_type="full_time",
        sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, technical_match_score=80.0,
        application_state=ApplicationState.READY_TO_APPLY, provider="greenhouse",
        canonical_url="https://boards.greenhouse.io/acme/jobs/1", url="https://boards.greenhouse.io/acme/jobs/1",
    )
    job_id = insert_job(job)
    execution_id = executions_repo.create_execution(job_id, provider="greenhouse", mode="ASSIST")
    session = browser_session.create_session(
        execution_id=execution_id, job_id=job_id, provider="greenhouse",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    return job_id, execution_id, session["session_id"]


def test_confirms_and_persists_from_independently_obtained_evidence(tmp_env):
    """Real scenario caught live (2026-08-31, job 200/Robinhood): a plain
    HTTP GET of Greenhouse's own public, session-independent /confirmation
    URL returned this exact body text, which the shared confirmation_parser/
    confirmation_evidence pipeline genuinely grades as confirming -- proving
    the SAME persistence a live-browser confirmation would produce."""
    job_id, execution_id, session_id = _prepared_session(tmp_env)

    result = browser_assist.attempt_user_submit_reconciliation_from_evidence(
        session_id, current_url=CONFIRMATION_URL, body_text=ROBINHOOD_BODY_TEXT,
    )
    assert result["ok"] is True
    assert result["session"]["status"] == "CONFIRMED"
    assert result["session"]["confirmation_url"] == CONFIRMATION_URL

    execution = executions_repo.get_execution(execution_id)
    assert execution["status"] == ExecutionStatus.APPLIED.value
    assert execution["confirmation_url"] == CONFIRMATION_URL
    assert execution["requires_user_action"] == 0

    from app.applications import receipts

    execution_receipts = receipts.list_receipts_for_execution(execution_id)
    assert len(execution_receipts) == 1
    assert execution_receipts[0]["submitted_via"] == "browser_assist_external_evidence:greenhouse"


def test_no_matching_phrase_never_confirms(tmp_env):
    _job_id, execution_id, session_id = _prepared_session(tmp_env)

    result = browser_assist.attempt_user_submit_reconciliation_from_evidence(
        session_id, current_url="https://boards.greenhouse.io/acme/jobs/1",
        body_text="Please review your answers before submitting.",
    )
    assert result["ok"] is False
    assert "no confirmation evidence" in result["detail"]

    execution = executions_repo.get_execution(execution_id)
    assert execution["status"] != ExecutionStatus.APPLIED.value


def test_duplicate_application_evidence_never_confirms(tmp_env):
    _job_id, execution_id, session_id = _prepared_session(tmp_env)

    result = browser_assist.attempt_user_submit_reconciliation_from_evidence(
        session_id, current_url="https://boards.greenhouse.io/acme/jobs/1",
        body_text="You have already applied to this position.",
    )
    assert result["ok"] is False
    assert result["session"]["status"] == "DUPLICATE_APPLICATION_DETECTED"

    execution = executions_repo.get_execution(execution_id)
    assert execution["status"] != ExecutionStatus.APPLIED.value


def test_idempotent_when_already_confirmed(tmp_env):
    _job_id, execution_id, session_id = _prepared_session(tmp_env)

    first = browser_assist.attempt_user_submit_reconciliation_from_evidence(
        session_id, current_url=CONFIRMATION_URL, body_text=ROBINHOOD_BODY_TEXT,
    )
    assert first["ok"] is True

    second = browser_assist.attempt_user_submit_reconciliation_from_evidence(
        session_id, current_url=CONFIRMATION_URL, body_text=ROBINHOOD_BODY_TEXT,
    )
    assert second["ok"] is True
    assert second["detail"] == "already confirmed"

    from app.applications import receipts

    assert len(receipts.list_receipts_for_execution(execution_id)) == 1
