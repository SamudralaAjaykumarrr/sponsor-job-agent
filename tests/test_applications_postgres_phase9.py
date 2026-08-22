"""CLAUDE.md Phase 9 section 40: distributed application-worker acceptance
against REAL PostgreSQL with multiple concurrent claimers. Marked
`postgres` -- skipped automatically if `pgserver` isn't installed (see
tests/conftest.py::postgres_url), exactly like tests/test_applications_postgres.py."""

import json
import threading

import pytest

from app.models import ApplicationMode, Job

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def _mock_job(external_job_id: str, *, company: str = "Acme Corp") -> Job:
    JD_TEXT = (
        "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
        "This is a full-time position. H-1B sponsorship is available for this role."
    )
    return Job(
        title="Backend Software Engineer", company=company, location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, provider_metadata=json.dumps({"mock_scenario": "simple"}),
        mode=ApplicationMode.ASSIST,
    )


def test_four_concurrent_workers_never_double_claim_the_same_execution(pg_db, tmp_env, sample_profile, monkeypatch):
    from app import config
    from app.applications import queue as app_queue
    from app.applications.executor import queue_application
    from app.candidate.profile import save_profile
    from app.pipeline import ingest_and_process

    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    save_profile(sample_profile)

    execution_ids = []
    for i in range(12):
        job = ingest_and_process(_mock_job(f"pg-multi-{i}"))
        result = queue_application(job.id, mode="ASSIST")
        assert result.queued
        execution_ids.append(result.execution_id)

    claimed_by_worker: dict[str, list[str]] = {}
    errors: list[Exception] = []
    lock = threading.Lock()

    def claim_loop(worker_id: str) -> None:
        try:
            rows = app_queue.claim_execution_batch(worker_id=worker_id, limit=20, lease_seconds=120)
            with lock:
                claimed_by_worker[worker_id] = [r["execution_id"] for r in rows]
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=claim_loop, args=(f"pg-worker-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    all_claimed = [eid for ids in claimed_by_worker.values() for eid in ids]
    assert len(all_claimed) == len(set(all_claimed)), "an execution was claimed by more than one worker"
    assert set(all_claimed) == set(execution_ids)


def test_duplicate_submission_race_only_one_execution_wins(pg_db, tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md Phase 9 acceptance C: two 'workers' racing to start an
    execution for the SAME job must result in exactly one owner -- the
    partial unique index on application_executions(job_id) WHERE active=1 is
    the real, atomic guard (Postgres-verified here, SQLite-verified already
    in tests/test_applications_concurrency.py)."""
    from app import config
    from app.applications import repo as applications_repo
    from app.applications.executor import queue_application
    from app.candidate.profile import save_profile
    from app.pipeline import ingest_and_process

    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("pg-race-1"))

    results = []
    errors = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            result = queue_application(job.id, mode="ASSIST")
            with lock:
                results.append(result)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert all(r.queued for r in results)
    execution_ids = {r.execution_id for r in results}
    assert len(execution_ids) == 1
    assert len(applications_repo.list_executions_for_job(job.id)) == 1


def test_full_end_to_end_distributed_worker_reaches_applied(pg_db, tmp_env, sample_profile, monkeypatch):
    """CLAUDE.md Phase 9 acceptance A: FULL_TIME + CONFIRMED + supported mock
    provider -> distributed worker -> submit -> confirm -> APPLIED, driven
    through the real ApplicationWorker against real Postgres."""
    from app import config
    from app.applications.executor import queue_application
    from app.applications.repo import get_execution
    from app.applications.worker import ApplicationWorker
    from app.candidate.profile import save_profile
    from app.pipeline import ingest_and_process

    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("pg-e2e-1"))
    result = queue_application(job.id, mode="AUTO_PERMITTED")

    worker = ApplicationWorker(single_cycle=True)
    worker._run_cycle()

    execution = get_execution(result.execution_id)
    assert execution["status"] == "APPLIED"
    assert execution["confirmation_id"]


def test_four_real_application_workers_process_a_shared_batch_with_no_duplicates(
    pg_db, tmp_env, sample_profile, monkeypatch,
):
    """CLAUDE.md Phase 9 section 40, taken literally: at least 4 concurrent
    REAL ApplicationWorker instances (not just the bare claim primitive)
    against synthetic mock-ATS work, on real PostgreSQL. Verifies clean
    final states and no double-processing."""
    from app import config
    from app.applications.executor import queue_application
    from app.applications.repo import list_executions_for_job
    from app.applications.worker import ApplicationWorker
    from app.candidate.profile import save_profile
    from app.pipeline import ingest_and_process

    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_PROVIDER_CONCURRENCY_DEFAULT", 10)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_DAY", 100)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 100)
    save_profile(sample_profile)

    # Each job is for a genuinely DIFFERENT company (not just a different
    # external_job_id) -- app.applications.duplicate's company+title+location
    # fallback check would otherwise (correctly) flag jobs 2-16 as
    # duplicates of job 1 the moment it reaches APPLIED, since this fixture
    # would otherwise give every job the identical title/company/location.
    job_ids = []
    for i in range(16):
        job = ingest_and_process(_mock_job(f"pg-4worker-{i}", company=f"Acme Corp {i}"))
        job_ids.append(job.id)
        result = queue_application(job.id, mode="AUTO_PERMITTED")
        assert result.queued

    workers = [ApplicationWorker(single_cycle=True) for _ in range(4)]
    errors: list[Exception] = []
    lock = threading.Lock()

    def run_worker(w: ApplicationWorker) -> None:
        try:
            w._run_cycle()
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run_worker, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors

    # Every job ends up with exactly ONE execution row that reached a clean
    # final state -- no duplicate submissions, no execution left dangling.
    for job_id in job_ids:
        executions = list_executions_for_job(job_id)
        assert len(executions) == 1
        assert executions[0]["status"] == "APPLIED"
        assert executions[0]["lease_owner"] is None


def test_global_rate_limit_enforced_across_concurrent_workers(pg_db, tmp_env, sample_profile, monkeypatch):
    from app import config
    from app.applications.executor import queue_application
    from app.applications.repo import get_execution
    from app.applications.worker import ApplicationWorker
    from app.candidate.profile import save_profile
    from app.pipeline import ingest_and_process

    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_HOUR", 2)
    # Isolate the RATE limiter from the (separate, deliberately tiny)
    # per-provider submission CONCURRENCY limit -- raise concurrency so every
    # item genuinely gets attempted within this one cycle, rather than some
    # being cooldown-deferred for an unrelated reason.
    monkeypatch.setattr(config, "APPLICATION_PROVIDER_CONCURRENCY_DEFAULT", 10)
    save_profile(sample_profile)

    execution_ids = []
    for i in range(4):
        job = ingest_and_process(_mock_job(f"pg-rl-{i}"))
        result = queue_application(job.id, mode="AUTO_PERMITTED")
        execution_ids.append(result.execution_id)

    worker = ApplicationWorker(single_cycle=True)
    worker._run_cycle()

    statuses = [get_execution(eid)["status"] for eid in execution_ids]
    assert statuses.count("APPLIED") <= 2
    assert "NEEDS_USER_ACTION" in statuses  # rate-limited ones stop here, not submitted
