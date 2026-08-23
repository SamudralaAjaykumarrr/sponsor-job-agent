"""CLAUDE.md Phase 8 section 61: two workers must never submit the same
application simultaneously. Exercises the actual atomic guard
(application_executions(job_id) WHERE active=1 partial unique index) under
real concurrent threads sharing one SQLite connection pool -- not a mocked
lock."""

import json
import threading

import pytest

from app import config
from app.applications import queue as app_queue
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


def test_two_workers_never_claim_the_same_execution_batch(profile_saved):
    """CLAUDE.md Phase 9 section 4/CLAUDE.md mission 'duplicate worker':
    app.applications.queue.claim_execution_batch is the atomic guard behind
    the application worker fleet -- multiple worker identities racing to
    claim from the same pool of QUEUED executions must partition it exactly,
    with zero executions claimed by more than one worker."""
    jobs = [
        ingest_and_process(Job(
            title="Backend Software Engineer", company=f"DupCo{i}", location="Remote - US",
            description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
            external_job_id=f"dup-worker-{i}", provider_metadata=json.dumps({"mock_scenario": "simple"}),
            mode=ApplicationMode.ASSIST,
        ))
        for i in range(20)
    ]
    execution_ids = [queue_application(j.id, mode="ASSIST").execution_id for j in jobs]
    assert all(execution_ids)

    results: dict[str, list[str]] = {}
    lock = threading.Lock()

    def worker(worker_id: str) -> None:
        claimed = app_queue.claim_execution_batch(worker_id=worker_id, limit=20, lease_seconds=60)
        with lock:
            results[worker_id] = [c["execution_id"] for c in claimed]

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_claimed = [eid for ids in results.values() for eid in ids]
    assert len(all_claimed) == len(set(all_claimed)) == len(execution_ids)
    assert set(all_claimed) == set(execution_ids)
