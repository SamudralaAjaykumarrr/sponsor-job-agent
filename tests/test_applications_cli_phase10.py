"""CLAUDE.md Phase 10: browser-assist CLI commands (python -m
app.applications.cli browser-*)."""

import json

import pytest

from app import config
from app.applications import browser_assist, browser_runtime, browser_session
from app.applications import repo as executions_repo
from app.applications.cli import main as cli_main
from app.candidate.profile import save_profile
from app.jobs_repo import insert_job
from app.models import ApplicationState, Job, SponsorshipStatus


@pytest.fixture(autouse=True)
def _enabled(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    return tmp_env


def _job_and_execution(tmp_env, sample_profile) -> tuple[Job, str]:
    save_profile(sample_profile)
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description="Full-time role. H-1B sponsorship is available.", employment_type="full_time",
        sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, technical_match_score=80.0,
        application_state=ApplicationState.READY_TO_APPLY, provider="never_configured", canonical_url="https://x/1",
        url="https://x/1",
    )
    job_id = insert_job(job)
    job_dir = tmp_env["output_dir"] / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "resume.pdf").write_bytes(b"%PDF fake")
    (job_dir / "resume.docx").write_bytes(b"fake")
    (job_dir / "application_answers.json").write_text(json.dumps({
        "full_name": "Test Candidate", "email": "t@example.com", "phone": "555", "do_you_require_sponsorship": "No",
    }))
    from app.jobs_repo import update_job, get_job

    update_job(job_id, resume_pdf_path=str(job_dir / "resume.pdf"), resume_docx_path=str(job_dir / "resume.docx"),
               application_answers_path=str(job_dir / "application_answers.json"))
    execution_id = executions_repo.create_execution(job_id, provider="never_configured", mode="ASSIST")
    return get_job(job_id), execution_id


def test_browser_start_and_status_and_list(tmp_env, sample_profile, monkeypatch, capsys):
    job, execution_id = _job_and_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: browser_runtime.DiscoveryOutcome(
        pause_reason=None, current_url="https://x/1",
        fields=[{"index": 0, "label": "Full Name", "name": "full_name", "type": "text", "required": True, "choices": []}],
        fingerprint="fp1", submit_button={"text": "Submit"},
    ))
    monkeypatch.setattr(browser_runtime, "fill_fields",
                         lambda *a, **k: browser_runtime.FillOutcome(filled=["Full Name"]))

    rc = cli_main(["browser-start", execution_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "created=True" in out

    rc = cli_main(["browser-status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ready_for_submit: 1" in out

    rc = cli_main(["browser-list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "job=" in out


def test_browser_start_fails_honestly_for_non_eligible_job(tmp_env, sample_profile, capsys):
    save_profile(sample_profile)
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", description="Contract role.",
        employment_type="contract", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        application_state=ApplicationState.READY_TO_APPLY,
    )
    job_id = insert_job(job)
    execution_id = executions_repo.create_execution(job_id, provider="never_configured", mode="ASSIST")

    rc = cli_main(["browser-start", execution_id])
    assert rc == 1
    out = capsys.readouterr().out
    assert "created=False" in out


def test_browser_close_and_reconcile_commands(tmp_env, sample_profile, monkeypatch, capsys):
    job, execution_id = _job_and_execution(tmp_env, sample_profile)
    monkeypatch.setattr(browser_runtime, "open_session", lambda *a, **k: browser_runtime.DiscoveryOutcome(
        pause_reason=None, current_url="https://x/1", fields=[], fingerprint="fp1",
        submit_button={"text": "Submit"},
    ))
    monkeypatch.setattr(browser_runtime, "fill_fields", lambda *a, **k: browser_runtime.FillOutcome())

    result = browser_assist.start_session(execution_id)
    session_id = result["session"]["session_id"]

    monkeypatch.setattr(browser_runtime, "is_live", lambda sid: False)
    rc = cli_main(["browser-reconcile", session_id])
    assert rc == 1  # not live -> outcome unknown, honest failure exit code

    rc = cli_main(["browser-close", session_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CLOSED" in out
