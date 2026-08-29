"""Tsenta-parity-closure-v1, P0#2: deterministic coverage for the new
"READY FOR FINAL REVIEW" human hand-off outcome recorder
(app.applications.handoff). No real employer network, no real submission --
every scenario operates on a synthetic job/execution row."""

import pytest

from app.applications import handoff, repo
from app.applications.models import ExecutionStatus
from app.jobs_repo import get_job, insert_job
from app.models import ApplicationMode, ApplicationState, Job


@pytest.fixture(autouse=True)
def _db(tmp_env):
    return tmp_env


def _make_job(external_job_id: str) -> int:
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="full-time backend role", employment_type="Full-time", provider="lever",
        external_job_id=external_job_id, mode=ApplicationMode.ASSIST,
    )
    return insert_job(job)


def _approved_execution(job_id: int) -> str:
    execution_id = repo.create_execution(job_id, provider="lever", mode="ASSIST")
    repo.update_execution(execution_id, job_id, ExecutionStatus.APPROVED)
    return execution_id


def test_is_ready_for_final_review_true_only_for_approved_or_needs_action():
    for status, expected in (
        (ExecutionStatus.APPROVED, True), (ExecutionStatus.NEEDS_USER_ACTION, True),
        (ExecutionStatus.QUEUED, False), (ExecutionStatus.SUBMISSION_STATUS_UNKNOWN, False),
        (ExecutionStatus.APPLIED, False),
    ):
        assert handoff.is_ready_for_final_review({"status": status.value, "active": 1}) is expected
    assert handoff.is_ready_for_final_review({"status": ExecutionStatus.APPROVED.value, "active": 0}) is False
    assert handoff.is_ready_for_final_review(None) is False


def test_build_final_review_matches_the_existing_presubmit_manifest(sample_profile):
    from app.candidate.profile import save_profile

    save_profile(sample_profile)
    job_id = _make_job("handoff-fr-1")
    manifest = handoff.build_final_review(job_id)
    assert manifest is not None
    assert manifest.company == "Acme Corp"
    assert manifest.title == "Backend Software Engineer"


def test_submitted_confirmed_requires_a_confirmation_artifact():
    job_id = _make_job("handoff-1")
    execution_id = _approved_execution(job_id)

    result = handoff.record_manual_outcome(execution_id, handoff.OUTCOME_SUBMITTED_CONFIRMED)

    assert result.ok is False
    assert "confirmation" in result.detail.lower()
    unchanged = repo.get_execution(execution_id)
    assert unchanged["status"] == ExecutionStatus.APPROVED.value
    assert unchanged["active"] == 1


def test_submitted_confirmed_with_confirmation_id_marks_applied():
    job_id = _make_job("handoff-2")
    execution_id = _approved_execution(job_id)

    result = handoff.record_manual_outcome(
        execution_id, handoff.OUTCOME_SUBMITTED_CONFIRMED, confirmation_id="ABC-123",
    )

    assert result.ok is True
    assert result.execution_status == ExecutionStatus.APPLIED.value
    row = repo.get_execution(execution_id)
    assert row["status"] == ExecutionStatus.APPLIED.value
    assert row["active"] == 0
    assert row["confirmation_id"] == "ABC-123"
    job = get_job(job_id)
    assert job.application_state == ApplicationState.APPLIED


def test_user_completed_externally_is_never_confused_with_applied():
    job_id = _make_job("handoff-3")
    execution_id = _approved_execution(job_id)

    result = handoff.record_manual_outcome(execution_id, handoff.OUTCOME_USER_COMPLETED_EXTERNALLY, note="did it by hand")

    assert result.ok is True
    assert result.execution_status == ExecutionStatus.USER_COMPLETED_EXTERNALLY.value
    row = repo.get_execution(execution_id)
    assert row["status"] == ExecutionStatus.USER_COMPLETED_EXTERNALLY.value
    assert row["status"] != ExecutionStatus.APPLIED.value
    assert row["active"] == 0
    # Never fabricates a receipt for a self-reported, unverified completion.
    from app.applications.receipts import get_latest_receipt_for_execution

    assert get_latest_receipt_for_execution(execution_id) is None
    job = get_job(job_id)
    assert job.application_state == ApplicationState.COMPLETED_BY_USER
    assert job.application_state != ApplicationState.APPLIED


def test_submission_status_unknown_hands_off_to_existing_reconcile_queue():
    job_id = _make_job("handoff-4")
    execution_id = _approved_execution(job_id)

    result = handoff.record_manual_outcome(execution_id, handoff.OUTCOME_SUBMISSION_STATUS_UNKNOWN)
    assert result.ok is True
    row = repo.get_execution(execution_id)
    assert row["status"] == ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value
    assert row["active"] == 1  # still blocks a second concurrent attempt, matches the existing model

    # The existing, unmodified reconcile queue can now resolve it -- no
    # second/parallel reconciliation mechanism was introduced.
    from app.applications.reconcile import reconcile_execution

    outcome = reconcile_execution(execution_id, "confirmed_applied", confirmation_id="XYZ-1")
    assert outcome.ok is True
    assert repo.get_execution(execution_id)["status"] == ExecutionStatus.APPLIED.value


def test_not_submitted_marks_withdrawn_and_frees_the_job_for_a_fresh_attempt():
    job_id = _make_job("handoff-5")
    execution_id = _approved_execution(job_id)

    result = handoff.record_manual_outcome(execution_id, handoff.OUTCOME_NOT_SUBMITTED)
    assert result.ok is True
    assert result.execution_status == ExecutionStatus.WITHDRAWN.value
    row = repo.get_execution(execution_id)
    assert row["status"] == ExecutionStatus.WITHDRAWN.value
    assert row["active"] == 0

    # A duplicate/no-duplicate-execution guarantee: the job is now free for
    # a fresh execution row (the partial unique index only guards active=1).
    fresh_execution_id = repo.create_execution(job_id, provider="lever", mode="ASSIST")
    assert fresh_execution_id != execution_id


def test_ineligible_status_is_rejected_without_mutating_the_row():
    job_id = _make_job("handoff-6")
    execution_id = repo.create_execution(job_id, provider="lever", mode="ASSIST")  # stays QUEUED

    result = handoff.record_manual_outcome(execution_id, handoff.OUTCOME_NOT_SUBMITTED)

    assert result.ok is False
    assert "not eligible" in result.detail
    unchanged = repo.get_execution(execution_id)
    assert unchanged["status"] == ExecutionStatus.QUEUED.value
    assert unchanged["active"] == 1


def test_unknown_outcome_value_is_rejected():
    job_id = _make_job("handoff-7")
    execution_id = _approved_execution(job_id)
    result = handoff.record_manual_outcome(execution_id, "SOMETHING_MADE_UP")
    assert result.ok is False
    assert repo.get_execution(execution_id)["status"] == ExecutionStatus.APPROVED.value


def test_missing_execution_is_rejected():
    result = handoff.record_manual_outcome("exec_does_not_exist", handoff.OUTCOME_NOT_SUBMITTED)
    assert result.ok is False
    assert "not found" in result.detail
