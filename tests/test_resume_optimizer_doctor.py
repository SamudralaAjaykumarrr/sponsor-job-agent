"""CLAUDE.md Phase 14 section 66: resume optimizer doctor."""

from app.candidate.profile import save_profile
from app.jobs_repo import insert_job
from app.models import ApplicationMode, Job
from app.resume_optimizer.doctor import run_doctor
from app.resume_optimizer.optimizer import optimize_resume


def _job() -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote",
        description="Required: Python, PostgreSQL. This role offers H-1B sponsorship.",
        mode=ApplicationMode.ASSIST,
    )


def test_doctor_clean_after_normal_generation(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_id = insert_job(_job())
    optimize_resume(job_id)
    report = run_doctor()
    assert report.serious_count == 0


def test_doctor_flags_stale_profile_version(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_id = insert_job(_job())
    optimize_resume(job_id)

    updated = sample_profile.model_copy(deep=True)
    updated.skills.append("terraform")
    save_profile(updated)

    report = run_doctor()
    assert any(i.check == "stale_profile_version" for i in report.issues)


def test_doctor_flags_missing_artifact(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_id = insert_job(_job())
    optimize_resume(job_id)

    from app.resume_optimizer import repo as ro_repo

    variant = ro_repo.get_current_variant(job_id)
    import os

    os.remove(variant["resume_pdf_path"])

    report = run_doctor()
    assert any(i.check == "missing_artifact" for i in report.issues)


def test_doctor_cli_exit_code(tmp_env, sample_profile, capsys):
    save_profile(sample_profile)
    job_id = insert_job(_job())
    optimize_resume(job_id)

    from app.resume_optimizer.cli import cmd_doctor

    assert cmd_doctor() == 0
