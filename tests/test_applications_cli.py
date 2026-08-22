"""CLAUDE.md Phase 8 section 57: application executor CLI."""

import json

import pytest

from app import config
from app.applications.cli import main as cli_main
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


@pytest.fixture(autouse=True)
def _executor_enabled(monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)


@pytest.fixture
def job_id(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id="cli-1", provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    ))
    return job.id


def test_cli_validate(job_id, capsys):
    rc = cli_main(["validate", str(job_id)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "enters_queue=True" in out


def test_cli_prepare_auto_permitted(job_id, capsys):
    rc = cli_main(["prepare", str(job_id), "--mode", "AUTO_PERMITTED"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "status: APPLIED" in out


def test_cli_status(job_id, capsys):
    cli_main(["prepare", str(job_id), "--mode", "AUTO_PERMITTED"])
    rc = cli_main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "applications_confirmed" in out


def test_cli_doctor_clean(job_id, capsys):
    cli_main(["prepare", str(job_id), "--mode", "AUTO_PERMITTED"])
    rc = cli_main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 serious issue" in out
