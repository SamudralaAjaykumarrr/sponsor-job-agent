"""Greenhouse Verified Submission Contract V1: the pure, read-only pre-submit
contract (`app.applications.greenhouse_submit_contract`). No Playwright here
-- every scenario below is exercised through the real, unmodified executor/
approval pipeline against a mocked Greenhouse Job Board API (httpx.MockTransport,
never a live network call), matching tests/test_approval.py's own established
pattern for this exact provider."""

import httpx
import pytest

from app.applications import approval as applications_approval
from app.applications import provider_registry
from app.applications.executor import process_execution, queue_application
from app.applications.greenhouse_submit_contract import BrowserEvidence, build_submit_contract
from app.applications.models import ExecutionStatus
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.candidate.profile import save_profile
from app.jobs_repo import update_job
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process
from app import config

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)

MINIMAL_PAYLOAD = {
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

UNANSWERABLE_PAYLOAD = {
    "questions": MINIMAL_PAYLOAD["questions"] + [
        {"label": "Which internal Acme initiative most closely matches your background?", "required": True,
         "fields": [{"name": "question_99001", "type": "input_text", "values": []}]},
    ],
}

# Real live shape: a conditional "if you answered Yes, explain" follow-up,
# OPTIONAL, unmapped, and genuinely blank (the parent Yes/No question was
# answered "No") -- must never permanently block contract.ready.
OPTIONAL_HIGH_RISK_PAYLOAD = {
    "questions": MINIMAL_PAYLOAD["questions"] + [
        {"label": "If you answered \"Yes\" to the above question, please provide additional information here:",
         "required": False, "fields": [{"name": "question_99002", "type": "input_text", "values": []}]},
    ],
}


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)


def _install_mock_greenhouse(payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    return original


def _install_mock_greenhouse_status(status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        if status_code >= 400:
            return httpx.Response(status_code, text="gone")
        return httpx.Response(status_code, json=MINIMAL_PAYLOAD)

    mocked_client = httpx.Client(transport=httpx.MockTransport(handler))
    original = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(client=mocked_client)
    return original


def _restore(original):
    provider_registry._PROVIDERS["greenhouse"] = original


def _make_job(external_job_id: str = "gh-c-1") -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
        external_job_id=external_job_id, company_identifier="acme", mode=ApplicationMode.ASSIST,
    )


def _drive_to_approved(job: Job) -> str:
    """Runs the real, unmodified pipeline to SUBMISSION_READY, then approves
    -- landing on APPROVED (submission_supported stays False for greenhouse)
    with a durable, current ACTIVE approval row. Returns the execution_id."""
    result = queue_application(job.id, mode="ASSIST")
    assert result.queued, result.reason
    execution = process_execution(result.execution_id)
    assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value, execution
    approved = applications_approval.approve_and_apply(job.id)
    assert approved.ok, approved.reason
    assert approved.execution["status"] == ExecutionStatus.APPROVED.value
    return result.execution_id


def test_ready_contract_when_everything_is_current(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-ready"))
        execution_id = _drive_to_approved(job)

        contract = build_submit_contract(job.id)
        assert contract is not None
        assert contract.execution_id == execution_id
        assert contract.identity.recognized is True
        assert contract.already_attempted is False
        for step in contract.steps[:6]:
            assert step.status.value == "PASSED", (step.number, step.name, step.detail)
        assert contract.ready is True
        assert contract.blocking_reasons == []
    finally:
        _restore(original)


def test_missing_required_field_blocks_readiness(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(UNANSWERABLE_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-missing"))
        # This job never reaches SUBMISSION_READY (the unanswerable question
        # blocks validation) -- exercise the contract against the execution
        # in whatever state it actually lands on, which is the honest
        # "required fields complete" == False case either way.
        result = queue_application(job.id, mode="ASSIST")
        assert result.queued
        process_execution(result.execution_id)

        contract = build_submit_contract(job.id)
        assert contract is not None
        required_step = next(s for s in contract.steps if s.name == "required_fields_complete")
        assert required_step.status.value == "FAILED"
        assert contract.ready is False
        assert any("required field" in r for r in contract.blocking_reasons)
    finally:
        _restore(original)


def test_optional_unmapped_high_risk_field_never_permanently_blocks_readiness(tmp_env, sample_profile):
    """Real live bug: an OPTIONAL, unmapped high-risk question left
    genuinely blank (no generic mapping, no evidence -- and none could ever
    apply, since it's a conditional follow-up whose parent question was
    answered "No") used to fail required_fields_complete forever, even
    though it is not required and the browser session's own readiness
    check correctly resolves the identical form."""
    save_profile(sample_profile)
    original = _install_mock_greenhouse(OPTIONAL_HIGH_RISK_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-optional-high-risk"))
        execution_id = _drive_to_approved(job)

        contract = build_submit_contract(job.id)
        assert contract is not None
        assert contract.execution_id == execution_id
        required_step = next(s for s in contract.steps if s.name == "required_fields_complete")
        assert required_step.status.value == "PASSED", required_step.detail
        assert contract.ready is True
        assert contract.blocking_reasons == []
    finally:
        _restore(original)


def test_stale_approval_blocks_readiness(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-stale-appr"))
        _drive_to_approved(job)

        # Something material changed since approval: sponsorship status.
        from app.models import SponsorshipStatus

        update_job(job.id, sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR)

        contract = build_submit_contract(job.id)
        approval_step = next(s for s in contract.steps if s.name == "approved_answer_set")
        assert approval_step.status.value == "FAILED"
        assert contract.ready is False
        assert any("stale" in r.lower() or "changed" in r.lower() for r in contract.blocking_reasons)
    finally:
        _restore(original)


def test_stale_form_fingerprint_blocks_readiness(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-stale-form"))
        _drive_to_approved(job)

        # The employer changes the form after approval -- swap the mocked
        # payload for a genuinely different schema, producing a different
        # fingerprint on the next (re)discovery.
        changed_payload = {
            "questions": MINIMAL_PAYLOAD["questions"] + [
                {"label": "LinkedIn URL", "required": False,
                 "fields": [{"name": "linkedin", "type": "input_text", "values": []}]},
            ],
        }
        provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(
            client=httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=changed_payload)))
        )

        contract = build_submit_contract(job.id)
        form_step = next(s for s in contract.steps if s.name == "current_form_fingerprint")
        assert form_step.status.value == "FAILED"
        assert "stale form" in form_step.detail or "changed" in form_step.detail
        assert contract.ready is False
    finally:
        _restore(original)


def test_identity_not_recognized_blocks_readiness(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
        # No external_job_id/company_identifier/greenhouse URL at all.
    )
    from app.jobs_repo import insert_job

    job_id = insert_job(job)

    contract = build_submit_contract(job_id)
    assert contract is not None
    identity_step = contract.steps[0]
    assert identity_step.name == "canonical_identity"
    assert identity_step.status.value == "FAILED"
    assert contract.ready is False


def test_expired_job_blocks_readiness(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-expired"))
        _drive_to_approved(job)
    finally:
        _restore(original)

    original = _install_mock_greenhouse_status(404)
    try:
        contract = build_submit_contract(job.id)
        active_step = next(s for s in contract.steps if s.name == "job_still_active")
        assert active_step.status.value == "FAILED"
        assert contract.ready is False
    finally:
        _restore(original)


def test_no_execution_yet_reports_honest_not_ready_contract(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-no-exec"))
        contract = build_submit_contract(job.id)
        assert contract is not None
        assert contract.execution_id == ""
        assert contract.ready is False
        assert len(contract.steps) == 8
    finally:
        _restore(original)


def test_missing_job_returns_none():
    assert build_submit_contract(999999999) is None


def test_steps_seven_eight_are_not_yet_checked_without_browser_evidence(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-no-browser-evidence"))
        _drive_to_approved(job)
        contract = build_submit_contract(job.id)
        submit_control_step = next(s for s in contract.steps if s.name == "submit_control_unique")
        claim_step = next(s for s in contract.steps if s.name == "submit_once_claim")
        assert submit_control_step.status.value == "NOT_YET_CHECKED"
        assert claim_step.status.value == "NOT_YET_CHECKED"
        # NOT_YET_CHECKED steps never count as blocking.
        assert contract.ready is True
    finally:
        _restore(original)


def test_browser_evidence_folds_genuine_facts_into_steps_seven_eight(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-browser-evidence"))
        _drive_to_approved(job)

        unique_control = BrowserEvidence(submit_control_unique=True, submit_control_detail="one control found")
        contract = build_submit_contract(job.id, browser_evidence=unique_control)
        submit_control_step = next(s for s in contract.steps if s.name == "submit_control_unique")
        claim_step = next(s for s in contract.steps if s.name == "submit_once_claim")
        assert submit_control_step.status.value == "PASSED"
        assert claim_step.status.value == "PASSED"
        assert contract.ready is True

        captcha = BrowserEvidence(captcha_present=True)
        contract_captcha = build_submit_contract(job.id, browser_evidence=captcha)
        submit_control_step = next(s for s in contract_captcha.steps if s.name == "submit_control_unique")
        assert submit_control_step.status.value == "FAILED"
        assert contract_captcha.ready is False

        ambiguous = BrowserEvidence(submit_control_unique=False, submit_control_detail="two controls found")
        contract_ambiguous = build_submit_contract(job.id, browser_evidence=ambiguous)
        submit_control_step = next(s for s in contract_ambiguous.steps if s.name == "submit_control_unique")
        assert submit_control_step.status.value == "FAILED"
        assert contract_ambiguous.ready is False
    finally:
        _restore(original)


def test_already_attempted_claim_fails_step_eight(tmp_env, sample_profile):
    save_profile(sample_profile)
    original = _install_mock_greenhouse(MINIMAL_PAYLOAD)
    try:
        job = ingest_and_process(_make_job("gh-c-already-attempted"))
        execution_id = _drive_to_approved(job)

        from app.applications import greenhouse_submit_claim as claim

        claim.acquire_submit_claim(execution_id, job.id)

        contract = build_submit_contract(job.id, browser_evidence=BrowserEvidence(submit_control_unique=True))
        assert contract.already_attempted is True
        claim_step = next(s for s in contract.steps if s.name == "submit_once_claim")
        assert claim_step.status.value == "FAILED"
        assert contract.ready is False
    finally:
        _restore(original)
