"""CLAUDE.md Phase 6 sections 35-36: structured JSON logging and correlation
id propagation from a poll attempt through to the stored job row."""

import json
import logging

import httpx

from app.observability.logging_config import StructuredFormatter, _STRUCTURED_FIELDS
from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import repo as workers_repo
from app.workers.runner import Worker

GREENHOUSE_OK = {"jobs": [
    {"id": 111, "title": "Backend Software Engineer", "location": {"name": "Remote - US"},
     "content": "We sponsor H-1B. Python FastAPI backend.",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/111", "updated_at": "2026-08-21T10:00:00Z",
     "departments": [{"name": "Engineering"}]},
]}


def _seed() -> int:
    return registry_repo.insert_entry(
        CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme")
    )


def test_structured_formatter_emits_valid_json_with_allowlisted_fields():
    formatter = StructuredFormatter()
    record = logging.LogRecord(
        name="workers.runner", level=logging.INFO, pathname=__file__, lineno=1,
        msg="poll attempt succeeded", args=(), exc_info=None,
    )
    record.worker_id = "w1"
    record.attempt_id = "abc123"
    record.provider = "greenhouse"
    record.candidate_email = "should-not-appear@example.com"  # not in the allowlist

    line = formatter.format(record)
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["component"] == "workers.runner"
    assert data["worker_id"] == "w1"
    assert data["attempt_id"] == "abc123"
    assert data["provider"] == "greenhouse"
    assert "candidate_email" not in data
    assert "should-not-appear" not in line


def test_structured_fields_allowlist_has_no_pii_field_names():
    pii_markers = ("email", "phone", "resume", "password", "ssn", "dob")
    for field in _STRUCTURED_FIELDS:
        assert not any(marker in field.lower() for marker in pii_markers), field


def test_correlation_id_flows_from_attempt_to_stored_job(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    _seed()

    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = workers_repo.list_recent_attempts(limit=10)
    assert len(attempts) == 1
    attempt_id = attempts[0]["attempt_id"]

    from app.jobs_repo import list_jobs

    jobs = list_jobs({})
    assert len(jobs) == 1
    assert jobs[0].correlation_id == attempt_id
