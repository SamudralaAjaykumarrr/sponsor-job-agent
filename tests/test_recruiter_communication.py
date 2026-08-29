"""Tsenta Remaining-Gaps Closure V2, section 6/9: post-application
recruiter/contact communication and the truthful "mailbox not connected"
state. No real mailbox, no real network, no credentials anywhere."""

from app import notifications as _notifications
from app.applications.recruiter_communication import (
    UPDATE_CONFIRMATION,
    UPDATE_INTERVIEW_REQUEST,
    UPDATE_OTHER,
    UPDATE_REJECTION,
    UPDATE_STATUS_CHECK_IN,
    NullMailboxAdapter,
    classify_update_text,
    get_mailbox_adapter,
    list_updates,
    mailbox_status,
    record_update,
)


def test_null_mailbox_adapter_is_always_honestly_not_connected():
    adapter = NullMailboxAdapter()
    assert adapter.is_connected() is False
    assert "no mailbox is connected" in adapter.status_detail().lower()
    assert adapter.fetch_updates() == []


def test_default_active_adapter_is_the_null_adapter():
    adapter = get_mailbox_adapter()
    assert adapter.is_connected() is False


def test_mailbox_status_reports_truthfully():
    status = mailbox_status()
    assert status["connected"] is False
    assert status["detail"]


def test_classify_update_text_interview():
    assert classify_update_text("Interview request", "We'd love to schedule a call") == UPDATE_INTERVIEW_REQUEST


def test_classify_update_text_rejection():
    assert classify_update_text(
        "Update on your application", "Unfortunately we have decided to move forward with other candidates",
    ) == UPDATE_REJECTION


def test_classify_update_text_confirmation():
    assert classify_update_text("Thanks for applying", "Your application has been received") == UPDATE_CONFIRMATION


def test_classify_update_text_blank_is_other():
    assert classify_update_text("", "") == UPDATE_OTHER


def test_classify_update_text_generic_is_status_check_in():
    assert classify_update_text("Following up", "Just checking in on your application status") == \
        UPDATE_STATUS_CHECK_IN


def test_record_update_rejects_unknown_type(tmp_env):
    result = record_update(1, "NOT_A_REAL_TYPE")
    assert result.ok is False


def test_record_update_rejects_unknown_source(tmp_env):
    result = record_update(1, UPDATE_STATUS_CHECK_IN, source="carrier_pigeon")
    assert result.ok is False


def test_record_and_list_update(tmp_env):
    result = record_update(1, UPDATE_INTERVIEW_REQUEST, subject="Interview!", detail="Can you talk Tuesday?")
    assert result.ok is True
    assert result.update["update_type"] == UPDATE_INTERVIEW_REQUEST
    assert result.update["source"] == "manual"

    rows = list_updates(job_id=1)
    assert len(rows) == 1
    assert rows[0]["subject"] == "Interview!"


def test_list_updates_scoped_by_job(tmp_env):
    record_update(1, UPDATE_STATUS_CHECK_IN, subject="job 1 update")
    record_update(2, UPDATE_STATUS_CHECK_IN, subject="job 2 update")

    job1_rows = list_updates(job_id=1)
    assert len(job1_rows) == 1
    assert job1_rows[0]["subject"] == "job 1 update"

    all_rows = list_updates()
    assert len(all_rows) == 2


def test_record_update_never_writes_to_notifications_unless_asked(tmp_env):
    record_update(5, UPDATE_STATUS_CHECK_IN, subject="quiet update")
    assert _notifications.unread_count() == 0


def test_record_update_raises_needs_you_only_when_asked(tmp_env):
    record_update(5, UPDATE_INTERVIEW_REQUEST, subject="Interview request!", raise_needs_you=True)
    unread = _notifications.list_notifications(unread_only=True)
    assert len(unread) == 1
    assert unread[0]["kind"] == _notifications.KIND_NEEDS_YOU
    assert unread[0]["job_id"] == 5


def test_record_update_never_marks_anything_applied(tmp_env):
    """This module must never itself change an execution's terminal state
    -- app.applications.handoff remains the only path for that."""
    from app.applications import repo as _repo

    result = record_update(7, UPDATE_CONFIRMATION, subject="We received your application!")
    assert result.ok is True
    # no execution exists for job 7 at all -- proves record_update() never
    # tried to touch application_executions as a side effect
    assert _repo.get_active_execution_for_job(7) is None
