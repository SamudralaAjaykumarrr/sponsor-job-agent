"""CLAUDE.md Phase 8 section 61: two workers must never submit the same
application simultaneously. Exercises the actual atomic guard
(application_executions(job_id) WHERE active=1 partial unique index) under
real concurrent threads sharing one SQLite connection pool -- not a mocked
lock."""

import json
import threading

import pytest

from app import config
from app.applications import repo as applications_repo
from app.applications.executor import queue_application
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


@pytest.fixture
def profile_saved(tmp_env, sample_profile):
    save_profile(sample_profile)
    return sample_profile


def test_concurrent_queue_application_never_creates_two_active_executions(profile_saved):
    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id="concurrency-1", provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    ))

    results = []
    errors = []

    def worker():
        try:
            results.append(queue_application(job.id, mode="ASSIST"))
        except Exception as exc:  # pragma: no cover -- would indicate a real bug
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert all(r.queued for r in results)
    execution_ids = {r.execution_id for r in results}
    assert len(execution_ids) == 1, f"expected exactly one execution_id, got {execution_ids}"

    active_count = len(applications_repo.list_executions_for_job(job.id))
    assert active_count == 1
