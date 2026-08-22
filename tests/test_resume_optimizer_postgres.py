"""CLAUDE.md Phase 14 sections 71-72: real PostgreSQL persistence,
idempotency, stale invalidation, and concurrent-optimization acceptance for
the resume optimizer. Marked `postgres` -- skipped automatically if
`pgserver` isn't installed (see tests/conftest.py::postgres_url), mirroring
tests/test_applications_postgres_phase9.py's dual pg_db + tmp_env pattern."""

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


def _job() -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote",
        description=(
            "Required: Python, FastAPI, PostgreSQL, Docker. "
            "This role offers H-1B sponsorship."
        ),
        mode=ApplicationMode.ASSIST,
    )


def test_variant_persists_across_postgres_reads(pg_db, tmp_env, sample_profile):
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job
    from app.resume_optimizer import repo as ro_repo
    from app.resume_optimizer.optimizer import optimize_resume

    save_profile(sample_profile)
    job_id = insert_job(_job())
    result = optimize_resume(job_id)
    assert result.status == "READY"

    variant = ro_repo.get_current_variant(job_id)
    assert variant is not None
    assert variant["variant_id"] == result.variant_id
    report = ro_repo.get_quality_report_for_job(job_id)
    assert report is not None
    assert report["report"]["job_id"] == job_id


def test_idempotency_over_postgres(pg_db, tmp_env, sample_profile):
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job
    from app.resume_optimizer import repo as ro_repo
    from app.resume_optimizer.optimizer import optimize_resume

    save_profile(sample_profile)
    job_id = insert_job(_job())
    r1 = optimize_resume(job_id)
    r2 = optimize_resume(job_id)
    assert r1.variant_id == r2.variant_id
    assert r2.created is False
    assert len(ro_repo.list_variants_for_job(job_id)) == 1


def test_stale_invalidation_over_postgres(pg_db, tmp_env, sample_profile):
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job
    from app.pipeline import reanalyze_job
    from app.resume_optimizer import repo as ro_repo
    from app.resume_optimizer.optimizer import optimize_resume

    save_profile(sample_profile)
    job_id = insert_job(_job())
    optimize_resume(job_id)
    reanalyze_job(job_id, new_description=_job().description + " Additional: Kafka required.")
    assert ro_repo.get_current_variant(job_id)["status"] == "STALE"

    r2 = optimize_resume(job_id)
    assert r2.created is True
    assert ro_repo.get_current_variant(job_id)["status"] == "READY"
    variants = ro_repo.list_variants_for_job(job_id)
    assert len(variants) == 2
    assert sum(1 for v in variants if v["current"]) == 1


def test_concurrent_optimization_same_identity_never_duplicates(pg_db, tmp_env, sample_profile):
    """CLAUDE.md section 72: N concurrent callers optimizing the SAME job/
    JD/profile/optimizer-version identity must never create more than one
    'current' variant row -- the database's own unique index (not an
    app-level lock) is the actual guard."""
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job
    from app.resume_optimizer import repo as ro_repo
    from app.resume_optimizer.optimizer import optimize_resume

    save_profile(sample_profile)
    job_id = insert_job(_job())

    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            r = optimize_resume(job_id)
            with lock:
                results.append(r.variant_id)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"unexpected errors: {errors}"
    assert len(set(results)) == 1, "concurrent callers for the identical identity produced different variants"

    variants = ro_repo.list_variants_for_job(job_id)
    current_variants = [v for v in variants if v["current"]]
    assert len(current_variants) == 1
    ready_variants = [v for v in variants if v["status"] == "READY"]
    assert len(ready_variants) == 1
