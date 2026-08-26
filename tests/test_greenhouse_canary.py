"""Greenhouse Verified Submission Contract V1: the controlled canary gate
(`app.applications.greenhouse_canary`). Every test here proves a refusal --
no test in this project may enable GREENHOUSE_SUBMIT_CANARY_ENABLED and
actually run the engine; tests/test_greenhouse_submit_engine.py exercises the
engine directly against local fixtures instead."""

import httpx
import pytest

from app import config
from app.applications import approval as applications_approval
from app.applications import greenhouse_canary, provider_registry
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.candidate.profile import save_profile
from app.jobs_repo import insert_job
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

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


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)
    # Explicit, defense-in-depth: this whole test module must never itself
    # flip the canary flag on.
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", False)


def test_disabled_by_default(tmp_env):
    result = greenhouse_canary.check_gates(1, confirm=True)
    assert result.allowed is False
    assert "GREENHOUSE_SUBMIT_CANARY_ENABLED" in result.reason


def test_enabled_but_no_confirm_is_refused(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", True)
    result = greenhouse_canary.check_gates(1, confirm=False)
    assert result.allowed is False
    assert "confirm" in result.reason


def test_run_raises_when_disabled(tmp_env):
    with pytest.raises(greenhouse_canary.CanaryDisabled):
        greenhouse_canary.run_greenhouse_submit_canary(1, confirm=True)


def test_run_raises_when_confirm_missing_even_if_enabled(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", True)
    with pytest.raises(greenhouse_canary.CanaryDisabled):
        greenhouse_canary.run_greenhouse_submit_canary(1)  # confirm defaults to False


def test_unrecognized_job_is_refused(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", True)
    job_id = insert_job(Job(title="X", company="Acme", location="Remote", description="d", provider="greenhouse"))
    result = greenhouse_canary.check_gates(job_id, confirm=True)
    assert result.allowed is False
    assert "identity" in result.reason.lower() or "recognized" in result.reason.lower()


def test_non_greenhouse_job_is_refused(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", True)
    job_id = insert_job(Job(title="X", company="Acme", location="Remote", description="d", provider="lever"))
    result = greenhouse_canary.check_gates(job_id, confirm=True)
    assert result.allowed is False
    assert "greenhouse" in result.reason.lower()


def test_missing_job_is_refused(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", True)
    result = greenhouse_canary.check_gates(987654321, confirm=True)
    assert result.allowed is False


def test_no_active_execution_is_refused(tmp_env, monkeypatch, sample_profile):
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", True)
    save_profile(sample_profile)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MINIMAL_PAYLOAD)

    original = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id="gh-canary-noexec", company_identifier="acme", mode=ApplicationMode.ASSIST,
        ))
        result = greenhouse_canary.check_gates(job.id, confirm=True)
        assert result.allowed is False
        assert "execution" in result.reason.lower()
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original


def test_unapproved_execution_is_refused(tmp_env, monkeypatch, sample_profile):
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", True)
    save_profile(sample_profile)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MINIMAL_PAYLOAD)

    original = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        job = ingest_and_process(Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
            external_job_id="gh-canary-unapproved", company_identifier="acme", mode=ApplicationMode.ASSIST,
        ))
        result = queue_application(job.id, mode="ASSIST")
        execution = process_execution(result.execution_id)
        assert execution["status"] == ExecutionStatus.SUBMISSION_READY.value

        gate = greenhouse_canary.check_gates(job.id, confirm=True)
        assert gate.allowed is False
        assert "authorization" in gate.reason.lower() or "approval" in gate.reason.lower()
    finally:
        provider_registry._PROVIDERS["greenhouse"] = original


def test_canary_never_imported_by_any_scheduler_module():
    """Structural guard: this module must never be reachable from a
    background/scheduled loop -- only an explicit operator action (the CLI)
    may ever call it."""
    import ast
    from pathlib import Path

    scheduler_modules = [
        Path("app/applications/background_scheduler.py"),
        Path("app/applications/scheduler.py"),
        Path("app/applications/reconcile_worker.py"),
        Path("app/agent/orchestrator.py"),
    ]
    for path in scheduler_modules:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "greenhouse_canary" not in alias.name, f"{path} imports greenhouse_canary"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "greenhouse_canary" not in node.module, f"{path} imports from greenhouse_canary"
