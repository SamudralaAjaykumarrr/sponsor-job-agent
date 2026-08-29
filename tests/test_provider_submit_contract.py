"""Canary Candidate Pool Expansion + Multi-Provider Readiness V1: the generic
multi-provider submit contract (`app.applications.provider_submit_contract`)
and its readiness classification (`app.applications.canary_readiness.
provider_readiness`). No Playwright, no real network -- mocked Lever/Ashby
public APIs via httpx.MockTransport, same pattern as
tests/test_greenhouse_submit_contract.py. Never asserts submission_supported
becomes True for any real provider."""

import httpx
import pytest

from app.applications import approval as applications_approval
from app.applications import provider_registry
from app.applications import repo
from app.applications.canary_readiness import ReadinessLevel, provider_readiness
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.provider_submit_claim import acquire_submit_claim
from app.applications.provider_submit_contract import StepStatus, build_submit_contract
from app.applications.providers_ashby import AshbyApplicationProvider
from app.applications.providers_lever import LeverApplicationProvider
from app.candidate.profile import save_profile
from app.jobs_repo import update_job
from app.models import ApplicationMode, Job, SponsorshipStatus
from app.pipeline import ingest_and_process
from app import config

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)

LEVER_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ASHBY_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)


def _install_mock_lever(status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status_code >= 400:
            return httpx.Response(status_code, text="gone")
        return httpx.Response(status_code, json={"id": LEVER_UUID, "text": "Backend Engineer"})

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original = provider_registry._PROVIDERS["lever"]
    provider_registry._PROVIDERS["lever"] = LeverApplicationProvider(client=mocked_client)
    return original, "lever"


def _install_mock_ashby(status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status_code >= 400:
            return httpx.Response(status_code, text="gone")
        return httpx.Response(200, json={"jobs": [{"id": ASHBY_UUID, "title": "Backend Engineer"}]})

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original = provider_registry._PROVIDERS["ashby"]
    provider_registry._PROVIDERS["ashby"] = AshbyApplicationProvider(client=mocked_client)
    return original, "ashby"


def _restore(original, provider_name: str):
    provider_registry._PROVIDERS[provider_name] = original


def _make_job(provider: str, external_job_id: str, company_identifier: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider=provider,
        external_job_id=external_job_id, company_identifier=company_identifier, mode=ApplicationMode.ASSIST,
    )


def _drive_to_approved(job: Job) -> str:
    """No provider covered by this file publishes a public form-question API
    (confirmed by reading providers_lever.py/providers_ashby.py:
    discover_form() always returns None), so the ordinary form-driven
    executor pipeline never reaches SUBMISSION_READY on its own for these
    jobs -- it correctly lands on NEEDS_USER_ACTION -- exactly the honest
    gap this whole feature reports (the real path onward is browser-assist,
    out of scope here since this suite opens no browser). To exercise the
    contract's approval/document steps (4-5, fully provider-neutral) against
    a REAL, internally-consistent approval row without a browser, this
    test-only helper runs the real pipeline once to populate genuine
    answers_version/resume_artifact_hash on the execution, force-transitions
    it to APPROVED, then records an approval row directly from THOSE SAME
    current values via the same `_record_approval_row()` `approve_and_apply()`
    itself uses -- never calling `process_execution()` again afterward, which
    would otherwise recompute fresh values and immediately (and correctly)
    invalidate an approval recorded a moment earlier against stale ones."""
    result = queue_application(job.id, mode="ASSIST")
    assert result.queued, result.reason
    process_execution(result.execution_id)
    execution = repo.get_active_execution_for_job(job.id)
    repo.update_execution(result.execution_id, job.id, ExecutionStatus.APPROVED)
    execution = repo.get_active_execution_for_job(job.id)
    applications_approval._record_approval_row(job, execution, provider_submission_supported=False)
    return result.execution_id


# --------------------------------------------------------------------------
# provider_submit_contract.build_submit_contract
# --------------------------------------------------------------------------

def test_lever_contract_checkable_steps_pass_uncheckable_steps_not_yet_checked(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        execution_id = _drive_to_approved(job)

        contract = build_submit_contract("lever", job.id)
        assert contract is not None
        assert contract.execution_id == execution_id
        assert contract.identity_recognized is True

        by_name = {s.name: s for s in contract.steps}
        assert by_name["canonical_identity"].status == StepStatus.PASSED
        assert by_name["job_still_active"].status == StepStatus.PASSED, by_name["job_still_active"].detail
        assert by_name["approved_answer_set"].status == StepStatus.PASSED
        assert by_name["approved_documents"].status == StepStatus.PASSED

        # No public question-schema API exists for Lever -- honestly
        # NOT_YET_CHECKED, never FAILED (would wrongly imply a real check
        # ran and found something broken) and never PASSED (would fake
        # capability this provider doesn't have).
        assert by_name["current_form_fingerprint"].status == StepStatus.NOT_YET_CHECKED
        assert by_name["required_fields_complete"].status == StepStatus.NOT_YET_CHECKED
        assert by_name["submit_control_unique"].status == StepStatus.NOT_YET_CHECKED
        assert by_name["submit_once_claim"].status == StepStatus.NOT_YET_CHECKED
    finally:
        _restore(original, name)


def test_ashby_contract_checkable_steps_pass(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_ashby()
    try:
        job = ingest_and_process(_make_job("ashby", ASHBY_UUID, "acme"))
        _drive_to_approved(job)

        contract = build_submit_contract("ashby", job.id)
        assert contract is not None
        by_name = {s.name: s for s in contract.steps}
        assert by_name["canonical_identity"].status == StepStatus.PASSED
        assert by_name["job_still_active"].status == StepStatus.PASSED
        assert by_name["approved_answer_set"].status == StepStatus.PASSED
        assert by_name["approved_documents"].status == StepStatus.PASSED
        assert by_name["current_form_fingerprint"].status == StepStatus.NOT_YET_CHECKED
    finally:
        _restore(original, name)


def test_contract_returns_none_for_nonexistent_job():
    assert build_submit_contract("lever", 999_999_999) is None


def test_contract_returns_none_when_provider_mismatch(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        # Asking for the ashby contract on a lever job must never silently
        # evaluate it against the wrong provider's identity rules.
        assert build_submit_contract("ashby", job.id) is None
    finally:
        _restore(original, name)


def test_no_active_execution_fails_every_execution_dependent_step(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        contract = build_submit_contract("lever", job.id)
        assert contract is not None
        assert contract.execution_id == ""
        by_name = {s.name: s for s in contract.steps}
        assert by_name["job_still_active"].status == StepStatus.FAILED
        assert by_name["approved_answer_set"].status == StepStatus.FAILED
        assert contract.ready is False
    finally:
        _restore(original, name)


def test_stale_approval_blocks_readiness(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        _drive_to_approved(job)
        update_job(job.id, sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR)

        contract = build_submit_contract("lever", job.id)
        approval_step = next(s for s in contract.steps if s.name == "approved_answer_set")
        assert approval_step.status == StepStatus.FAILED
        assert contract.ready is False
    finally:
        _restore(original, name)


def test_job_no_longer_active_fails_liveness_step(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever(status_code=404)
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        contract = build_submit_contract("lever", job.id)
        assert contract is not None
        liveness = next(s for s in contract.steps if s.name == "job_still_active")
        assert liveness.status == StepStatus.NOT_YET_CHECKED or liveness.status == StepStatus.FAILED
    finally:
        _restore(original, name)


def test_submit_once_claim_already_attempted_reported(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        execution_id = _drive_to_approved(job)

        claim = acquire_submit_claim("lever", execution_id, job.id, claimed_by="test")
        assert claim.acquired is True

        contract = build_submit_contract("lever", job.id)
        assert contract.already_attempted is True
        from app.applications.provider_submit_contract import BrowserEvidence
        contract2 = build_submit_contract("lever", job.id, browser_evidence=BrowserEvidence(submit_control_unique=True))
        claim_step = next(s for s in contract2.steps if s.name == "submit_once_claim")
        assert claim_step.status == StepStatus.FAILED

        # Never a second claim once one is already held.
        second = acquire_submit_claim("lever", execution_id, job.id, claimed_by="test-2")
        assert second.acquired is False
    finally:
        _restore(original, name)


# --------------------------------------------------------------------------
# canary_readiness.provider_readiness
# --------------------------------------------------------------------------

def test_provider_readiness_infrastructure_ready_for_lever(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        _drive_to_approved(job)

        report = provider_readiness("lever", job.id)
        assert report.level == ReadinessLevel.INFRASTRUCTURE_READY, report.explanation
        assert report.submission_supported is False
    finally:
        _restore(original, name)


def test_provider_readiness_not_ready_without_execution(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        report = provider_readiness("lever", job.id)
        assert report.level == ReadinessLevel.NO_ACTIVE_EXECUTION
    finally:
        _restore(original, name)


def test_provider_readiness_unsupported_provider_reports_not_ready():
    report = provider_readiness("smartrecruiters", 1)
    assert report.level == ReadinessLevel.NOT_READY
    assert "not yet a supported provider" in report.explanation


def test_provider_readiness_never_sets_submission_supported_true(tmp_env, sample_profile):
    save_profile(sample_profile)
    original, name = _install_mock_lever()
    try:
        job = ingest_and_process(_make_job("lever", LEVER_UUID, "acme"))
        _drive_to_approved(job)
        report = provider_readiness("lever", job.id)
        assert report.submission_supported is False
        from app.applications.provider import ApplicationProvider  # noqa: F401
        from app.applications.provider_registry import get_application_provider
        assert get_application_provider(job).capabilities.submission_supported is False
    finally:
        _restore(original, name)
