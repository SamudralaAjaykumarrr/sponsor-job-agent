"""Approval-gated-autonomy-v1: the ONE normal human gate (READY_FOR_APPROVAL
-> explicit APPROVE & APPLY). Exercises app.applications.approval directly
against the real, unmodified executor/eligibility/mock_ats pipeline -- no
manual intermediate button clicks, matching this project's existing
orchestrator/executor test conventions (see tests/test_agent_orchestrator.py,
tests/test_applications_mock_ats.py)."""

import json
import threading

import httpx
import pytest

from app import config
from app.applications import approval as applications_approval
from app.applications import product_state
from app.applications import provider_registry
from app.applications import repo as applications_repo
from app.applications.executor import _approved_submit_permitted, process_execution, queue_application
from app.applications.mock_ats import MockATSProvider
from app.applications.models import AutomationPolicy, ExecutionStatus
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


def _mock_job(scenario: str, external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": scenario}),
        mode=ApplicationMode.ASSIST,
    )


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    # Approval-gated-autonomy-v1: AUTO_SUBMIT_ENABLED stays OFF throughout --
    # approval is the only thing that may unlock submission in these tests.
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)


def _prepare_ready_for_approval(job: Job) -> dict:
    result = queue_application(job.id, mode="ASSIST")
    assert result.queued
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value
    return execution


def test_prepared_application_stops_at_ready_for_approval_never_needs_action(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-1"))
    execution = _prepare_ready_for_approval(job)

    # No submission has happened yet -- the ONE normal human gate.
    assert product_state.ready_for_approval(execution) is True
    assert product_state.needs_user_action(execution) is False
    assert product_state.submitted(execution) is False
    assert product_state.confirmed(execution) is False

    # The Needs Action queue must never include a plain READY_FOR_APPROVAL
    # item (spec section 15) -- only genuine blockers do.
    from app.pipeline_dashboard import build_needs_action_queue, count_ready_for_approval

    needs_action_job_ids = {item["job_id"] for item in build_needs_action_queue(limit=200)}
    assert job.id not in needs_action_job_ids
    assert count_ready_for_approval() >= 1


def test_approve_and_apply_reaches_applied_for_mock_ats(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-2"))
    _prepare_ready_for_approval(job)

    result = applications_approval.approve_and_apply(job.id)

    assert result.ok is True
    assert result.execution["status"] == ExecutionStatus.APPLIED.value
    assert result.execution["confirmation_id"]
    assert product_state.confirmed(result.execution) is True

    approval = applications_approval.get_latest_approval(result.execution_id)
    assert approval is not None
    assert approval["approval_id"] == result.approval_id
    assert approval["submission_capability"] == "SUPPORTED"

    # An audit trail entry was recorded for the approval action itself.
    audit = applications_repo.list_audit_log(execution_id=result.execution_id)
    assert any(a["event_type"] == "approved" for a in audit)


def test_approve_and_apply_requires_ready_for_approval(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-3"))
    # No execution ever queued for this job.
    result = applications_approval.approve_and_apply(job.id)
    assert result.ok is False
    assert "no active application" in result.reason


def test_approve_and_apply_is_idempotent_after_first_success(tmp_env, sample_profile):
    """CLAUDE.md approval spec section 22 'approval double-click' scenario:
    a second APPROVE & APPLY click after the first already completed must
    never trigger a second submission attempt."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-4"))
    _prepare_ready_for_approval(job)

    first = applications_approval.approve_and_apply(job.id)
    assert first.ok is True
    assert first.execution["status"] == ExecutionStatus.APPLIED.value

    second = applications_approval.approve_and_apply(job.id)
    assert second.ok is False  # no longer an active execution -- job is terminal (APPLIED)

    # Only one approval row and one submission were ever recorded.
    approvals = applications_approval.list_approvals_for_job(job.id)
    assert len(approvals) == 1
    audit = applications_repo.list_audit_log(execution_id=first.execution_id)
    assert sum(1 for a in audit if a["event_type"] == "submit_attempted") == 1


def test_concurrent_approval_claim_only_lets_one_caller_through(tmp_env, sample_profile):
    """Directly exercises the atomic SUBMISSION_READY -> STARTED claim that
    guards against two concurrent APPROVE & APPLY requests racing on the
    SAME still-ready execution (before either has moved it past
    SUBMISSION_READY)."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-5"))
    execution = _prepare_ready_for_approval(job)

    first_claim = applications_approval._claim_ready_execution(execution["execution_id"])
    second_claim = applications_approval._claim_ready_execution(execution["execution_id"])

    assert first_claim is True
    assert second_claim is False


def test_bulk_approval_isolates_one_job_failure(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_a = ingest_and_process(_mock_job("simple", "mock-appr-bulk-a"))
    job_b = ingest_and_process(_mock_job("simple", "mock-appr-bulk-b"))
    _prepare_ready_for_approval(job_a)
    # job_b is deliberately left with no prepared execution -- its approval
    # attempt must fail without stopping job_a's from succeeding.

    result = applications_approval.approve_and_apply_bulk([job_a.id, job_b.id])

    by_job = {r["job_id"]: r for r in result.results}
    assert by_job[job_a.id]["ok"] is True
    assert by_job[job_b.id]["ok"] is False
    assert result.succeeded == 1
    assert result.failed == 1


def test_is_current_valid_detects_resume_and_answer_changes(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-6"))
    execution = _prepare_ready_for_approval(job)

    provider = provider_registry.get_application_provider(job)
    approval_id = applications_approval._record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )
    approval = applications_approval.get_latest_approval(execution["execution_id"])
    assert approval["approval_id"] == approval_id

    fresh_job = job.model_copy(deep=True)
    fresh_execution = dict(execution)
    valid, reasons = applications_approval.is_current_valid(fresh_job, fresh_execution, approval)
    assert valid is True
    assert reasons == []

    # Resume fingerprint changed since approval (e.g. a regenerate happened).
    fresh_execution["resume_artifact_hash"] = "different-hash-value"
    valid, reasons = applications_approval.is_current_valid(fresh_job, fresh_execution, approval)
    assert valid is False
    assert "resume changed since approval" in reasons

    # Candidate profile / answers changed since approval.
    other_profile = sample_profile.model_copy(deep=True)
    other_profile.contact.phone = "999-999-9999"
    save_profile(other_profile)
    valid, reasons = applications_approval.is_current_valid(fresh_job, dict(execution), approval)
    assert valid is False
    assert "candidate answers changed since approval" in reasons


def test_is_current_valid_detects_sponsorship_and_jd_change(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-7"))
    execution = _prepare_ready_for_approval(job)
    provider = provider_registry.get_application_provider(job)
    applications_approval._record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )
    approval = applications_approval.get_latest_approval(execution["execution_id"])

    from app.models import SponsorshipStatus

    downgraded = job.model_copy(deep=True)
    downgraded.sponsorship_status = SponsorshipStatus.LIKELY_SPONSOR
    valid, reasons = applications_approval.is_current_valid(downgraded, dict(execution), approval)
    assert valid is False
    assert "sponsorship status changed since approval" in reasons

    jd_changed = job.model_copy(deep=True)
    jd_changed.resume_jd_fingerprint = "some-other-fingerprint"
    valid, reasons = applications_approval.is_current_valid(jd_changed, dict(execution), approval)
    assert valid is False
    assert "job description changed since approval" in reasons


def test_approved_submit_permitted_rejects_unsupported_provider(monkeypatch):
    """Pure-function guard: no submission is ever unlocked for a provider
    that hasn't genuinely earned submission_supported=True, regardless of
    how the human-approved gate is reached. The durable-approval gate itself
    is stubbed out here (it has its own dedicated DB-backed tests below) so
    this test stays focused on the provider-capability check specifically."""
    from dataclasses import dataclass

    from app.applications.eligibility import EligibilityResult
    from app.models import EmploymentType, Job, SponsorshipStatus

    @dataclass
    class _FakeCaps:
        submission_supported: bool

    @dataclass
    class _FakeProvider:
        capabilities: _FakeCaps

    monkeypatch.setattr(applications_approval, "verify_durable_approval_for_submission",
                         lambda job, execution: (True, "stubbed ok"))

    job = Job(title="Backend Engineer", company="Acme", description="x",
              sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR)
    eligibility = EligibilityResult(enters_queue=True, auto_submit_eligible=False,
                                     employment_type=EmploymentType.FULL_TIME)
    provider = _FakeProvider(capabilities=_FakeCaps(submission_supported=False))
    from app.applications.models import ValidationResult

    validation = ValidationResult(ok=True, policy=AutomationPolicy.PERMITTED_AUTO)

    ok, reason = _approved_submit_permitted(job, eligibility, provider, validation, execution={})
    assert ok is False
    assert "no verified final-submission capability" in reason


def test_approved_submit_permitted_blocked_without_durable_approval(tmp_env):
    """Requirement 3: the boolean `approved=True` alone is never sufficient
    -- with no durable approval gate stubbed out this time, the very first
    check in `_approved_submit_permitted` must reject before any other
    condition is even evaluated, regardless of how favorable the rest of
    the state is."""
    from dataclasses import dataclass

    from app.applications.eligibility import EligibilityResult
    from app.applications.models import ValidationResult
    from app.models import EmploymentType, Job, SponsorshipStatus

    @dataclass
    class _FakeCaps:
        submission_supported: bool

    @dataclass
    class _FakeProvider:
        capabilities: _FakeCaps

    job = Job(title="Backend Engineer", company="Acme", description="x",
              sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR)
    eligibility = EligibilityResult(enters_queue=True, auto_submit_eligible=False,
                                     employment_type=EmploymentType.FULL_TIME)
    provider = _FakeProvider(capabilities=_FakeCaps(submission_supported=True))
    validation = ValidationResult(ok=True, policy=AutomationPolicy.PERMITTED_AUTO)

    # execution_id references nothing real -- get_latest_approval() legitimately
    # finds no row, matching "missing approval row" (requirement 5C).
    ok, reason = _approved_submit_permitted(job, eligibility, provider, validation,
                                             execution={"execution_id": "exec_does_not_exist"})
    assert ok is False
    assert "no durable approval record" in reason


def test_approve_and_apply_lands_on_approved_for_provider_without_submission_support(tmp_env, sample_profile):
    """The real-world Greenhouse case (spec section 8): form discovery/fill/
    validation genuinely work (live-verified, see
    app.applications.providers_greenhouse), but submission_supported is
    honestly False -- approval must never fake a submission for it. Uses
    the same httpx.MockTransport fixture pattern as
    tests/test_applications_providers_greenhouse.py (no live network)."""
    save_profile(sample_profile)

    fixture_payload = {
        "questions": [
            {"label": "First Name", "required": True,
             "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
            {"label": "Last Name", "required": True,
             "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
            {"label": "Email", "required": True,
             "fields": [{"name": "email", "type": "input_text", "values": []}]},
            {"label": "Resume/CV", "required": True,
             "fields": [{"name": "resume", "type": "input_file", "values": []}]},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture_payload)

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_provider = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id="gh-appr-1", company_identifier="acme", mode=ApplicationMode.ASSIST,
        ))
        execution = _prepare_ready_for_approval(job)
        assert execution["provider"] == "greenhouse"

        result = applications_approval.approve_and_apply(job.id)

        assert result.ok is True
        assert result.execution["status"] == ExecutionStatus.APPROVED.value
        assert result.execution["status"] != ExecutionStatus.APPLIED.value
        assert product_state.approved_for_submission(result.execution) is True
        assert product_state.confirmed(result.execution) is False

        approval = applications_approval.get_latest_approval(result.execution_id)
        assert approval["submission_capability"] == "UNSUPPORTED"

        from app.jobs_repo import get_job

        refreshed = get_job(job.id)
        assert refreshed.application_state.value == "APPROVED"
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original_provider


# --- Approval-safety correction regression tests --------------------------
# (requirements A-G: is_current_valid answers_version/form_fingerprint
# coverage, the durable server-side gate immediately before provider.submit(),
# and the claim-then-record ordering that prevents duplicate ACTIVE approval
# rows under simultaneous clicks.)

def test_is_current_valid_detects_answers_version_change(tmp_env, sample_profile):
    """Requirement A: a changed answers_version (the form was re-mapped/
    re-snapshotted with a different set of answered fields since approval)
    must invalidate, even though every other fingerprint is unchanged."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-20"))
    execution = _prepare_ready_for_approval(job)
    provider = provider_registry.get_application_provider(job)
    applications_approval._record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )
    approval = applications_approval.get_latest_approval(execution["execution_id"])

    unchanged = dict(execution)
    valid, reasons = applications_approval.is_current_valid(job, unchanged, approval)
    assert valid is True
    assert reasons == []

    drifted = dict(execution)
    drifted["answers_version"] = int(execution.get("answers_version") or 0) + 1
    valid, reasons = applications_approval.is_current_valid(job, drifted, approval)
    assert valid is False
    assert any("answers_version" in r for r in reasons)

    # Conservative in the other direction too: approved=0 (unknown at
    # approval time) but current is now a real, known non-zero value must
    # also invalidate -- never silently treat "became known" as still valid.
    unknown_at_approval = dict(approval)
    unknown_at_approval["answers_version"] = 0
    valid, reasons = applications_approval.is_current_valid(job, dict(execution), unknown_at_approval)
    if int(execution.get("answers_version") or 0) != 0:
        assert valid is False
        assert any("answers_version" in r for r in reasons)


def test_is_current_valid_detects_form_fingerprint_change(tmp_env, sample_profile):
    """Requirement B: a changed form_fingerprint (the ATS form itself
    shifted since approval -- new/removed/reordered fields) must
    invalidate."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-21"))
    execution = _prepare_ready_for_approval(job)
    provider = provider_registry.get_application_provider(job)
    applications_approval._record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )
    approval = applications_approval.get_latest_approval(execution["execution_id"])

    drifted = dict(execution)
    drifted["form_fingerprint"] = "a-different-form-fingerprint"
    valid, reasons = applications_approval.is_current_valid(job, drifted, approval)
    assert valid is False
    assert any("form_fingerprint" in r for r in reasons)

    # unknown ("") at approval -> now known/non-empty must also invalidate.
    unknown_at_approval = dict(approval)
    unknown_at_approval["form_fingerprint"] = ""
    if execution.get("form_fingerprint"):
        valid, reasons = applications_approval.is_current_valid(job, dict(execution), unknown_at_approval)
        assert valid is False
        assert any("form_fingerprint" in r for r in reasons)


def test_verify_durable_approval_blocks_missing_row(tmp_env, sample_profile):
    """Requirement C: no application_approvals row at all for this execution
    -- e.g. process_execution(approved=True) invoked directly, bypassing
    approve_and_apply() entirely -- must never be treated as permitted."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-22"))
    execution = _prepare_ready_for_approval(job)

    ok, reason = applications_approval.verify_durable_approval_for_submission(job, execution)
    assert ok is False
    assert "no durable approval record" in reason


def test_verify_durable_approval_blocks_non_active_status(tmp_env, sample_profile):
    """Defense in depth: even though nothing in this codebase currently sets
    application_approvals.status to anything but ACTIVE, the gate must
    genuinely check the column rather than assuming any row found is good."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-23"))
    execution = _prepare_ready_for_approval(job)
    provider = provider_registry.get_application_provider(job)
    approval_id = applications_approval._record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )
    from app.db import db_session

    with db_session() as conn:
        conn.execute("UPDATE application_approvals SET status = 'SUPERSEDED' WHERE approval_id = ?", (approval_id,))

    ok, reason = applications_approval.verify_durable_approval_for_submission(job, execution)
    assert ok is False
    assert "not ACTIVE" in reason


def test_verify_durable_approval_permits_unchanged_snapshot(tmp_env, sample_profile):
    """Requirement E: an unchanged exact snapshot (job identity, JD, resume,
    answers_version, profile, form_fingerprint, sponsorship, employment
    classification all still match) must be permitted -- the gate is a
    live-recomputed match check, not a trapdoor that always blocks."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-24"))
    execution = _prepare_ready_for_approval(job)
    provider = provider_registry.get_application_provider(job)
    assert provider.capabilities.submission_supported is True
    applications_approval._record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )

    ok, reason = applications_approval.verify_durable_approval_for_submission(job, dict(execution))
    assert ok is True
    assert "verified current" in reason


def test_stale_approval_never_reaches_provider_submit(tmp_env, sample_profile):
    """Requirement D: a stale durable approval (its stored form_fingerprint
    no longer matches what the pipeline would actually submit) must be
    caught by the server-side gate BEFORE provider.submit() is ever called
    -- even when the execution row has been legitimately claimed (STARTED)
    and approved=True is passed straight into process_execution(), proving
    the boolean parameter alone is never sufficient (requirement 4)."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-25"))
    execution = _prepare_ready_for_approval(job)
    provider = provider_registry.get_application_provider(job)
    approval_id = applications_approval._record_approval_row(
        job, execution, provider_submission_supported=provider.capabilities.submission_supported,
    )
    from app.db import db_session

    with db_session() as conn:
        conn.execute(
            "UPDATE application_approvals SET form_fingerprint = ? WHERE approval_id = ?",
            ("stale-fingerprint-does-not-match-current-form", approval_id),
        )

    assert applications_approval._claim_ready_execution(execution["execution_id"]) is True

    submit_calls = []
    original_submit = MockATSProvider.submit

    def counting_submit(self, job_arg, form, draft):
        submit_calls.append(1)
        return original_submit(self, job_arg, form, draft)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(MockATSProvider, "submit", counting_submit)
        result = process_execution(execution["execution_id"], approved=True)

    assert not submit_calls, "provider.submit() must never be called for a stale approval"
    assert result["status"] == ExecutionStatus.APPROVED.value
    assert result["status"] != ExecutionStatus.APPLIED.value
    assert result.get("user_action_reason") and "approval is stale" in result["user_action_reason"]


def test_concurrent_double_click_causes_at_most_one_provider_submit(tmp_env, sample_profile):
    """Requirements F and G, exercised together under real concurrent
    threads (not a mocked lock), mirroring this project's existing
    concurrency-test convention (tests/test_applications_concurrency.py):

    F. N simultaneous APPROVE & APPLY clicks for the same job must result in
       provider.submit() being invoked at most once.
    G. The claim-then-record ordering in approve_and_apply() (requirement 6)
       must never let two simultaneous clicks each insert their own ACTIVE
       application_approvals row -- exactly one durable row survives."""
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("simple", "mock-appr-26"))
    _prepare_ready_for_approval(job)

    submit_calls: list = []
    submit_lock = threading.Lock()
    original_submit = MockATSProvider.submit

    def counting_submit(self, job_arg, form, draft):
        with submit_lock:
            submit_calls.append(1)
        return original_submit(self, job_arg, form, draft)

    results: list = []
    results_lock = threading.Lock()

    def worker():
        r = applications_approval.approve_and_apply(job.id)
        with results_lock:
            results.append(r)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(MockATSProvider, "submit", counting_submit)
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(submit_calls) == 1, f"expected exactly one provider.submit() call, got {len(submit_calls)}"

    applied = [r for r in results if r.execution and r.execution.get("status") == ExecutionStatus.APPLIED.value]
    assert len(applied) == 1

    approvals = applications_approval.list_approvals_for_job(job.id)
    assert len(approvals) == 1, f"expected exactly one durable approval row, got {len(approvals)}"
    assert approvals[0]["status"] == "ACTIVE"
